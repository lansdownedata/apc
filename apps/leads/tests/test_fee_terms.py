"""APC-94 — the wedding cleaning-fee and overtime terms block and its acknowledgment stamp."""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.leads import services
from apps.leads.factories import LeadFactory
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
