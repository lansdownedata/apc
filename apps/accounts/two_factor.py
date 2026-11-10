"""One-time sign-in codes: issue, limit resends, verify. Sending lives elsewhere."""

import enum
import logging
import math
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.integrations import podium
from apps.notifications.email import send_html_email

from .models import SignInCode, User

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_GAP = timedelta(seconds=60)
RESEND_PER_HOUR = 5
KEEP_FOR = timedelta(hours=24)
_SALT = "apps.accounts.two_factor.code"
PENDING_TTL = 600  # seconds a half-finished sign-in stays open
REMEMBER_AGE = 30 * 24 * 60 * 60
COOKIES = {"staff": ("apc_staff_device", "/"), "planner": ("apc_planner_device", "/planner/")}
CODE_BODY_RE = re.compile(r"your sign-in code is \d{6}\.")


@dataclass(frozen=True)
class Subject:
    """Whoever is signing in, flattened so staff and planners share one code path."""

    kind: Literal["staff", "planner"]
    pk: int
    phone: str  # E.164 or ""
    email: str
    first_name: str
    password_hash: str


def staff_subject(user: User) -> Subject:
    return Subject("staff", user.pk, user.phone, user.email, user.first_name, user.password)


def enabled() -> bool:
    return bool(settings.TWO_FACTOR_ENABLED)


class ResendLimited(Exception):
    def __init__(self, retry_in: int) -> None:
        super().__init__(f"Next code allowed in {retry_in}s")
        self.retry_in = retry_in


class Verdict(enum.Enum):
    OK = "ok"
    WRONG = "wrong"
    EXPIRED = "expired"
    LOCKED = "locked"
    MISSING = "missing"


def _rows(subject: Subject):
    key = "user_id" if subject.kind == "staff" else "planner_id"
    return SignInCode.objects.filter(**{key: subject.pk})


def _hash(subject: Subject, code: str) -> str:
    return salted_hmac(_SALT, f"{subject.kind}:{subject.pk}:{code}").hexdigest()


def _seconds_until(moment, now) -> int:
    return max(1, math.ceil((moment - now).total_seconds()))


def issue(subject: Subject, channel: str) -> tuple[SignInCode, str]:
    """Create a fresh code, voiding the subject's earlier live ones. Raises ResendLimited."""
    now = timezone.now()
    with transaction.atomic():
        rows = _rows(subject)
        rows.filter(created_at__lt=now - KEEP_FOR).delete()
        recent = list(rows.filter(created_at__gt=now - timedelta(hours=1)).order_by("created_at"))
        waits = []
        if recent and now - recent[-1].created_at < RESEND_GAP:
            waits.append(_seconds_until(recent[-1].created_at + RESEND_GAP, now))
        if len(recent) >= RESEND_PER_HOUR:
            oldest = recent[len(recent) - RESEND_PER_HOUR]
            waits.append(_seconds_until(oldest.created_at + timedelta(hours=1), now))
        if waits:
            raise ResendLimited(max(waits))
        for row in rows.filter(consumed_at__isnull=True):
            row.consumed_at = now
            row.save(update_fields=["consumed_at"])
        raw = f"{secrets.randbelow(10**6):06d}"
        owner = {"user_id": subject.pk} if subject.kind == "staff" else {"planner_id": subject.pk}
        row = SignInCode.objects.create(
            code_hash=_hash(subject, raw),
            channel=channel,
            created_at=now,
            expires_at=now + CODE_TTL,
            **owner,
        )
    return row, raw


def verify(subject: Subject, raw: str) -> Verdict:
    """Check a typed code against the subject's newest live one; spends a try when wrong."""
    code = re.sub(r"\D", "", raw)
    now = timezone.now()
    with transaction.atomic():
        rows = _rows(subject).select_for_update()
        row = rows.filter(consumed_at__isnull=True).order_by("-created_at", "-pk").first()
        if row is None:
            last = rows.order_by("-created_at", "-pk").first()
            if last and last.attempts >= MAX_ATTEMPTS and last.expires_at > now:
                return Verdict.LOCKED
            return Verdict.MISSING
        if row.expires_at <= now:
            return Verdict.EXPIRED
        if constant_time_compare(row.code_hash, _hash(subject, code)):
            row.consumed_at = now
            row.save(update_fields=["consumed_at"])
            return Verdict.OK
        row.attempts += 1
        if row.attempts >= MAX_ATTEMPTS:
            row.consumed_at = now
            row.save(update_fields=["attempts", "consumed_at"])
            return Verdict.LOCKED
        row.save(update_fields=["attempts"])
        return Verdict.WRONG


@dataclass(frozen=True)
class Delivery:
    channel: str  # "sms" | "email" | "" when nothing went
    fell_back: bool  # asked for sms, went by email

    @property
    def sent(self) -> bool:
        return bool(self.channel)


def mask_phone(phone: str) -> str:
    return phone[-4:]


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}"


def is_code_echo(uid: str, body: str) -> bool:
    """True for Podium's `message.sent` echo of one of our own sign-in texts, which must
    never reach the Inbox."""
    if uid and SignInCode.objects.filter(podium_message_uid=uid).exists():
        return True
    return bool(body and CODE_BODY_RE.search(body))


def _email_code(subject: Subject, raw: str, reason: str) -> bool:
    from apps.messaging import rendering

    logo = rendering.logo_path()
    return send_html_email(
        to=subject.email,
        subject=f"Your {settings.COMPANY_NAME} sign-in code",
        template="sign_in_code",
        context={
            "first_name": subject.first_name,
            "code_display": f"{raw[:3]} {raw[3:]}",
            "reason": reason,
            "phone_last4": mask_phone(subject.phone),
            "company_name": settings.COMPANY_NAME,
            "company_phone": settings.COMPANY_PHONE,
            "company_email": settings.COMPANY_EMAIL,
            "logo_cid": "logo" if logo else "",
        },
        inline_images={"logo": logo} if logo else None,
        internal=True,
    )


def send_code(subject: Subject, *, prefer: str = "sms") -> Delivery:
    """Issue a code and deliver it: by text when asked and possible, else by email. A failed
    text falls back to emailing the same code. May raise ResendLimited."""
    if prefer == "email" or not subject.phone:
        row, raw = issue(subject, SignInCode.Channel.EMAIL)
        reason = "requested" if subject.phone else "no_phone"
        went = _email_code(subject, raw, reason)
        return Delivery("email" if went else "", False)

    row, raw = issue(subject, SignInCode.Channel.SMS)
    body = (
        f"{settings.COMPANY_NAME}: your sign-in code is {raw}. "
        "It expires in 10 minutes. Never share this code."
    )
    try:
        response = podium.send_message(identifier=subject.phone, body=body, internal=True)
    except Exception as exc:
        logger.warning("sign-in text to ...%s failed: %s", mask_phone(subject.phone), exc)
    else:
        uid = ""
        if isinstance(response, dict):
            uid = response.get("uid") or (response.get("data") or {}).get("uid") or ""
        if uid:
            row.podium_message_uid = uid
            row.save(update_fields=["podium_message_uid"])
        return Delivery("sms", False)

    row.channel = SignInCode.Channel.EMAIL
    row.save(update_fields=["channel"])
    went = _email_code(subject, raw, "fallback")
    return Delivery("email" if went else "", True)


@dataclass(frozen=True)
class Pending:
    """A password-checked sign-in still waiting for its code."""

    pk: int
    next_url: str
    channel: str  # how the latest code went: "sms" | "email" | ""
    fell_back: bool


def _pending_key(kind: str) -> str:
    return f"two_factor_pending_{kind}"


def begin(request, subject: Subject, *, next_url: str, delivery: Delivery) -> None:
    """Mark the session as password-verified for this subject. Does not sign anyone in."""
    request.session[_pending_key(subject.kind)] = {
        "pk": subject.pk,
        "next_url": next_url,
        "channel": delivery.channel,
        "fell_back": delivery.fell_back,
        "at": timezone.now().timestamp(),
    }


def pending(request, kind: str) -> Pending | None:
    data = request.session.get(_pending_key(kind))
    try:
        if not data or timezone.now().timestamp() - float(data["at"]) > PENDING_TTL:
            return None
        return Pending(
            int(data["pk"]), str(data["next_url"]), str(data["channel"]), bool(data["fell_back"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def note_delivery(request, kind: str, delivery: Delivery) -> None:
    data = request.session.get(_pending_key(kind))
    if data:
        request.session[_pending_key(kind)] = {
            **data,
            "channel": delivery.channel,
            "fell_back": delivery.fell_back,
        }


def clear(request, kind: str) -> None:
    request.session.pop(_pending_key(kind), None)


def _device_salt(kind: str) -> str:
    return f"apps.accounts.two_factor.device.{kind}"


def _device_hmac(subject: Subject) -> str:
    return salted_hmac(
        f"{_device_salt(subject.kind)}.hmac", f"{subject.password_hash}:{subject.phone}"
    ).hexdigest()


def remember(response, subject: Subject) -> None:
    """Skip the code on this browser for 30 days, until the password or phone changes."""
    name, path = COOKIES[subject.kind]
    value = signing.dumps(
        {"pk": subject.pk, "h": _device_hmac(subject)}, salt=_device_salt(subject.kind)
    )
    response.set_cookie(
        name,
        value,
        max_age=REMEMBER_AGE,
        path=path,
        httponly=True,
        samesite="Lax",
        secure=settings.SESSION_COOKIE_SECURE,
    )


def is_remembered(request, subject: Subject) -> bool:
    raw = request.COOKIES.get(COOKIES[subject.kind][0])
    if not raw:
        return False
    try:
        data = signing.loads(raw, salt=_device_salt(subject.kind), max_age=REMEMBER_AGE)
        return data["pk"] == subject.pk and constant_time_compare(
            str(data["h"]), _device_hmac(subject)
        )
    except (signing.BadSignature, KeyError, TypeError):
        return False


def forget(response, kind: str) -> None:
    name, path = COOKIES[kind]
    response.delete_cookie(name, path=path)
