import pytest
from django.core.signing import BadSignature

from apps.leads import services
from apps.leads.factories import LeadFactory

pytestmark = pytest.mark.django_db


def test_deposit_token_round_trips():
    lead = LeadFactory()
    token = services.make_deposit_token(lead)
    assert services.read_deposit_token(token).pk == lead.pk


def test_deposit_token_rejects_tampering():
    lead = LeadFactory()
    token = services.make_deposit_token(lead)
    with pytest.raises(BadSignature):
        services.read_deposit_token(token + "x")
