"""Copying and recurring a trip from the order page, not just the quote.

An order already grows trips — "Add reservation" sits in its Trips header — but the two
controls that grow it fastest were sales-only: duplicate (a second identical vehicle) and
copy-to-dates (the same run on Sep 8/9/10). Ops had to go back to the quote for both, and
on a booked order that is the screen they are trying not to be on.

Both were gated behind `actions == "sales"` in `components/trip_line.html` because they
needed the quote workspace's Alpine scope. They are now scope-free — a global function and
a window event — so the shared partial can offer them wherever a trip is listed. What
stays sales-only is what destroys work: reverse, return and remove.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory, StopFactory

pytestmark = pytest.mark.django_db


def _order(**over) -> Lead:
    return LeadFactory(status=Lead.Status.BOOKED, **over)


def _trip(lead, **over):
    trip = ReservationFactory(lead=lead, rate=Decimal("500"), hours=1, min_hours=0, **over)
    StopFactory(reservation=trip, sequence=0, address="Dulles International")
    StopFactory(reservation=trip, sequence=1, address="The Hay-Adams")
    return trip


@pytest.fixture
def owner(client):
    client.force_login(UserFactory(role=User.Role.OWNER_ADMIN))
    return client


# --- the controls reach the order page ----------------------------------------------


def test_an_order_trip_can_be_duplicated(owner):
    order = _order()
    trip = _trip(order)

    body = owner.get(reverse("order_detail", args=[order.pk])).content.decode()

    assert f"duplicateReservation({trip.pk})" in body
    assert reverse("reservation_duplicate", args=[trip.pk]) in body


def test_an_order_trip_can_be_copied_to_dates(owner):
    order = _order()
    trip = _trip(order)

    body = owner.get(reverse("order_detail", args=[order.pk])).content.decode()

    assert f"'copy-dates-open', {{ id: {trip.pk}" in body
    assert reverse("reservation_copy_dates") in body, "the modal itself is not on the page"


def test_the_destructive_controls_stay_on_the_quote(owner):
    """Copy and recur add work; reverse, return and remove undo it. An order is booked —
    those belong on the screen that is still selling it."""
    order = _order()
    trip = _trip(order)

    body = owner.get(reverse("order_detail", args=[order.pk])).content.decode()

    assert reverse("reservation_reverse", args=[trip.pk]) not in body
    assert reverse("reservation_return", args=[trip.pk]) not in body
    assert reverse("reservation_delete", args=[trip.pk]) not in body


# --- and hand the agent back to the order --------------------------------------------


def test_duplicating_from_an_order_comes_back_to_the_order(owner):
    """Both endpoints redirect to the quote workspace, which is exactly the screen ops
    was avoiding. `next` carries where the agent actually was."""
    order = _order()
    trip = _trip(order)
    back = reverse("order_detail", args=[order.pk])

    resp = owner.post(reverse("reservation_duplicate", args=[trip.pk]), {"count": 2, "next": back})

    assert resp.status_code == 302 and resp.url == back
    assert order.reservations.count() == 3


def test_copying_to_dates_from_an_order_comes_back_to_the_order(owner):
    order = _order()
    trip = _trip(order)
    back = reverse("order_detail", args=[order.pk])

    resp = owner.post(
        reverse("reservation_copy_dates"),
        {"reservation": trip.pk, "dates": ["2027-03-01", "2027-03-02"], "next": back},
    )

    assert resp.status_code == 302 and resp.url == back
    assert order.reservations.count() == 3


def test_without_a_next_both_still_land_on_the_quote(owner):
    """The workspace's own buttons send no `next` — they must not change behaviour."""
    order = _order()
    trip = _trip(order)
    quote = reverse("lead_detail", args=[order.pk])

    assert owner.post(reverse("reservation_duplicate", args=[trip.pk])).url == quote
    assert (
        owner.post(
            reverse("reservation_copy_dates"),
            {"reservation": trip.pk, "dates": ["2027-03-01"]},
        ).url
        == quote
    )


@pytest.mark.parametrize("hostile", ["https://evil.example.com/", "//evil.example.com/"])
def test_an_off_site_next_is_refused(owner, hostile):
    """`next` rides in on a POST body, so it is an open redirect until it is checked."""
    order = _order()
    trip = _trip(order)

    resp = owner.post(reverse("reservation_duplicate", args=[trip.pk]), {"next": hostile})

    assert resp.url == reverse("lead_detail", args=[order.pk])


def test_an_agent_without_payments_access_can_still_copy_a_trip(client):
    """Copying a trip is trip work, not money work — the order page's money block is
    what payments access gates."""
    client.force_login(UserFactory(role=User.Role.AGENT, can_manage_payments=False))
    order = _order()
    trip = _trip(order)

    body = client.get(reverse("order_detail", args=[order.pk])).content.decode()

    assert f"duplicateReservation({trip.pk})" in body


def test_the_duplicate_picker_does_not_call_an_order_a_quote():
    """The same picker opens from the quote and from a booked order (APC-87)."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[3] / "static" / "js" / "app.js").read_text()
    start = js.index("function duplicateReservation")
    picker = js[start : js.index("window.duplicateReservation", start)]
    assert "Duplicate reservation" in picker
    assert "to the quote" not in picker
