import importlib

import pytest
from django.db import IntegrityError
from django.db.models import ProtectedError

from apps.contacts.factories import CompanyFactory
from apps.dispatch.factories import FarmInRequestFactory, FarmInStatusPushFactory
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.leads.factories import LeadFactory
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


def test_defaults():
    r = FarmInRequestFactory()
    assert r.desk == FarmInRequest.Desk.RESERVATIONS
    assert r.status == FarmInRequest.Status.PENDING
    assert r.reservation_id_token == f"FI-{r.pk}"


def test_transaction_id_unique():
    FarmInRequestFactory(transaction_id="tx-dup")
    with pytest.raises(IntegrityError):
        FarmInRequestFactory(transaction_id="tx-dup")


def test_company_protected():
    r = FarmInRequestFactory()
    with pytest.raises(ProtectedError):
        r.company.delete()


def test_push_defaults():
    p = FarmInStatusPushFactory(status="CONFIRMED")
    assert p.state == FarmInStatusPush.State.PENDING and p.attempts == 0


def test_notification_links_request_without_lead():
    r = FarmInRequestFactory()
    n = Notification.notify(None, Notification.Kind.FARM_IN_NEW, title="t", farm_in_request=r)
    assert n.lead_id is None and n.farm_in_request_id == r.pk
    assert n.url == ""  # farm_in_list ships in APC-136


def test_setting_grid_id_reparents_requests():
    auto = CompanyFactory(gnet_grid_id="acmelimo", is_affiliate_account=True)
    r = FarmInRequestFactory(company=auto, requester_grid_id="acmelimo")
    auto.gnet_grid_id = None
    auto.save()
    real = CompanyFactory(name="Acme Limo Inc", gnet_grid_id="acmelimo")
    r.refresh_from_db()
    assert r.company_id == real.pk


def test_envelope_requires_lead_and_reservation():
    company = CompanyFactory(is_affiliate_account=True)
    with pytest.raises(IntegrityError):
        FarmInRequest.objects.create(
            company=company, transaction_id="tx-bare", requester_grid_id="gnettest"
        )


def test_reservation_is_one_to_one():
    first = FarmInRequestFactory()
    with pytest.raises(IntegrityError):
        FarmInRequestFactory(
            company=first.company,
            lead=first.lead,
            reservation=first.reservation,
            transaction_id="tx-second",
        )


def test_lead_farm_in_property():
    ordinary = LeadFactory()
    assert ordinary.farm_in is None
    envelope = FarmInRequestFactory()
    assert envelope.lead.farm_in == envelope
    assert envelope.lead.contact.company_id == envelope.company_id
    assert envelope.reservation.lead_id == envelope.lead_id


def test_deleting_a_gnet_lead_is_refused():
    envelope = FarmInRequestFactory()
    with pytest.raises(ProtectedError):
        envelope.lead.delete()


def test_deleting_a_gnet_reservation_is_refused():
    envelope = FarmInRequestFactory()
    with pytest.raises(ProtectedError):
        envelope.reservation.delete()


@pytest.mark.parametrize(
    "name",
    [
        "vehicle",
        "quoted_amount",
        "pricing_source",
        "priced_by",
        "pricing_note",
        "timezone_assumed",
        "pickup_summary",
        "dropoff_summary",
        "passenger_name",
        "passenger_count",
        "PricingSource",
    ],
)
def test_removed_fields_are_gone(name):
    assert hasattr(FarmInRequest, name) is False


def test_envelope_migration_alters_before_it_removes():
    migration = importlib.import_module("apps.dispatch.migrations.0014_farminrequest_envelope")
    names = [type(op).__name__ for op in migration.Migration.operations]
    assert names[:4] == ["RunPython", "AlterField", "AlterField", "RemoveIndex"]
