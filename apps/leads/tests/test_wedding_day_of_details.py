"""APC-18 — the captured T-7d wedding day-of contact + wedding name on the workspace."""

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.reservations.factories import ReservationFactory
from apps.reservations.services import is_wedding_trip

pytestmark = pytest.mark.django_db


# --- is_wedding_trip -------------------------------------------------------------------


def test_service_type_named_wedding_transportation_is_a_wedding():
    wedding = ServiceTypeFactory(name="Wedding Transportation")
    res = ReservationFactory(service_type=wedding)
    assert is_wedding_trip(res) is True


def test_a_wedding_builder_leg_is_a_wedding_even_without_the_service_type():
    res = ReservationFactory(source_leg_id="guests-in")
    assert is_wedding_trip(res) is True


def test_an_ordinary_trip_is_not_a_wedding():
    res = ReservationFactory()
    assert is_wedding_trip(res) is False


# --- workspace surface -----------------------------------------------------------------


def test_workspace_shows_the_card_for_a_wedding_trip(client):
    wedding = ServiceTypeFactory(name="Wedding Transportation")
    lead = LeadFactory()
    ReservationFactory(lead=lead, service_type=wedding)
    client.force_login(UserFactory())

    resp = client.get(reverse("lead_detail", args=[lead.pk]))

    assert resp.status_code == 200
    assert b"Day-of wedding details" in resp.content
    assert b"not yet provided" in resp.content


def test_workspace_hides_the_card_for_a_non_wedding_lead(client):
    lead = LeadFactory()
    ReservationFactory(lead=lead)
    client.force_login(UserFactory())

    resp = client.get(reverse("lead_detail", args=[lead.pk]))

    assert b"Day-of wedding details" not in resp.content


def test_workspace_omits_the_not_yet_provided_flag_once_both_are_captured(client):
    wedding = ServiceTypeFactory(name="Wedding Transportation")
    lead = LeadFactory(wedding_name="Boyne–Ellis Wedding", day_of_contact_name="Jamie Planner")
    ReservationFactory(lead=lead, service_type=wedding)
    client.force_login(UserFactory())

    resp = client.get(reverse("lead_detail", args=[lead.pk]))

    assert b"Day-of wedding details" in resp.content
    assert b"not yet provided" not in resp.content


# --- lead_update writes the fields -------------------------------------------------------


def test_lead_update_saves_wedding_day_of_fields(client):
    lead = LeadFactory()
    client.force_login(UserFactory())

    resp = client.post(
        reverse("lead_update", args=[lead.pk]),
        {
            "wedding_name": "Boyne–Ellis Wedding",
            "day_of_contact_name": "Jamie Planner",
            "day_of_contact_phone": "(703) 555-0148",
        },
    )

    assert resp.status_code == 200
    lead.refresh_from_db()
    assert lead.wedding_name == "Boyne–Ellis Wedding"
    assert lead.day_of_contact_name == "Jamie Planner"
    assert lead.day_of_contact_phone == "+17035550148"


def test_lead_update_rejects_an_invalid_day_of_contact_phone(client):
    lead = LeadFactory(day_of_contact_phone="+12025550100")
    client.force_login(UserFactory())

    resp = client.post(reverse("lead_update", args=[lead.pk]), {"day_of_contact_phone": "12345"})

    assert resp.status_code == 400
    assert "day-of contact phone" in resp.json()["error"].lower()
    lead.refresh_from_db()
    assert lead.day_of_contact_phone == "+12025550100"


def test_lead_update_writes_nothing_when_day_of_contact_phone_is_invalid(client):
    """An invalid day-of phone must reject the whole request before any field is written —
    including the contact's own phone, which is validated and saved earlier in the view."""
    lead = LeadFactory(day_of_contact_phone="")
    lead.contact.phone = "+12025550100"
    lead.contact.save(update_fields=["phone"])
    client.force_login(UserFactory())

    resp = client.post(
        reverse("lead_update", args=[lead.pk]),
        {"phone": "(703) 555-0148", "day_of_contact_phone": "12345"},
    )

    assert resp.status_code == 400
    lead.refresh_from_db()
    lead.contact.refresh_from_db()
    assert lead.contact.phone == "+12025550100"
    assert lead.day_of_contact_phone == ""
