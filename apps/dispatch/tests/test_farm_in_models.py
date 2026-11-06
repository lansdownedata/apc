import pytest
from django.db import IntegrityError
from django.db.models import ProtectedError

from apps.contacts.factories import CompanyFactory
from apps.dispatch.factories import FarmInRequestFactory, FarmInStatusPushFactory
from apps.dispatch.models import FarmInRequest, FarmInStatusPush
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


def test_defaults():
    r = FarmInRequestFactory()
    assert r.desk == FarmInRequest.Desk.RESERVATIONS
    assert r.status == FarmInRequest.Status.PENDING
    assert r.quoted_amount is None and r.vehicle is None
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
