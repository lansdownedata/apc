"""The registry (spec §3.1, §3.5, §4.2): what each operational model logs, and where."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db.models import Model
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.models import User
from apps.audit import registry
from apps.audit.display import money
from apps.audit.models import AuditEntry
from apps.billing.factories import (
    AccountGroupFactory,
    BillingAccountFactory,
    InvoiceFactory,
    InvoiceLineFactory,
)
from apps.billing.models import AccountGroup, BillingAccount, Invoice, InvoiceLine, SyncState
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.contacts.models import Company, Contact, ContactPhone
from apps.dispatch.factories import AssignmentFactory
from apps.dispatch.models import Assignment
from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.leads.models import Lead, LeadContact
from apps.payments.factories import ChargeFactory, PaymentPlanFactory
from apps.payments.models import AffiliatePayable, Charge, PaymentPlan
from apps.planners.models import PlannerAccount, PlannerRequest
from apps.reservations.factories import ReservationFactory, TripExtraFactory
from apps.reservations.models import Reservation, TripExtra
from apps.vendors.factories import (
    VendorDocumentFactory,
    VendorDriverFactory,
    VendorFactory,
    VendorInsuranceFactory,
)
from apps.vendors.models import Vendor, VendorDocument, VendorDriver, VendorInsurance

pytestmark = pytest.mark.django_db


def _entries(instance: Model, action: int | None = None):
    """The row's own entries: by content type too, since pks repeat across tables."""
    ct = ContentType.objects.get_for_model(type(instance))
    entries = AuditEntry.objects.filter(content_type=ct, object_id=instance.pk)
    return entries if action is None else entries.filter(action=action)


def _lead_contact() -> LeadContact:
    return LeadContact.objects.create(
        lead=LeadFactory(), contact=ContactFactory(), role=LeadContact.Role.COUPLE
    )


def _payable() -> AffiliatePayable:
    return AffiliatePayable.objects.create(assignment=AssignmentFactory(), expected_amount=350)


def _planner_request() -> PlannerRequest:
    return PlannerRequest.objects.create(
        account=PlannerAccount.objects.create(contact=ContactFactory(), email="w@plans.test"),
        lead=LeadFactory(),
        about="Saturday run",
    )


def _phone() -> ContactPhone:
    return ContactPhone.objects.create(contact=ContactFactory(phone=""), number="+12025550134")


@dataclass(frozen=True)
class Case:
    """One registered model: how to make a row, one tracked change, one excluded change.

    An excluded value of `None` means the model has no exclusions of its own beyond the
    timestamps, so the excluded case is a save that changes nothing but `updated_at`.
    """

    make: Callable[[], Model]
    tracked: tuple[str, Any]
    excluded: tuple[str, Any] | None


CASES: dict[str, Case] = {
    "accounts.User": Case(UserFactory, ("phone", "+15715550142"), ("last_login", timezone.now)),
    "reservations.TripExtra": Case(
        TripExtraFactory, ("label", "Parking"), ("created_by", UserFactory)
    ),
    "dispatch.Assignment": Case(
        AssignmentFactory, ("note", "Call on arrival"), ("gnet_transaction_id", "tx-9")
    ),
    "leads.Lead": Case(LeadFactory, ("notes", "Prefers texts"), ("quote_view_count", 3)),
    "leads.LeadContact": Case(_lead_contact, ("role", LeadContact.Role.LEAD_PLANNER), None),
    "payments.PaymentPlan": Case(
        PaymentPlanFactory,
        ("deposit_status", PaymentPlan.DepositStatus.REQUESTED),
        ("stripe_customer_id", "cus_123"),
    ),
    "contacts.Contact": Case(ContactFactory, ("notes", "VIP"), ("podium_contact_uid", "pod-1")),
    "contacts.ContactPhone": Case(_phone, ("label", ContactPhone.Label.WORK), None),
    "contacts.Company": Case(CompanyFactory, ("notes", "Net 30 only"), None),
    "billing.BillingAccount": Case(
        BillingAccountFactory, ("name", "Ridgeline Partners"), ("qbo_id", "77")
    ),
    "billing.AccountGroup": Case(
        AccountGroupFactory, ("po_required", True), ("qbo_sync_state", SyncState.SYNCED)
    ),
    "vendors.Vendor": Case(VendorFactory, ("status", Vendor.Status.INACTIVE), ("qbo_id", "42")),
    "vendors.VendorDriver": Case(VendorDriverFactory, ("phone", "+12025550199"), None),
    "vendors.VendorInsurance": Case(VendorInsuranceFactory, ("insurer", "Beta Mutual"), None),
    "vendors.VendorDocument": Case(
        VendorDocumentFactory, ("label", "COI 2027"), ("uploaded_by", UserFactory)
    ),
    "payments.Charge": Case(
        ChargeFactory, ("note", "Late fee"), ("stripe_client_secret", "pi_x_secret_y")
    ),
    "payments.AffiliatePayable": Case(
        _payable, ("invoice_number", "INV-7"), ("approved_at", timezone.now)
    ),
    "billing.Invoice": Case(InvoiceFactory, ("po_number", "PO-9"), ("edit_note", "Changed PO")),
    "billing.InvoiceLine": Case(InvoiceLineFactory, ("description", "Airport run"), None),
    "planners.PlannerRequest": Case(_planner_request, ("about", "Sunday run"), None),
}


def _value(value: Any) -> Any:
    """A factory in the table stands for a fresh related row."""
    return value() if callable(value) and not isinstance(value, str) else value


def test_registered_models_are_exactly_the_spec_list():
    registered = {spec.model for spec in registry.SPECS}

    assert registered == {"reservations.Reservation", *CASES}


@pytest.mark.parametrize("model", sorted(CASES))
def test_tracked_change_writes_one_entry(model):
    case = CASES[model]
    instance = case.make()
    name, value = case.tracked
    setattr(instance, name, _value(value))
    instance.save()

    [entry] = _entries(instance, AuditEntry.Action.UPDATE)
    assert set(entry.changes) == {name}


@pytest.mark.parametrize("model", sorted(CASES))
def test_excluded_change_writes_nothing(model):
    case = CASES[model]
    instance = case.make()
    if case.excluded is not None:
        name, value = case.excluded
        assert name in registry.effective_exclude(instance)
        setattr(instance, name, _value(value))
    instance.save()

    assert not _entries(instance, AuditEntry.Action.UPDATE).exists()


def test_qbo_fields_are_read_from_the_model():
    names = registry.qbo_fields(Vendor)

    assert {"qbo_id", "qbo_sync_token", "qbo_push_started_at"} <= set(names)
    assert all(n.startswith("qbo_") for n in names)
    assert set(names) <= set(registry.effective_exclude(Vendor))
    assert set(registry.qbo_fields(AccountGroup)) <= set(registry.effective_exclude(AccountGroup))
    assert registry.qbo_fields(Lead) == ()


def test_every_excluded_name_is_a_real_column():
    for spec in registry.SPECS:
        model = registry.model_of(spec)
        columns = {f.name for f in model._meta.concrete_fields}
        assert set(registry.effective_exclude(model)) <= columns, spec.model


def _parents(instance: Model) -> dict:
    [entry] = _entries(instance, AuditEntry.Action.CREATE)
    return {"trip_id": entry.trip_id, "order_id": entry.order_id, "profile": entry.profile}


def test_parent_columns():
    extra = TripExtraFactory()
    assert _parents(extra) == {
        "trip_id": extra.reservation_id,
        "order_id": extra.reservation.lead_id,
        "profile": "",
    }

    phone = _phone()
    assert _parents(phone)["profile"] == f"contact:{phone.contact_id}"

    group = AccountGroupFactory()
    assert _parents(group)["profile"] == f"contact:{group.account.contact_id}"

    account = BillingAccountFactory()
    assert _parents(account)["profile"] == f"contact:{account.contact_id}"

    driver = VendorDriverFactory()
    assert _parents(driver)["profile"] == f"vendor:{driver.vendor_id}"

    company = CompanyFactory()
    assert _parents(company)["profile"] == f"company:{company.pk}"

    contact = ContactFactory()
    assert _parents(contact)["profile"] == f"contact:{contact.pk}"

    lead = LeadFactory()
    assert _parents(lead) == {"trip_id": None, "order_id": lead.pk, "profile": ""}

    assignment = AssignmentFactory()
    assert _parents(assignment) == {
        "trip_id": assignment.reservation_id,
        "order_id": assignment.reservation.lead_id,
        "profile": "",
    }

    plan = PaymentPlanFactory()
    assert _parents(plan)["order_id"] == plan.lead_id

    people = _lead_contact()
    assert _parents(people)["order_id"] == people.lead_id


def test_two_hop_parents_read_one_column_when_the_parent_is_not_loaded(
    django_assert_num_queries,
):
    extra = TripExtraFactory()
    fresh = TripExtra.objects.get(pk=extra.pk)
    with django_assert_num_queries(1):
        parents = registry.parents(fresh)
    assert parents == {"trip_id": extra.reservation_id, "order_id": extra.reservation.lead_id}

    group = AccountGroupFactory()
    fresh_group = AccountGroup.objects.get(pk=group.pk)
    with django_assert_num_queries(1):
        assert registry.parents(fresh_group) == {"profile": f"contact:{group.account.contact_id}"}

    with django_assert_num_queries(0):
        registry.parents(group)  # its account is cached


def test_vendor_vehicle_types_m2m_logged():
    vendor = VendorFactory()
    sedan = VehicleTypeFactory()

    vendor.vehicle_types.add(sedan)
    vendor.vehicle_types.remove(sedan)

    added, removed = _entries(vendor, AuditEntry.Action.UPDATE).order_by("pk")
    assert added.changes["vehicle_types"]["operation"] == "add"
    assert removed.changes["vehicle_types"]["operation"] == "delete"
    assert added.profile == removed.profile == f"vendor:{vendor.pk}"


def test_lead_save_repr_costs_no_query(django_assert_num_queries):
    lead = LeadFactory()
    ContentType.objects.get_for_model(Lead)  # cached for the process; warm it here
    fresh = Lead.objects.get(pk=lead.pk)
    fresh.notes = "Prefers texts"

    # The UPDATE, auditlog's read of the old row, and the entry's INSERT. No contact read:
    # Lead.__str__ would load it.
    with django_assert_num_queries(3):
        fresh.save()

    [entry] = _entries(lead, AuditEntry.Action.UPDATE)
    assert entry.object_repr == lead.quote_no


def test_labels_are_cheap_and_name_the_row():
    assert registry.label(ContactFactory(name="Ada Ruiz")) == "Ada Ruiz"
    assert registry.label(CompanyFactory(name="Ridgeline")) == "Ridgeline"
    assert registry.label(VendorFactory(name="Capital Cars")) == "Capital Cars"
    lead = LeadFactory()
    assert registry.label(lead) == lead.quote_no
    extra = TripExtraFactory(kind=TripExtra.Kind.TOLLS, label="Tolls", amount=12)
    assert registry.label(extra) == "Tolls $12.00"


def test_who_when_columns_never_tracked():
    user = UserFactory()
    extra = TripExtraFactory(created_by=user)
    document = VendorDocumentFactory(uploaded_by=user)
    assignment = AssignmentFactory(
        insurance_override="expired",
        insurance_override_by=user,
        insurance_override_at=timezone.now(),
    )

    assert "created_by" not in _entries(extra, AuditEntry.Action.CREATE).get().changes
    assert "uploaded_by" not in _entries(document, AuditEntry.Action.CREATE).get().changes
    changes = _entries(assignment, AuditEntry.Action.CREATE).get().changes
    assert not {"insurance_override_by", "insurance_override_at"} & set(changes)
    assert "insurance_override" in changes


def test_sentences_name_the_customer_and_affiliate_actions():
    lead = registry.spec_for(Lead).sentences
    assert lead["accepted_terms_at"] == "Accepted the terms (version {accepted_terms_version})"
    assert lead["fees_acknowledged_at"] == "Acknowledged the cleaning fee and overtime terms"
    assert registry.spec_for(Reservation).sentences == {
        "customer_confirmed_at": "Confirmed the trip"
    }
    assert registry.spec_for(Assignment).sentences == {
        "affiliate_confirmed_at": "Confirmed the trip"
    }


def test_field_label_uses_the_map_then_the_verbose_name():
    assert registry.field_label(Reservation, "hours") == "Override hours"
    assert registry.field_label(Reservation, "trip_status") == "Status"
    assert registry.field_label(Lead, "assigned_agent") == "Agent"
    assert registry.field_label(Lead, "accepted_terms_at") == "Terms accepted"
    assert registry.field_label(Assignment, "affiliate_confirmed_at") == "Affiliate confirmed"
    assert registry.field_label(Contact, "la_account_id") == "LimoAnywhere account"
    assert registry.field_label(Vendor, "usdot_number") == "USDOT number"
    assert registry.field_label(Lead, "lost_reason") == "Lost reason"


MINIMAL: dict[str, Callable[[], Model]] = {
    "accounts.User": User,
    "reservations.Reservation": Reservation,
    "reservations.TripExtra": TripExtra,
    "dispatch.Assignment": Assignment,
    "leads.Lead": Lead,
    "leads.LeadContact": LeadContact,
    "payments.PaymentPlan": PaymentPlan,
    "contacts.Contact": Contact,
    "contacts.ContactPhone": ContactPhone,
    "contacts.Company": Company,
    "billing.BillingAccount": BillingAccount,
    "billing.AccountGroup": AccountGroup,
    "vendors.Vendor": Vendor,
    "vendors.VendorDriver": VendorDriver,
    "vendors.VendorInsurance": VendorInsurance,
    "vendors.VendorDocument": VendorDocument,
    "payments.Charge": Charge,
    "payments.AffiliatePayable": AffiliatePayable,
    "billing.Invoice": Invoice,
    "billing.InvoiceLine": InvoiceLine,
    "planners.PlannerRequest": PlannerRequest,
}


@pytest.mark.parametrize("model", sorted(MINIMAL))
def test_parents_and_label_never_raise_on_minimal_instance(model):
    instance = MINIMAL[model]()

    assert isinstance(registry.parents(instance), dict)
    assert isinstance(registry.label(instance), str)


def test_payable_with_a_cached_empty_assignment_has_no_parents():
    assert registry.parents(AffiliatePayable(assignment=None)) == {}


def test_two_hop_parents_survive_a_deleted_parent():
    """A child whose parent row is gone (a cascade delete logging its children) files the
    entry under what it still knows, without raising."""
    orphan_extra = TripExtra(reservation_id=ReservationFactory().pk + 10_000)
    assert registry.parents(orphan_extra) == {"trip_id": orphan_extra.reservation_id}

    orphan_group = AccountGroup(account_id=BillingAccountFactory().pk + 10_000)
    assert registry.parents(orphan_group) == {}


def test_money_formats_dollars_with_separators():
    assert money(Decimal("1250")) == "$1,250.00"
    assert money("450") == "$450.00"
    assert money(None) == "$0.00"


def test_money_parents_and_labels():
    deposit = ChargeFactory(kind=Charge.Kind.DEPOSIT, amount=Decimal("450"))
    assert _parents(deposit) == {
        "trip_id": None,
        "order_id": deposit.plan.lead_id,
        "profile": "",
    }
    assert registry.label(deposit) == "Deposit $450.00"

    reservation = ReservationFactory()
    trip = ChargeFactory(
        plan=PaymentPlanFactory(lead=reservation.lead),
        kind=Charge.Kind.TRIP,
        reservation=reservation,
        amount=Decimal("1250"),
    )
    assert _parents(trip) == {
        "trip_id": reservation.pk,
        "order_id": reservation.lead_id,
        "profile": "",
    }
    assert registry.label(trip) == "Trip charge $1,250.00"

    payable = _payable()
    assignment = payable.assignment
    assert _parents(payable) == {
        "trip_id": assignment.reservation_id,
        "order_id": assignment.reservation.lead_id,
        "profile": f"vendor:{assignment.vendor_id}",
    }
    assert registry.label(payable) == "Vendor bill"

    invoice = InvoiceFactory()
    assert _parents(invoice) == {
        "trip_id": None,
        "order_id": None,
        "profile": f"contact:{invoice.group.account.contact_id}",
    }
    assert registry.label(invoice) == f"Invoice (draft #{invoice.pk})"

    line = InvoiceLineFactory(invoice=invoice, amount=Decimal("450"))
    assert _parents(line) == {
        "trip_id": line.reservation_id,
        "order_id": line.reservation.lead_id,
        "profile": f"contact:{invoice.group.account.contact_id}",
    }
    assert registry.label(line) == "Invoice line $450.00"

    request = _planner_request()
    assert _parents(request) == {"trip_id": None, "order_id": request.lead_id, "profile": ""}
    assert registry.label(request) == "Planner request: Saturday run"


def test_money_parents_read_one_query_per_hop_when_not_loaded(django_assert_num_queries):
    made = ChargeFactory()
    charge = Charge.objects.get(pk=made.pk)
    with django_assert_num_queries(1):
        assert registry.parents(charge) == {"order_id": made.plan.lead_id}

    payable = AffiliatePayable.objects.get(pk=_payable().pk)
    with django_assert_num_queries(1):
        assert set(registry.parents(payable)) == {"trip_id", "order_id", "profile"}

    invoice = Invoice.objects.get(pk=InvoiceFactory().pk)
    with django_assert_num_queries(1):
        assert set(registry.parents(invoice)) == {"profile"}

    line = InvoiceLine.objects.get(pk=InvoiceLineFactory().pk)
    with django_assert_num_queries(2):
        assert set(registry.parents(line)) == {"trip_id", "order_id", "profile"}


def test_invoice_who_when_not_tracked():
    user = UserFactory()
    invoice = InvoiceFactory(created_by=user, number="1042")
    assert not {"created_by", "number"} & set(_entries(invoice, 0).get().changes)

    invoice.voided_by = user
    invoice.voided_at = timezone.now()
    invoice.edited_by = user
    invoice.edited_at = timezone.now()
    invoice.edit_note = "x"
    invoice.number = "1043"
    invoice.void_reason = "Wrong trip"
    invoice.save()

    [entry] = _entries(invoice, AuditEntry.Action.UPDATE)
    assert set(entry.changes) == {"void_reason"}


def test_payable_who_when_not_tracked():
    payable = _payable()
    payable.status = AffiliatePayable.Status.APPROVED
    payable.approved_by = UserFactory()
    payable.approved_at = timezone.now()
    payable.paid_at = timezone.now()
    payable.invoice_received_at = timezone.now()
    payable.save()

    [entry] = _entries(payable, AuditEntry.Action.UPDATE)
    assert set(entry.changes) == {"status"}


def test_no_secret_or_integration_field_tracked():
    pattern = re.compile(r"secret|token|password", re.IGNORECASE)
    for spec in registry.SPECS:
        model = registry.model_of(spec)
        excluded = set(registry.effective_exclude(model))
        for column in model._meta.concrete_fields:
            risky = (
                pattern.search(column.name)
                or column.name.startswith(("stripe_", "qbo_"))
                or column.name == "idempotency_key"
            )
            if risky:
                assert column.name in excluded, f"{spec.model}.{column.name} is tracked"
    assert "stripe_client_secret" in registry.effective_exclude(Charge)


def test_secret_never_in_changes():
    charge = ChargeFactory(stripe_client_secret="pi_x_secret_y")
    charge.stripe_client_secret = "pi_z_secret_w"
    charge.note = "Changed"
    charge.save()

    entries = _entries(charge)
    assert entries.count() == 2
    for entry in entries:
        assert "secret" not in json.dumps(entry.changes).lower()


def test_planner_request_sentence():
    assert registry.spec_for(PlannerRequest).sentences == {"create": 'Planner request: "{about}"'}


def test_charge_plan_is_not_tracked():
    assert "plan" in registry.effective_exclude(Charge)


def test_owner_address_foreign_keys_are_not_tracked():
    """track_address logs the address on its owner (Ruling 26); the FK itself would render
    through the Address's current text, contradicting those lines after a later edit."""
    assert {"primary_address", "billing_address"} <= set(registry.effective_exclude(Contact))
    assert "address" in registry.effective_exclude(Vendor)
