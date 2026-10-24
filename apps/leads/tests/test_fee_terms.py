"""APC-94 — the wedding cleaning-fee and overtime terms block and its acknowledgment stamp."""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.leads import services
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.tasks.models import TaskConfig

pytestmark = pytest.mark.django_db


def _config(**fields):
    cfg = TaskConfig.load()
    for name, value in fields.items():
        setattr(cfg, name, value)
    cfg.save()
    return cfg


def test_fee_terms_version_changes_with_the_fee_and_the_grace():
    base = services.fee_terms_version(_config(cleaning_fee=Decimal("250")))

    assert base == services.fee_terms_version(_config(cleaning_fee=Decimal("250")))
    assert base != services.fee_terms_version(_config(cleaning_fee=Decimal("300")))
    assert base != services.fee_terms_version(
        _config(cleaning_fee=Decimal("250"), grace_large_minutes=10)
    )
    assert len(base) == 8


def test_fee_terms_hide_the_cleaning_line_when_the_fee_is_zero():
    html = services.fee_terms_html(_config(cleaning_fee=Decimal("0")))
    assert "cleaning fee" not in html.lower()
    assert "hour" in html  # the overtime rule always shows

    html = services.fee_terms_html(_config(cleaning_fee=Decimal("250")))
    assert "$250.00" in html
    assert "5-minute" in html and "15-minute" in html  # the rule table's numbers, not literals


def test_acknowledge_fees_stamps_once():
    _config(cleaning_fee=Decimal("250"))
    lead = LeadFactory()

    assert services.acknowledge_fees(lead, ip="203.0.113.9") is True
    lead.refresh_from_db()
    first = lead.fees_acknowledged_at
    assert first is not None and first <= timezone.now()
    assert lead.fees_acknowledged_ip == "203.0.113.9"
    assert lead.fees_acknowledged_version == services.fee_terms_version()

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
