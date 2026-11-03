import json
from pathlib import Path

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.reservations import groups
from apps.reservations.editor import editor_context, reservation_draft
from apps.reservations.factories import PassengerFactory, TransferReservationFactory

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[3]

DANA = {"name": "Dana Whitfield", "mobile": "(202) 555-0147", "lead": True}
PRIYA = {"name": "Priya Shah", "mobile": "", "lead": False}


def _payload(res, **over):
    base = {
        "lead_id": res.lead_id,
        "id": res.pk,
        "tripType": "transfer",
        "date": "2026-07-04",
        "time": "15:00",
        "pax": 4,
        "rate": 200,
        "hours": 1,
        "minHours": 0,
        "stops": [{"address": "A"}, {"address": "B"}],
    }
    base.update(over)
    return base


def _post(client, payload):
    return client.post(
        reverse("reservation_save"), data=json.dumps(payload), content_type="application/json"
    )


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def test_draft_round_trips_passengers(staff):
    res = TransferReservationFactory()
    resp = _post(staff, _payload(res, passengerList=[DANA, PRIYA]))
    assert resp.status_code == 302
    rows = list(res.named_passengers.order_by("sort"))
    assert [(p.name, p.mobile, p.is_lead) for p in rows] == [
        ("Dana Whitfield", "+12025550147", True),
        ("Priya Shah", "", False),
    ]
    res.refresh_from_db()
    assert reservation_draft(res)["passengerList"] == [
        {"name": "Dana Whitfield", "mobile": "(202) 555-0147", "lead": True},
        {"name": "Priya Shah", "mobile": "", "lead": False},
    ]


def test_save_without_passenger_key_keeps_existing_list(staff):
    res = TransferReservationFactory()
    PassengerFactory(reservation=res, name="Keep Me")
    assert _post(staff, _payload(res)).status_code == 302
    assert list(res.named_passengers.values_list("name", flat=True)) == ["Keep Me"]


def test_empty_passenger_list_clears_the_list(staff):
    res = TransferReservationFactory()
    PassengerFactory(reservation=res)
    assert _post(staff, _payload(res, passengerList=[])).status_code == 302
    assert res.named_passengers.count() == 0


def test_group_copies_start_with_no_passengers(staff):
    res = TransferReservationFactory()
    PassengerFactory(reservation=res, name="A")
    PassengerFactory(reservation=res, name="B")
    groups.set_group_size(res, 3)
    copies = res.lead.reservations.exclude(pk=res.pk)
    assert copies.count() == 2
    assert all(c.named_passengers.count() == 0 for c in copies)
    assert res.named_passengers.count() == 2


def test_apply_to_group_does_not_copy_passengers(staff):
    res = TransferReservationFactory()
    groups.set_group_size(res, 2)
    sibling = res.lead.reservations.exclude(pk=res.pk).get()
    PassengerFactory(reservation=sibling, name="Sibling's own")
    PassengerFactory(reservation=res, name="Mine")
    groups.apply_to_group(res)
    assert list(sibling.named_passengers.values_list("name", flat=True)) == ["Sibling's own"]


def test_bad_mobile_is_a_400_with_the_message(staff):
    res = TransferReservationFactory()
    resp = _post(staff, _payload(res, passengerList=[{"name": "A", "mobile": "12"}]))
    assert resp.status_code == 400
    assert "Passenger 1's mobile isn't a valid phone number." in resp.content.decode()


def test_editor_context_carries_the_booking_contact_name(rf):
    res = TransferReservationFactory()
    ctx = editor_context(rf.get("/"), res.lead, [res])
    assert ctx["booking_contact_name"] == res.lead.contact.name
    assert editor_context(rf.get("/"), None, [])["booking_contact_name"] == ""


def test_draft_view_hands_over_the_booking_contact_name(staff):
    res = TransferReservationFactory()
    body = staff.get(reverse("reservation_draft", args=[res.pk])).json()
    assert body["bookingContactName"] == res.lead.contact.name


def test_editor_template_has_the_passenger_section_strings():
    html = (ROOT / "templates/leads/_reservation_editor.html").read_text()
    for text in (
        "Passenger list",
        "Add passenger",
        "Make lead",
        "Needs mobile",
        "The lead passenger gets every passenger message for this trip.",
        "Mark a lead to send to one person only.",
        "(booking contact) gets them.",
        "Update the passenger count if that's right.",
    ):
        assert text in html


def test_editor_html_escapes_the_booking_contact_name(staff):
    res = TransferReservationFactory()
    res.lead.contact.name = 'Dana "DJ" O\'Neil'
    res.lead.contact.save()
    html = staff.get(reverse("lead_detail", args=[res.lead_id])).content.decode()
    assert "bookingContactName: &quot;Dana \\&quot;DJ\\&quot;" in html


def test_passenger_inputs_are_sized_by_wrappers_not_by_w_classes():
    """.field is width:100% and beats w-* on an input, so sizing must sit on a wrapper.
    This only greps the markup (no layout engine in tests)."""
    html = (ROOT / "templates/leads/_reservation_editor.html").read_text()
    for label in ("Passenger name", "Passenger mobile"):
        tag = html[html.index(f'aria-label="{label}"') :].split(">", 1)[0]
        assert "sm:w-" not in tag and "flex-none" not in tag and "sm:flex-1" not in tag
    assert 'class="min-w-0 flex-1 sm:w-36 sm:flex-none" data-passenger-mobile' in html
