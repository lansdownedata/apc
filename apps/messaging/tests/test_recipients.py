import pytest

from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory
from apps.leads.models import LeadContact
from apps.messaging.factories import MessageRowFactory
from apps.messaging.recipients import addresses, resolve
from apps.planners.models import PlannerAccount
from apps.reservations.factories import PassengerFactory, ReservationFactory
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead():
    return LeadFactory(contact=ContactFactory(name="Bea Booker", phone="(202) 555-0100"))


@pytest.fixture
def trip(lead):
    return ReservationFactory(lead=lead)


def _row(*keys, **kw):
    return MessageRowFactory(recipients=list(keys), **kw)


def test_booking_contact(lead, trip):
    [r] = resolve(_row("booking_contact"), lead, trip)
    assert (r.name, r.phone, r.email, r.contact_id) == (
        "Bea Booker",
        "+12025550100",
        lead.contact.email,
        lead.contact_id,
    )
    assert r.label == "Bea Booker (booking contact)"


def test_billing_contact_uses_effective_billing_contact(lead, trip):
    payer = ContactFactory(name="Pat Payer", phone="2025550111")
    lead.billing_contact = payer
    lead.save()
    [r] = resolve(_row("billing_contact"), lead, trip)
    assert r.contact_id == payer.pk and r.phone == "+12025550111"


def test_role_resolves_each_holder(lead, trip):
    a, b = ContactFactory(phone="2025550121"), ContactFactory(phone="2025550122")
    LeadContact.objects.create(lead=lead, contact=a, role="day_of_coordinator")
    LeadContact.objects.create(lead=lead, contact=b, role="day_of_coordinator")
    LeadContact.objects.create(lead=lead, contact=ContactFactory(), role="emergency")
    got = resolve(_row("role:day_of_coordinator"), lead, trip)
    assert [r.contact_id for r in got] == [a.pk, b.pk]
    assert got[0].label.endswith("(Day-of coordinator)")


def test_same_person_two_roles_gets_one_copy(lead, trip):
    person = ContactFactory(phone="(202) 555-0147")
    twin = ContactFactory(phone="+12025550147")
    lead.contact = person
    lead.save()
    LeadContact.objects.create(lead=lead, contact=person, role="day_of_coordinator")
    LeadContact.objects.create(lead=lead, contact=twin, role="transportation")
    row = _row("booking_contact", "role:day_of_coordinator", "role:transportation")
    got = resolve(row, lead, trip)
    assert len(got) == 3
    pairs = addresses(got, "sms")
    assert [a for _, a in pairs] == ["+12025550147"]
    assert pairs[0][0].label.endswith("(booking contact)")


def test_email_dedupe_is_case_insensitive(lead, trip):
    other = ContactFactory(email=None)
    LeadContact.objects.create(lead=lead, contact=other, role="couple")
    Rec = type(resolve(_row("booking_contact"), lead, trip)[0])
    a = Rec("A", "A (x)", "", "Same@Example.com", None)
    b = Rec("B", "B (y)", "", "same@example.com", None)
    assert addresses([a, b], "email") == [(a, "same@example.com")]


def test_passengers_lead_only(lead, trip):
    PassengerFactory(reservation=trip, name="Other", mobile="+12025550133")
    PassengerFactory(reservation=trip, name="Dana Whitfield", mobile="+12025550134", is_lead=True)
    [r] = resolve(_row("passengers"), lead, trip)
    assert r.name == "Dana Whitfield"
    assert r.label == "Dana Whitfield (lead passenger)"


def test_passengers_everyone_with_a_mobile_when_no_lead(lead, trip):
    PassengerFactory(reservation=trip, name="A", mobile="+12025550133")
    PassengerFactory(reservation=trip, name="B", mobile="")
    PassengerFactory(reservation=trip, name="C", mobile="+12025550135")
    got = resolve(_row("passengers"), lead, trip)
    assert [r.name for r in got] == ["A", "C"]


def test_passengers_fallback_to_booking_contact(lead, trip):
    PassengerFactory(reservation=trip, name="B", mobile="")
    [r] = resolve(_row("passengers"), lead, trip)
    assert r.contact_id == lead.contact_id


def test_passengers_without_a_trip_fall_back_to_booking_contact(lead):
    [r] = resolve(_row("passengers"), lead, None)
    assert r.contact_id == lead.contact_id


def test_driver_and_passengers_have_no_email_address(lead, trip):
    PassengerFactory(reservation=trip, name="A", mobile="+12025550133")
    AssignmentFactory(
        reservation=trip,
        status=Assignment.Status.CONFIRMED,
        driver_name="Dave Driver",
        driver_cell="(202) 555-0166",
    )
    got = resolve(_row("passengers", "driver"), lead, trip)
    assert [r.email for r in got] == ["", ""]
    assert addresses(got, "email") == []
    assert [a for _, a in addresses(got, "sms")] == ["+12025550133", "+12025550166"]
    assert got[1].name == "Dave Driver"


def test_driver_needs_a_confirmed_assignment(lead, trip):
    AssignmentFactory(reservation=trip, driver_name="Dave", driver_cell="2025550166")
    assert resolve(_row("driver"), lead, trip) == []


def test_company_is_the_assigned_vendor(lead, trip):
    vendor = VendorFactory(name="Acme Coach", phone="(202) 555-0177", email="ops@acme.test")
    AssignmentFactory(reservation=trip, vendor=vendor, status=Assignment.Status.CONFIRMED)
    [r] = resolve(_row("company", audience="affiliate"), lead, trip)
    assert (r.name, r.phone, r.email) == ("Acme Coach", "+12025550177", "ops@acme.test")
    assert r.contact_id is None


def test_company_with_no_coverage_resolves_nobody(lead, trip):
    assert resolve(_row("company"), lead, trip) == []


def test_planner_is_the_registered_account_email(lead, trip):
    planner = ContactFactory(name="Wendy Planner")
    PlannerAccount.objects.create(contact=planner, email="wendy@plans.test")
    LeadContact.objects.create(lead=lead, contact=planner, role="lead_planner")
    LeadContact.objects.create(lead=lead, contact=ContactFactory(), role="lead_planner")
    [r] = resolve(_row("planner", audience="planner"), lead, trip)
    assert (r.name, r.email, r.phone) == ("Wendy Planner", "wendy@plans.test", "")


def test_revoked_planner_account_is_skipped(lead, trip):
    from django.utils import timezone

    planner = ContactFactory()
    PlannerAccount.objects.create(contact=planner, email="w@plans.test", revoked_at=timezone.now())
    LeadContact.objects.create(lead=lead, contact=planner, role="lead_planner")
    assert resolve(_row("planner"), lead, trip) == []


def test_staff_audience_uses_staff_emails(lead, trip):
    row = _row(audience="staff", staff_emails=["Ops@apc.test", "ceo@apc.test"])
    got = resolve(row, lead, trip)
    assert [a for _, a in addresses(got, "email")] == ["ops@apc.test", "ceo@apc.test"]
    assert addresses(got, "sms") == []


def test_missing_address_is_returned_but_has_no_address(lead, trip):
    lead.contact.phone = ""
    lead.contact.save()
    got = resolve(_row("booking_contact"), lead, trip)
    assert len(got) == 1 and got[0].phone == ""
    assert addresses(got, "sms") == []


def test_unparseable_phone_is_kept_as_typed(lead, trip):
    lead.contact.phone = " 555 "
    lead.contact.save()
    [r] = resolve(_row("booking_contact"), lead, trip)
    assert r.phone == "555"


def test_unknown_key_is_ignored(lead, trip):
    assert resolve(_row("nonsense"), lead, trip) == []


@pytest.fixture
def operator_lead():
    company = CompanyFactory(is_affiliate_account=True)
    boss = ContactFactory(name="Olga Operator", company=company, phone="2025550188")
    company.billing_contact = ContactFactory(name="Biller")
    company.save()
    return LeadFactory(contact=boss)


def test_farm_in_sends_to_operator_only(operator_lead):
    trip = ReservationFactory(lead=operator_lead)
    LeadContact.objects.create(lead=operator_lead, contact=ContactFactory(), role="couple")
    PassengerFactory(reservation=trip, mobile="+12025550133", is_lead=True)
    row = _row("booking_contact", "billing_contact", "role:couple", "passengers", "planner")
    got = resolve(row, operator_lead, trip)
    assert [r.contact_id for r in got] == [operator_lead.contact_id]
    assert len(addresses(got, "sms")) == 1


def test_farm_in_wedding_billing_contact_is_the_operator(operator_lead):
    operator_lead.wedding_name = "Smith wedding"
    operator_lead.save()
    trip = ReservationFactory(lead=operator_lead)
    [r] = resolve(_row("billing_contact"), operator_lead, trip)
    assert r.contact_id == operator_lead.contact_id


def test_farm_in_leaves_vendor_and_driver_alone(operator_lead):
    trip = ReservationFactory(lead=operator_lead)
    vendor = VendorFactory(phone="2025550177")
    AssignmentFactory(
        reservation=trip,
        vendor=vendor,
        status=Assignment.Status.CONFIRMED,
        driver_name="D",
        driver_cell="2025550166",
    )
    got = resolve(_row("company", "driver"), operator_lead, trip)
    assert len(got) == 2


def test_farm_in_staff_rows_unchanged(operator_lead):
    got = resolve(_row(audience="staff", staff_emails=["a@b.test"]), operator_lead, None)
    assert [r.email for r in got] == ["a@b.test"]
