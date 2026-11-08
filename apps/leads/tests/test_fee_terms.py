"""APC-94/95 — the cleaning-fee (per vehicle type) and overtime terms block and its
acknowledgment stamp."""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead, VehicleType
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db


def _config(**fields):
    cfg = TaskConfig.load()
    for name, value in fields.items():
        setattr(cfg, name, value)
    cfg.save()
    return cfg


def _order(*fleet):
    """A booked order with one trip per (vehicle name, fee[, trip_status]) entry."""
    from apps.leads.factories import VehicleTypeFactory
    from apps.reservations.factories import ReservationFactory

    lead = LeadFactory(status=Lead.Status.BOOKED)
    for name, fee, *status in fleet:
        vehicle = VehicleTypeFactory(name=name)
        VehicleType.objects.filter(pk=vehicle.pk).update(cleaning_fee=Decimal(fee))
        extra = {"trip_status": status[0]} if status else {}
        ReservationFactory(lead=lead, vehicle=vehicle, **extra)
    return lead


def test_fee_block_lists_each_held_type_and_the_highest_hold():
    lead = _order(
        ("Test sprinter", "250"),
        ("Test coach", "500"),
        ("Test coach", "500"),  # a second bus is not a second line
        ("Test sedan", "0"),
        ("Test stretch", "750", "cancelled"),  # a cancelled trip neither lists nor raises the hold
    )

    terms = services.fee_terms_context(lead)
    html = services.fee_terms_html(lead)

    assert terms["fees"] == [("Test coach", Decimal("500")), ("Test sprinter", Decimal("250"))]
    assert terms["hold"] == Decimal("500")
    assert "Test coach — $500.00 cleaning fee" in html
    assert "Test sprinter — $250.00 cleaning fee" in html
    assert "Test sedan" not in html and "Test stretch" not in html
    assert html.index("Test coach") < html.index("Test sprinter")  # highest first
    assert "One hold for the highest of these, $500.00" in html
    assert "5-minute" in html and "15-minute" in html  # the overtime rule stays


def test_fee_block_without_held_vehicle_has_overtime_only():
    lead = _order(("Test sedan", "0"))

    terms = services.fee_terms_context(lead)
    html = services.fee_terms_html(lead)

    assert terms["fees"] == [] and terms["hold"] == Decimal("0")
    assert "cleaning fee" not in html.lower() and "hold" not in html.lower()
    assert "Overtime." in html and "5-minute" in html


def test_fee_terms_version_changes_with_the_fees_and_the_grace():
    lead = _order(("Test coach", "500"))
    base = services.fee_terms_version(lead, _config())

    assert base == services.fee_terms_version(lead, _config())
    VehicleType.objects.filter(name="Test coach").update(cleaning_fee=Decimal("450"))
    assert base != services.fee_terms_version(lead, _config())
    VehicleType.objects.filter(name="Test coach").update(cleaning_fee=Decimal("500"))
    assert base != services.fee_terms_version(lead, _config(grace_large_minutes=10))
    assert len(base) == 8


def test_acknowledge_fees_stamps_once():
    lead = _order(("Test coach", "250"))

    assert services.acknowledge_fees(lead, ip="203.0.113.9") is True
    lead.refresh_from_db()
    first = lead.fees_acknowledged_at
    assert first is not None and first <= timezone.now()
    assert lead.fees_acknowledged_ip == "203.0.113.9"
    assert lead.fees_acknowledged_version == services.fee_terms_version(lead)

    assert services.acknowledge_fees(lead, ip="198.51.100.1") is False
    lead.refresh_from_db()
    assert (lead.fees_acknowledged_at, lead.fees_acknowledged_ip) == (first, "203.0.113.9")


# --- where staff see it -------------------------------------------------------------------


def _wedding_lead(**fields):
    from apps.leads.factories import ServiceTypeFactory
    from apps.reservations.factories import ReservationFactory

    lead = LeadFactory(status=Lead.Status.BOOKED, **fields)
    ReservationFactory(lead=lead, service_type=ServiceTypeFactory(name="Wedding Transportation"))
    return lead


def test_lead_is_wedding_reads_its_trips():
    from apps.reservations.factories import ReservationFactory

    assert _wedding_lead().is_wedding is True
    plain = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=plain)
    assert plain.is_wedding is False


def test_the_money_card_shows_the_fee_acknowledgment_on_a_wedding(client):
    from django.urls import reverse

    from apps.accounts.factories import UserFactory

    client.force_login(UserFactory())
    pending = _wedding_lead()
    done = _wedding_lead(fees_acknowledged_at=timezone.now(), fees_acknowledged_version="feed1234")
    plain = LeadFactory(status=Lead.Status.BOOKED)

    pending_html = client.get(reverse("lead_detail", args=[pending.pk])).content.decode()
    done_html = client.get(reverse("lead_detail", args=[done.pk])).content.decode()
    plain_html = client.get(reverse("lead_detail", args=[plain.pk])).content.decode()

    assert "Cleaning fee and overtime not yet acknowledged" in pending_html
    assert "Cleaning fee and overtime acknowledged" in done_html and "vfeed1234" in done_html
    assert "Cleaning fee and overtime" not in plain_html
