"""Golden recording of the invites, the planner password reset and the wedding-details email:
what each sends today, word for word (APC-120).

Recorded once on the senders as they stood before they moved onto message rows (the planner
invite and its resend, the planner password reset, the staff user invite and its resend, and
the email a couple gets when they send their wedding details). `golden/invites.json` is never
re-recorded after that: a failure here means the move changed what someone receives, or who
receives it.

Each scenario runs on the frozen clock of `golden/scenarios.py` with fixed people and answers.
Every send is captured at the transport: Podium (nothing here should reach it) and the text
part of every email handed to Django's mail backend, compared whitespace-normalised.

The account links carry one-time tokens. Each is recorded as what it opens and which token
check accepts it now (`<UID Dana Planner>/<TOKEN invite>`), so a link to the wrong account, a
reset link that would pass as an invite, or a dead link fails; the wedding resume link's
signed token is named by its lead, as in the other goldens.

Record (once only): GOLDEN_RECORD=1 pytest apps/messaging/tests/test_invites_golden.py
"""

import json
import os
import re
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.urls import Resolver404, resolve
from django.utils.http import urlsafe_base64_decode

from apps.accounts import services as account_services
from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.contacts.factories import ContactFactory
from apps.planners import services as planner_services
from apps.planners.models import PlannerAccount
from apps.planners.tokens import planner_reset_token, planner_token
from apps.public import services as public_services
from apps.public.forms import WeddingRequestForm

from .golden import scenarios as s

pytestmark = pytest.mark.django_db

EXPECTED = Path(__file__).parent / "golden" / "invites.json"
RECORD = os.environ.get("GOLDEN_RECORD") == "1"
SEND = "apps.integrations.podium.send_message"
PASSWORD = "a-long-pass-phrase-77"
WEDDING_DAY = date(2027, 9, 18)


@pytest.fixture(autouse=True)
def _live(settings):
    settings.TOUCHPOINTS_ENABLED = False  # only the send itself
    settings.OUTBOUND_ENABLED = True
    settings.PUBLIC_BASE_URL = s.BASE_URL
    settings.TIME_ZONE = "America/New_York"
    settings.COMPANY_NAME = "All Pro Charter"
    settings.COMPANY_PHONE = "(703) 555-0100"
    settings.COMPANY_EMAIL = "reservations@allprocharter.com"


# --- people ----------------------------------------------------------------------------


def _planner(name="Dana Planner", email="Dana@Example.com"):
    return ContactFactory(name=name, email=email, phone="")


def _admin() -> User:
    return UserFactory(role=User.Role.OWNER_ADMIN, email="owner@allprocharter.com")


def _wedding_answers(**over) -> dict:
    data = {
        "name": "Jane Rider",
        "email": "jane.rider@example.com",
        "phone": "",
        "wedding_date": WEDDING_DAY.isoformat(),
        "venue_name": "The Oak Barn at Loyalty",
        "same_site": "",
        "ceremony_venue_name": "St. John's Church",
        "groups": "guests,party",
        "guest_count": "105",
        "party_count": "14",
        "family_count": "8",
        "hotels_json": json.dumps(
            [
                {"venue_id": None, "name": "Hampton Inn Leesburg"},
                {"venue_id": None, "name": "Lansdowne Resort"},
            ]
        ),
        "ceremony_time": "16:00",
        "end_time": "23:00",
        "notes": "Bridesmaids get ready at an Airbnb in Purcellville & need 2 cars.",
        "company": "",
    }
    form = WeddingRequestForm(data | over)
    assert form.is_valid(), form.errors
    return form.cleaned_data


# --- the sends -------------------------------------------------------------------------


def _planner_invite():
    _, sent = planner_services.invite(_planner(), actor=_admin())
    assert sent is True


def _planner_invite_without_a_name():
    _, sent = planner_services.invite(_planner(name=""), actor=_admin())
    assert sent is True


def _planner_invite_then_resend():
    account, _ = planner_services.invite(_planner(), actor=_admin())
    s.clock.set(s.clock.now.replace(hour=s.clock.now.hour + 1))
    assert planner_services.resend(account) is True


def _planner_password_reset():
    account, _ = planner_services.invite(_planner(), actor=_admin())
    planner_services.register(account, PASSWORD)
    mail.outbox.clear()  # the invite is its own scenario
    assert planner_services.send_password_reset("dana@example.com") is True


def _invite_user(**over):
    values = {
        "first_name": "New",
        "last_name": "Hire",
        "email": "New.Hire@AllProCharter.com",
        "role": User.Role.AGENT,
        "can_manage_payments": False,
        "actor": _admin(),
    }
    return account_services.invite_user(**(values | over))


def _user_invite():
    _, sent = _invite_user()
    assert sent is True


def _user_invite_admin_without_a_first_name():
    _, sent = _invite_user(first_name="", role=User.Role.OWNER_ADMIN)
    assert sent is True


def _user_invite_resend():
    user, _ = _invite_user()
    mail.outbox.clear()  # the invite is its own scenario
    assert account_services.resend_invite(target=user) is True


def _wedding_details():
    lead = public_services.create_lead_from_wedding(_wedding_answers())
    assert public_services.send_wedding_confirmation(lead, base_url=s.BASE_URL) is True


def _wedding_details_updated_from_the_resume_link():
    lead = public_services.create_lead_from_wedding(_wedding_answers())
    lead = public_services.create_lead_from_wedding(
        _wedding_answers(guest_count="120", hotels_json="[]", hotels_tbd="1"), lead=lead
    )
    assert public_services.send_wedding_confirmation(lead, base_url=s.BASE_URL) is True


SENDS = {
    "planner_invite": _planner_invite,
    "planner_invite_without_a_name": _planner_invite_without_a_name,
    "planner_invite_then_resend": _planner_invite_then_resend,
    "planner_password_reset": _planner_password_reset,
    "user_invite": _user_invite,
    "user_invite_admin_without_a_first_name": _user_invite_admin_without_a_first_name,
    "user_invite_resend": _user_invite_resend,
    "wedding_details": _wedding_details,
    "wedding_details_updated_from_the_resume_link": _wedding_details_updated_from_the_resume_link,
}


# --- capture ---------------------------------------------------------------------------

_ACCOUNT_LINK = re.compile(re.escape(s.BASE_URL) + r"(/\S*?/)(?=\s|$)")
# Which token checks each account page's link is meant for, by name.
_CHECKS = {
    "planners:register": {"invite": planner_token, "reset": planner_reset_token},
    "planners:set_password": {"invite": planner_token, "reset": planner_reset_token},
    "accept_invite": {"invite": default_token_generator},
}
_OWNERS = {
    "planners:register": lambda pk: PlannerAccount.objects.get(pk=pk),
    "planners:set_password": lambda pk: PlannerAccount.objects.get(pk=pk),
    "accept_invite": lambda pk: User.objects.get(pk=pk),
}


def _owner_label(owner) -> str:
    if isinstance(owner, PlannerAccount):
        return owner.contact.name or owner.email
    return owner.get_full_name() or owner.email


def _account_link(match: re.Match) -> str:
    """An account page's link with its uid named by its account and its token by the checks
    that accept it now; any other link is left for `scenarios.normalise`."""
    path = match.group(1)
    try:
        found = resolve(path)
    except Resolver404:
        return match.group(0)
    if found.view_name not in _CHECKS:
        return match.group(0)
    uid, token = found.kwargs["uidb64"], found.kwargs["token"]
    owner = _OWNERS[found.view_name](int(urlsafe_base64_decode(uid).decode()))
    passes = [
        name for name, gen in _CHECKS[found.view_name].items() if gen.check_token(owner, token)
    ]
    page = path[: path.index(uid)]
    return f"{s.BASE_URL}{page}<UID {_owner_label(owner)}>/<TOKEN {' '.join(passes) or 'dead'}>/"


def _text(value: str) -> str:
    """Whitespace-normalised, with what differs between runs (tokens, pks, numbers) named."""
    return " ".join(s.normalise(_ACCOUNT_LINK.sub(_account_link, value)).split())


def _outcome(name: str) -> list[dict]:
    """Run the send once at the scenario's moment; every send it made, normalised, sorted."""
    calls: list[dict] = []

    def send(*, identifier, body, channel_type="phone", **_):
        calls.append({"channel_type": channel_type, "identifier": identifier, "body": body})
        return {"uid": f"golden-{len(calls)}"}

    with s.frozen(), patch(SEND, side_effect=send):
        mail.outbox.clear()
        SENDS[name]()
        for message in mail.outbox:
            calls.append(
                {"email_to": list(message.to), "subject": message.subject, "body": message.body}
            )
        normalised = [
            {k: _text(v) if isinstance(v, str) else v for k, v in c.items()} for c in calls
        ]
    return sorted(normalised, key=lambda c: json.dumps(c, sort_keys=True))


def _expected() -> dict:
    return json.loads(EXPECTED.read_text()) if EXPECTED.exists() else {}


@pytest.mark.parametrize("name", sorted(SENDS))
def test_invites_golden(name):
    got = _outcome(name)
    recorded = _expected()
    if RECORD and name not in recorded:
        # Recording only ever adds a new scenario; an existing entry is never rewritten.
        recorded[name] = got
        EXPECTED.write_text(
            json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        return
    assert got == recorded[name]


@pytest.mark.skipif(RECORD, reason="recording")
def test_every_send_is_recorded():
    assert set(_expected()) == set(SENDS)


@pytest.mark.skipif(RECORD, reason="recording")
def test_the_recording_is_real_copy():
    """Every scenario emailed someone; no blank body, no "None", no unrendered placeholder,
    and every account link opens its account with a live token."""
    for name, calls in _expected().items():
        assert calls, name
        for call in calls:
            assert "email_to" in call, name
            text = " ".join(str(v) for v in call.values())
            assert "None" not in text, name
            assert not re.search(r"\{\w+\}", text), name
            assert call["body"].strip(), name
            if "<UID" in call["body"] and name != "planner_invite_then_resend":
                assert "<TOKEN dead>" not in call["body"], name
