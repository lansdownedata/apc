"""The History entry points on existing pages, and the import seam (APC-140, spec §6.1, §9)."""

from datetime import date, time

import pytest
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.audit.context import audit_context
from apps.audit.middleware import AuditMiddleware
from apps.audit.models import AuditEntry
from apps.audit.tests.test_views import LA, NY, _get, _quiet_trip
from apps.audit.views import _order_panel
from apps.contacts.factories import CompanyFactory, ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.reservations.factories import ReservationFactory, TripExtraFactory
from apps.reservations.models import Reservation
from apps.vendors.factories import VendorFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(client):
    client.force_login(UserFactory(is_superuser=True))
    return client


def _booked_trip(**kwargs) -> Reservation:
    kwargs.setdefault("lead", LeadFactory(status=Lead.Status.BOOKED))
    return ReservationFactory(**kwargs)


def _body(client, url: str) -> str:
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


def _opens(url: str, title: str) -> str:
    return f"$dispatch('drawer-open', {{ url: '{url}', title: '{title}' }})"


@pytest.mark.parametrize("status", [Lead.Status.NEW, Lead.Status.BOOKED])
def test_quote_workspace_links_the_order_history(staff, status):
    lead = LeadFactory(status=status)
    ReservationFactory(lead=lead)

    body = _body(staff, reverse("lead_detail", args=[lead.pk]))

    assert _opens(reverse("audit:order", args=[lead.pk]), "Order history") in body
    assert body.count("data-drawer") == 1


def test_quote_workspace_trip_line_links_the_trip_history(staff):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(lead=lead)

    body = _body(staff, reverse("lead_detail", args=[lead.pk]))

    assert _opens(reverse("audit:trip", args=[trip.pk]), "Trip history") in body
    assert 'aria-label="Trip history"' in body


def test_order_page_links_order_and_trip_history_with_one_drawer(staff):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    trip = ReservationFactory(lead=lead)

    body = _body(staff, reverse("order_detail", args=[lead.pk]))

    assert _opens(reverse("audit:order", args=[lead.pk]), "Order history") in body
    assert _opens(reverse("audit:trip", args=[trip.pk]), "Trip history") in body
    assert body.count("data-drawer") == 1


def test_processing_pane_links_the_full_trip_history():
    trip = ReservationFactory()
    html = render_to_string("processing/_history.html", {"trip": trip, "events": []})

    assert _opens(reverse("audit:trip", args=[trip.pk]), "Trip history") in html
    assert "Full history" in html


def test_processing_page_includes_the_drawer_once(staff):
    body = _body(staff, reverse("processing"))

    assert body.count("data-drawer") == 1


def test_dispatch_assign_panel_links_the_trip_history(staff):
    trip = _booked_trip(pickup_date=date(2026, 8, 26), pickup_time=time(6, 15))

    body = _body(staff, reverse("dispatch_assign_panel", args=[trip.pk]))

    assert _opens(reverse("audit:trip", args=[trip.pk]), "Trip history") in body


def test_contact_page_links_history_with_one_drawer(staff):
    contact = ContactFactory()

    body = _body(staff, reverse("contact_detail", args=[contact.pk]))

    assert _opens(reverse("audit:profile", args=["contact", contact.pk]), "Contact history") in body
    assert body.count("data-drawer") == 1


def test_company_page_links_history_with_one_drawer(staff):
    company = CompanyFactory()

    body = _body(staff, reverse("company_detail", args=[company.pk]))

    assert _opens(reverse("audit:profile", args=["company", company.pk]), "Company history") in body
    assert body.count("data-drawer") == 1


def test_vendor_page_links_history_with_one_drawer(staff):
    vendor = VendorFactory()

    body = _body(staff, reverse("vendor_detail", args=[vendor.pk]))

    assert _opens(reverse("audit:profile", args=["vendor", vendor.pk]), "Vendor history") in body
    assert body.count("data-drawer") == 1


def test_drawer_failure_toast_names_the_title_only_when_one_was_passed():
    js = (settings.BASE_DIR / "static/js/app.js").read_text()

    assert 'title ? `Could not load ${title}` : "Could not open that trip"' in js


# --- rulings 23a / 23b: the order panel's trip labels and earliest trip ---


def test_order_panel_survives_a_trip_type_outside_the_choices(staff):
    lead = LeadFactory()
    trip = _quiet_trip(lead=lead, service_type=None, pickup_date=date(2026, 7, 1))
    Reservation.objects.filter(pk=trip.pk).update(trip_type="mystery")
    with audit_context(actor=None, source="portal"):
        TripExtraFactory(reservation=trip, label="Tolls", amount=25)

    resp = _get(staff, "order", lead.pk)

    assert resp.status_code == 200
    assert "Jul 1 · mystery" in resp.content.decode()


def test_order_panel_zone_is_the_timed_trip_when_dates_tie(staff):
    lead = LeadFactory()
    _quiet_trip(lead=lead, pickup_date=date(2026, 7, 1), pickup_time=None, pickup_timezone=LA)
    timed = _quiet_trip(
        lead=lead, pickup_date=date(2026, 7, 1), pickup_time=time(9, 0), pickup_timezone=NY
    )
    assert _order_panel(lead.pk).zone == timed.pickup_timezone == NY


# --- the import seam ---


def test_import_seam_restores_request_frame():
    user = UserFactory()
    other = UserFactory()
    seen = {}

    def get_response(request):
        ReservationFactory()
        with audit_context(actor=other, source="import: x.xlsx", cid="import-7"):
            ContactFactory()
        ReservationFactory()
        seen["done"] = True
        return HttpResponse("ok")

    request = RequestFactory().post("/portal/orders/1/")
    request.user = user
    AuditMiddleware(get_response)(request)

    entries = AuditEntry.objects.order_by("pk")
    trips = list(entries.filter(content_type=ContentType.objects.get_for_model(Reservation)))
    imported = list(entries.filter(cid="import-7"))
    assert seen and len(trips) == 2 and imported
    assert {e.source for e in trips} == {"portal"}
    assert {e.actor_id for e in trips} == {user.pk}
    assert trips[0].cid == trips[1].cid and trips[0].cid.startswith("r-")
    assert {(e.source, e.actor_id) for e in imported} == {("import: x.xlsx", other.pk)}
