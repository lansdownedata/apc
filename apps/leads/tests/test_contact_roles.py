"""APC-64 — contact roles on an order, and the day-of contact moving onto them."""

import importlib
from datetime import time, timedelta
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.contacts.models import Contact, ContactPhone
from apps.leads import contact_roles
from apps.leads.factories import LeadFactory, ServiceTypeFactory
from apps.leads.models import Lead, LeadContact
from apps.public.services import WEDDING_SERVICE_NAME
from apps.reservations import acknowledgements as ack
from apps.reservations.factories import ReservationFactory
from apps.tasks import services as tasks
from apps.tasks.models import Task

pytestmark = pytest.mark.django_db

DAY_OF = LeadContact.Role.DAY_OF_COORDINATOR
MIGRATION = "apps.leads.migrations.0016_day_of_contact_roles"


@pytest.fixture(autouse=True)
def _quiet():
    with patch("apps.integrations.la_sync.push_lead_bookings"):
        yield


def _legacy(lead, name="Jordan Planner", phone="+12025550143"):
    """A lead as it stood before APC-64: the day-of contact only in the old columns."""
    Lead.objects.filter(pk=lead.pk).update(day_of_contact_name=name, day_of_contact_phone=phone)
    lead.refresh_from_db()
    return lead


def _backfill():
    return contact_roles.backfill_day_of_roles(Lead, Contact, ContactPhone, LeadContact)


def _wedding(status=Lead.Status.BOOKED):
    lead = LeadFactory(status=status)
    trip = ReservationFactory(
        lead=lead,
        service_type=ServiceTypeFactory(name=WEDDING_SERVICE_NAME),
        pickup_date=timezone.localdate() + timedelta(days=20),
        pickup_time=time(15, 0),
        pickup_timezone="America/New_York",
    )
    return lead, trip


# --- the data migration --------------------------------------------------------------


def test_backfill_turns_the_day_of_fields_into_a_contact_and_a_role():
    lead = _legacy(LeadFactory())

    assert _backfill() == 1

    role = LeadContact.objects.get(lead=lead)
    assert role.role == DAY_OF
    assert (role.contact.name, role.contact.phone) == ("Jordan Planner", "+12025550143")
    assert role.contact.phones.filter(number="+12025550143", texting=True).exists()


def test_backfill_is_safe_to_rerun():
    _legacy(LeadFactory())
    _backfill()

    assert _backfill() == 0
    assert LeadContact.objects.count() == 1
    assert Contact.objects.filter(name="Jordan Planner").count() == 1


def test_backfill_dedupes_on_phone_like_contact_creation():
    existing = ContactFactory(name="Jordan P.", phone="+12025550143")
    first = _legacy(LeadFactory())
    second = _legacy(LeadFactory())

    _backfill()

    assert LeadContact.objects.get(lead=first).contact == existing
    assert LeadContact.objects.get(lead=second).contact == existing


def test_backfill_matches_an_extra_number_too():
    existing = ContactFactory(phone="+12025550100")
    existing.phones.create(number="+12025550143")
    lead = _legacy(LeadFactory())

    _backfill()

    assert LeadContact.objects.get(lead=lead).contact == existing


def test_backfill_skips_leads_with_no_day_of_contact():
    LeadFactory()

    assert _backfill() == 0
    assert not LeadContact.objects.exists()


def test_the_migration_runs_on_historical_models_and_reruns_safely():
    lead = _legacy(LeadFactory(), phone="(202) 555-0143")
    forwards = importlib.import_module(MIGRATION).forwards

    forwards(django_apps, None)
    forwards(django_apps, None)

    role = LeadContact.objects.get(lead=lead)
    assert role.contact.phone == "+12025550143"


# --- readers: the same values before and after ---------------------------------------


def test_the_reader_returns_what_the_columns_held():
    lead = _legacy(LeadFactory())
    _backfill()

    assert contact_roles.day_of_contact(lead) == ("Jordan Planner", "+12025550143")


def test_no_role_reads_as_blank():
    assert contact_roles.day_of_contact(LeadFactory()) == ("", "")


def test_the_day_of_task_closes_from_the_role():
    lead, _trip = _wedding()
    tasks.ensure_tasks(lead)
    task = Task.objects.get(lead=lead, kind="day_of_contact")
    assert task.status == Task.Status.OPEN

    contact_roles.set_day_of_contact(lead, name="Jordan Planner", phone="(202) 555-0143")
    tasks.evaluate_lead(lead)

    task.refresh_from_db()
    assert task.status == Task.Status.DONE


def test_a_role_without_a_phone_doesnt_close_the_day_of_task():
    lead, _trip = _wedding()
    tasks.ensure_tasks(lead)

    contact_roles.set_day_of_contact(lead, name="Jordan Planner", phone="")
    tasks.evaluate_lead(lead)

    assert Task.objects.get(lead=lead, kind="day_of_contact").status == Task.Status.OPEN


def test_the_wedding_details_page_reads_and_writes_the_role(client):
    lead, trip = _wedding()
    url = reverse("wedding_details", args=[ack.make_wedding_details_token(trip)])

    client.post(
        url,
        {
            "wedding_name": "Kim & Lee",
            "contact_name": "Jordan Planner",
            "contact_phone": "202-555-0143",
        },
    )

    lead.refresh_from_db()
    # Both, for one release.
    assert (lead.day_of_contact_name, lead.day_of_contact_phone) == (
        "Jordan Planner",
        "+12025550143",
    )
    assert contact_roles.day_of_contact(lead) == ("Jordan Planner", "+12025550143")
    page = client.get(url)
    assert page.context["submitted"] is True
    assert (page.context["contact_name"], page.context["contact_phone"]) == (
        "Jordan Planner",
        "+12025550143",
    )


def test_the_workspace_header_writes_both_and_reads_the_role(client):
    lead, _trip = _wedding(status=Lead.Status.QUOTED)
    client.force_login(UserFactory())

    resp = client.post(
        reverse("lead_update", args=[lead.pk]),
        {"day_of_contact_name": "Jordan Planner", "day_of_contact_phone": "202-555-0143"},
    )

    assert resp.status_code == 200
    lead.refresh_from_db()
    assert lead.day_of_contact_name == "Jordan Planner"
    assert contact_roles.day_of_contact(lead) == ("Jordan Planner", "+12025550143")
    body = client.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Jordan Planner" in body


def test_clearing_the_day_of_fields_removes_the_role(client):
    lead, _trip = _wedding(status=Lead.Status.QUOTED)
    contact_roles.set_day_of_contact(lead, name="Jordan Planner", phone="202-555-0143")
    client.force_login(UserFactory())

    client.post(
        reverse("lead_update", args=[lead.pk]),
        {"day_of_contact_name": "", "day_of_contact_phone": ""},
    )

    assert not LeadContact.objects.filter(lead=lead, role=DAY_OF).exists()


def test_changing_the_phone_moves_the_role_to_another_contact():
    lead = LeadFactory()
    contact_roles.set_day_of_contact(lead, name="Jordan Planner", phone="202-555-0143")

    contact_roles.set_day_of_contact(lead, name="Sam Backup", phone="202-555-0199")

    [role] = LeadContact.objects.filter(lead=lead, role=DAY_OF)
    assert role.contact.name == "Sam Backup"


def test_a_name_fix_renames_a_contact_that_is_only_a_role():
    lead = LeadFactory()
    contact_roles.set_day_of_contact(lead, name="Jordan Plannr", phone="202-555-0143")

    contact_roles.set_day_of_contact(lead, name="Jordan Planner", phone="202-555-0143")

    assert contact_roles.day_of_contact(lead) == ("Jordan Planner", "+12025550143")


def test_a_customer_is_never_renamed_from_the_day_of_field():
    customer = LeadFactory().contact
    customer.phone = "+12025550143"
    customer.save()
    other = LeadFactory()

    contact_roles.set_day_of_contact(other, name="Somebody Else", phone="202-555-0143")

    customer.refresh_from_db()
    assert customer.name != "Somebody Else"
    assert LeadContact.objects.get(lead=other, role=DAY_OF).contact == customer


# --- the model -----------------------------------------------------------------------


def test_one_row_per_lead_contact_role():
    lead, person = LeadFactory(), ContactFactory()
    LeadContact.objects.create(lead=lead, contact=person, role=DAY_OF)

    with pytest.raises(IntegrityError), transaction.atomic():
        LeadContact.objects.create(lead=lead, contact=person, role=DAY_OF)


def test_a_contact_can_hold_several_roles():
    lead, person = LeadFactory(), ContactFactory()
    LeadContact.objects.create(lead=lead, contact=person, role=LeadContact.Role.LEAD_PLANNER)
    LeadContact.objects.create(lead=lead, contact=person, role=DAY_OF)

    assert LeadContact.objects.filter(lead=lead, contact=person).count() == 2


# --- the People card endpoints -------------------------------------------------------


@pytest.fixture
def staff(client):
    client.force_login(UserFactory())
    return client


def _add(client, lead, **data):
    return client.post(reverse("lead_people_add", args=[lead.pk]), data)


def test_adding_an_existing_contact(staff):
    lead, person = LeadFactory(), ContactFactory()

    resp = _add(staff, lead, role=LeadContact.Role.LEAD_PLANNER, contact=person.pk)

    assert resp.status_code == 200 and resp.json()["ok"]
    assert LeadContact.objects.filter(lead=lead, contact=person, role="lead_planner").exists()


def test_adding_a_typed_name_creates_the_contact_through_the_dedupe(staff):
    lead = LeadFactory()
    known = ContactFactory(name="Riley Coordinator", phone="+12025550188")

    _add(staff, lead, role=LeadContact.Role.EMERGENCY, contact="New Person", phone="202-555-0177")
    _add(staff, lead, role=LeadContact.Role.TRANSPORTATION, contact="Riley C", phone="2025550188")

    new = Contact.objects.get(name="New Person")
    assert new.phone == "+12025550177"
    assert LeadContact.objects.get(lead=lead, role="transportation").contact == known


def test_adding_the_day_of_coordinator_also_writes_the_old_columns(staff):
    lead, person = LeadFactory(), ContactFactory(name="Jordan Planner", phone="+12025550143")

    _add(staff, lead, role=DAY_OF, contact=person.pk)

    lead.refresh_from_db()
    assert (lead.day_of_contact_name, lead.day_of_contact_phone) == (
        "Jordan Planner",
        "+12025550143",
    )


@pytest.mark.parametrize(
    "data",
    [
        {"role": "florist", "contact": "1"},
        {"role": "couple", "contact": ""},
        {"role": "couple", "contact": "999999"},
    ],
)
def test_adding_refuses_bad_input(staff, data):
    resp = _add(staff, LeadFactory(), **data)

    assert resp.status_code == 400
    assert resp.json()["error"]
    assert not LeadContact.objects.exists()


def test_adding_the_same_person_twice_in_one_role_is_refused_politely(staff):
    lead, person = LeadFactory(), ContactFactory()
    _add(staff, lead, role="couple", contact=person.pk)

    resp = _add(staff, lead, role="couple", contact=person.pk)

    assert resp.status_code == 400
    assert "already" in resp.json()["error"]


def test_removing_a_role(staff):
    lead = LeadFactory()
    row = LeadContact.objects.create(lead=lead, contact=ContactFactory(), role="couple")

    resp = staff.post(reverse("lead_people_remove", args=[lead.pk, row.pk]))

    assert resp.status_code == 200
    assert not LeadContact.objects.exists()


def test_removing_the_day_of_coordinator_clears_the_old_columns(staff):
    lead = LeadFactory()
    contact_roles.set_day_of_contact(lead, name="Jordan Planner", phone="202-555-0143")
    row = LeadContact.objects.get(lead=lead)

    staff.post(reverse("lead_people_remove", args=[lead.pk, row.pk]))

    lead.refresh_from_db()
    assert (lead.day_of_contact_name, lead.day_of_contact_phone) == ("", "")


def test_a_role_on_another_order_cant_be_removed_through_this_one(staff):
    row = LeadContact.objects.create(lead=LeadFactory(), contact=ContactFactory(), role="couple")

    resp = staff.post(reverse("lead_people_remove", args=[LeadFactory().pk, row.pk]))

    assert resp.status_code == 404
    assert LeadContact.objects.filter(pk=row.pk).exists()


def test_the_endpoints_need_a_login(client):
    lead = LeadFactory()

    resp = client.post(reverse("lead_people_add", args=[lead.pk]), {"role": "couple"})

    assert resp.status_code == 302


def test_the_people_card_lists_roles_on_the_workspace_and_order_page(staff):
    lead, _trip = _wedding()
    LeadContact.objects.create(
        lead=lead, contact=ContactFactory(name="Avery Planner"), role="lead_planner"
    )

    for url in (reverse("lead_detail", args=[lead.pk]), reverse("order_detail", args=[lead.pk])):
        body = staff.get(url).content.decode()
        assert "People" in body, url
        assert "Avery Planner" in body and "Lead planner" in body, url
