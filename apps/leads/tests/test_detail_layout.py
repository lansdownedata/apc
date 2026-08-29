"""Quote workspace layout — the header carries agent + LA state, reservations own a
full-width row, and the payment plan lives inside the ledger card."""

import re

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.integrations.models import ZapEvent
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.payments import services as payment_services
from apps.reservations.factories import ReservationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def page(client):
    client.force_login(UserFactory())

    def _get(lead) -> str:
        return client.get(reverse("lead_detail", args=[lead.pk])).content.decode()

    return _get


def test_agent_select_sits_in_the_header_beside_source(page):
    html = page(LeadFactory())
    source, agent = html.index('id="hdr-channel"'), html.index('id="hdr-agent"')
    assert source < agent < html.index("Add reservation")


def test_created_date_sits_in_the_header_not_a_side_card(page):
    html = page(LeadFactory())
    assert html.index("Created") < html.index("Add reservation")
    assert "Assignment</div>" not in html


def test_reservations_own_a_full_width_row(page):
    html = page(LeadFactory())
    assert "lg:grid-cols-3" not in html
    assert "lg:col-span-2" not in html


def test_payment_plan_lives_inside_the_ledger_card(page):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    ReservationFactory(lead=lead)
    payment_services.ensure_plan(lead)
    html = page(lead)
    assert html.index("Payments &amp; Ledger") < html.index("Payment plan")
    assert "Deposit (50%)" in html


def test_an_unsent_quote_explains_the_deposit_inside_the_ledger_card(page):
    html = page(LeadFactory())
    assert html.index("Payments &amp; Ledger") < html.index("No deposit requested yet")


def test_la_sync_state_is_hidden_on_a_quote(page):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    ReservationFactory(lead=lead)
    assert "LimoAnywhere sync" not in page(lead)


def test_la_sync_state_shows_in_the_header_of_an_order(page):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    ReservationFactory(lead=lead)
    html = page(lead)
    assert html.index("LimoAnywhere sync") < html.index("Add reservation")
    assert "Not sent" in html


# --- the chip summarises every trip's LA state in one word ---


def _order_with_trips(n: int):
    lead = LeadFactory(status=Lead.Status.BOOKED)
    return lead, [ReservationFactory(lead=lead) for _ in range(n)]


def _event(res, result):
    ZapEvent.objects.create(
        lead=res.lead,
        action=ZapEvent.Action.CREATE_RESERVATION,
        idempotency_key=f"create_reservation-res{res.pk}",
        result=result,
    )


def test_la_chip_reads_sent_when_every_trip_went_through(page):
    lead, trips = _order_with_trips(2)
    for res in trips:
        _event(res, ZapEvent.Result.SUCCESS)
    assert "LA · Sent" in page(lead)


def test_la_chip_reads_failed_when_any_trip_errored(page):
    lead, (ok, bad) = _order_with_trips(2)
    _event(ok, ZapEvent.Result.SUCCESS)
    _event(bad, ZapEvent.Result.ERROR)
    assert "LA · Failed" in page(lead)


def test_la_chip_reads_preview_when_a_trip_was_only_previewed(page):
    lead, (res,) = _order_with_trips(1)
    _event(res, ZapEvent.Result.PREVIEW)
    assert "LA · Preview" in page(lead)


def test_la_chip_reads_not_sent_when_only_some_trips_went_through(page):
    """One trip sent and one never attempted is not "Sent" — the dispatcher would stop
    looking for the missing booking."""
    lead, (sent, _) = _order_with_trips(2)
    _event(sent, ZapEvent.Result.SUCCESS)
    assert "LA · Not sent" in page(lead)


# --- pricing block: minimum is locked, hours is the override ---


def _editor_input(html: str, model: str) -> str:
    match = re.search(rf"<input[^>]*{re.escape(model)}[^>]*>", html)
    assert match, f"no input bound to {model}"
    return match.group(0)


def test_editor_locks_min_hours_and_frees_override_hours(page):
    html = page(LeadFactory())
    assert "readonly" in _editor_input(html, 'x-model.number="draft.minHours"')
    assert "Override hours" in html
    assert 'x-model.number="draft.hours"' in html
    assert ':readonly="draft.tripType' not in html


def test_editor_rate_and_gratuity_inputs_paint_no_background_of_their_own(page):
    """Dark mode: the wrapper is bg-surface; an inner input with the browser default
    background renders as a light box inside a dark frame."""
    html = page(LeadFactory())
    for model in ('x-model.number="draft.rate"', 'x-model.number="draft.gratuityPct"'):
        assert "bg-transparent" in _editor_input(html, model)


# --- the editor's airline picker is fed from context, active carriers only ---


def test_airline_options_offer_active_carriers_only(client):
    """The editor's airline picker (Task 4) reads this context; retired carriers must not
    be offered for new stops."""
    from apps.addresses.factories import AirlineFactory
    from apps.addresses.models import Airline

    AirlineFactory(iata="ZZ", name="Retired Air", is_active=False)
    client.force_login(UserFactory())
    resp = client.get(reverse("lead_detail", args=[LeadFactory().pk]))
    options = list(resp.context["airline_options"])
    united = Airline.objects.get(iata="UA")
    assert (united.pk, "UA — United Airlines") in options
    assert all(label != "ZZ — Retired Air" for _, label in options)


def test_editor_renders_the_flight_row_for_airport_stops(page):
    html = page(LeadFactory())
    assert 'x-show="s.airport"' in html
    assert 'x-model="s.flight"' in html
    assert "initTomSelects($el)" in html
    assert 'x-text="s.airportCode"' in html
    assert "flightVerifyComingSoon()" in html
    assert "UA — United Airlines</option>" in html


# --- the route loop shows the flight on an airport stop ---


def _with_flight(reservation, *, sequence=0, number="123"):
    """Attach IAD + United + `number` to the stop at `sequence` and return it."""
    from apps.addresses.models import Airline, Airport

    stop = reservation.stops.get(sequence=sequence)
    stop.airport = Airport.objects.get(iata="IAD")  # seeded by addresses.0003
    stop.airline = Airline.objects.get(iata="UA")
    stop.flight_number = number
    stop.save()
    return stop


def test_workspace_card_shows_the_flight_on_an_airport_stop(page):
    lead = LeadFactory()
    _with_flight(ReservationFactory(lead=lead))
    html = page(lead)
    assert "✈ UA 123" in html
