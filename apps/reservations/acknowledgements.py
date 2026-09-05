"""Signed-token public acknowledgement links (APC-19 / APC-20 / APC-18).

Same shape as ``apps.leads.services.make_deposit_token`` — an opaque, tamper-evident token
encoding one id — but with its own salts so a leaked trip-sheet link can never stand in for
a deposit link (and each of the three link families is independently revocable by rotating
its salt).
"""

from __future__ import annotations

from datetime import date

from django.core import signing
from django.urls import reverse

_TRIP_DAY_ACK_SALT = "apc.trip-day-ack.v1"
_AFFILIATE_ACK_SALT = "apc.affiliate-ack.v1"
_WEDDING_DETAILS_SALT = "apc.wedding-details.v1"


# --- customer trip confirmation (APC-19) ------------------------------------------------
# Keyed on the customer + the local pickup date, not one reservation: a customer with
# several trips that day confirms them together from one link. The trip set is resolved
# when the link is opened, so a trip added or cancelled after the notice went out is
# reflected then rather than frozen into the token.
def make_trip_day_ack_token(contact, day) -> str:
    return signing.dumps({"contact": contact.pk, "date": day.isoformat()}, salt=_TRIP_DAY_ACK_SALT)


def read_trip_day_ack_token(token: str):
    """Returns ``(contact, date)``. Raises BadSignature on a forged or foreign token."""
    from apps.contacts.models import Contact

    data = signing.loads(token, salt=_TRIP_DAY_ACK_SALT)
    return Contact.objects.get(pk=data["contact"]), date.fromisoformat(data["date"])


def trip_day_ack_url(contact, day, *, base_url: str = "") -> str:
    token = make_trip_day_ack_token(contact, day)
    return f"{base_url}{reverse('trip_confirm', args=[token])}"


# --- affiliate trip confirmation (APC-20) -----------------------------------------------
def make_affiliate_ack_token(assignment) -> str:
    return signing.dumps({"assignment": assignment.pk}, salt=_AFFILIATE_ACK_SALT)


def read_affiliate_ack_token(token: str):
    from apps.dispatch.models import Assignment

    data = signing.loads(token, salt=_AFFILIATE_ACK_SALT)
    return Assignment.objects.get(pk=data["assignment"])


def affiliate_ack_url(assignment, *, base_url: str = "") -> str:
    return (
        f"{base_url}"
        f"{reverse('affiliate_trip_confirm', args=[make_affiliate_ack_token(assignment)])}"
    )


# --- wedding day-of details (APC-18) ---------------------------------------------------
def make_wedding_details_token(reservation) -> str:
    return signing.dumps({"reservation": reservation.pk}, salt=_WEDDING_DETAILS_SALT)


def read_wedding_details_token(token: str):
    from .models import Reservation

    data = signing.loads(token, salt=_WEDDING_DETAILS_SALT)
    return Reservation.objects.get(pk=data["reservation"])


def wedding_details_url(reservation, *, base_url: str = "") -> str:
    return f"{base_url}{reverse('wedding_details', args=[make_wedding_details_token(reservation)])}"
