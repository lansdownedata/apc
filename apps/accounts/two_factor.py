"""One-time sign-in codes: issue, limit resends, verify. Sending lives elsewhere."""

import enum
import math
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import SignInCode, User

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
RESEND_GAP = timedelta(seconds=60)
RESEND_PER_HOUR = 5
KEEP_FOR = timedelta(hours=24)
_SALT = "apps.accounts.two_factor.code"


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
