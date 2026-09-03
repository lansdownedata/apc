"""APC-17 — copy one reservation onto a set of selected service dates."""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.addresses.factories import AirlineFactory, AirportFactory
from apps.reservations import groups
from apps.reservations.factories import TransferReservationFactory
from apps.reservations.models import Stop

pytestmark = pytest.mark.django_db


def _shuttle(**over):
    defaults = {
        "pickup_date": date(2026, 9, 8),
        "pickup_time": time(7, 0),
        "dropoff_date": date(2026, 9, 8),
        "dropoff_time": time(9, 0),
        "rate": Decimal("140"),
        "passengers": 14,
    }
    defaults.update(over)
    return TransferReservationFactory(**defaults)


def test_copy_to_dates_makes_one_reservation_per_date():
    res = _shuttle()

    made = groups.copy_to_dates(res, [date(2026, 9, 9), date(2026, 9, 10)])

    assert [m.pickup_date for m in made] == [date(2026, 9, 9), date(2026, 9, 10)]
    assert res.lead.reservations.count() == 3


def test_copies_carry_the_vehicle_pricing_pax_and_route():
    iad = AirportFactory(iata="IAD", timezone="America/New_York")
    ua = AirlineFactory(iata="UA")
    res = _shuttle()
    res.stops.all().delete()
    Stop.objects.create(
        reservation=res, sequence=0, address="IAD", airport=iad, airline=ua, flight_number="55"
    )
    Stop.objects.create(reservation=res, sequence=1, address="Convention Center")

    made = groups.copy_to_dates(res, [date(2026, 9, 9)])

    copy = made[0]
    assert copy.vehicle_id == res.vehicle_id
    assert copy.rate == Decimal("140")
    assert copy.passengers == 14
    assert copy.pickup_time == time(7, 0)
    pickup = copy.stops.order_by("sequence").first()
    assert pickup.airport_id == iad.pk
    assert pickup.flight_number == "55"


def test_copies_are_independent_not_a_linked_set():
    res = _shuttle()

    made = groups.copy_to_dates(res, [date(2026, 9, 9), date(2026, 9, 10)])

    assert all(m.group_key is None for m in made)


def test_copies_preserve_the_overnight_dropoff_offset():
    res = _shuttle(pickup_date=date(2026, 9, 8), dropoff_date=date(2026, 9, 9))  # +1 day

    made = groups.copy_to_dates(res, [date(2026, 9, 20)])

    assert made[0].pickup_date == date(2026, 9, 20)
    assert made[0].dropoff_date == date(2026, 9, 21)


def test_the_sources_own_date_is_skipped():
    res = _shuttle(pickup_date=date(2026, 9, 8))

    made = groups.copy_to_dates(res, [date(2026, 9, 8), date(2026, 9, 9)])

    assert [m.pickup_date for m in made] == [date(2026, 9, 9)]


def test_duplicate_dates_are_collapsed():
    res = _shuttle()

    made = groups.copy_to_dates(res, [date(2026, 9, 9), date(2026, 9, 9)])

    assert len(made) == 1


def test_copy_to_dates_is_capped_at_duplicate_max():
    res = _shuttle(pickup_date=date(2026, 1, 1))
    many = [date(2026, 6, 1) + timedelta(days=n) for n in range(40)]

    made = groups.copy_to_dates(res, many)

    assert len(made) == groups.DUPLICATE_MAX


def test_copies_are_appended_after_the_leads_last_trip():
    res = _shuttle()
    TransferReservationFactory(lead=res.lead, sort_order=5)

    made = groups.copy_to_dates(res, [date(2026, 9, 9), date(2026, 9, 10)])

    assert sorted(m.sort_order for m in made) == [6, 7]


# --- the view ------------------------------------------------------------------------


def _post(client, res, dates):
    return client.post(
        reverse("reservation_copy_dates"),
        data={"reservation": res.pk, "dates": dates},
    )


def test_copy_view_creates_the_dated_copies_and_redirects(client):
    res = _shuttle()
    client.force_login(UserFactory())

    resp = _post(client, res, ["2026-09-09", "2026-09-10"])

    assert resp.status_code == 302
    assert resp.url == reverse("lead_detail", args=[res.lead_id])
    assert res.lead.reservations.count() == 3


def test_copy_view_ignores_unparseable_dates(client):
    res = _shuttle()
    client.force_login(UserFactory())

    _post(client, res, ["2026-09-09", "not-a-date"])

    assert res.lead.reservations.count() == 2


def test_copy_view_400s_when_no_valid_date_is_given(client):
    res = _shuttle()
    client.force_login(UserFactory())

    resp = _post(client, res, ["nope"])

    assert resp.status_code == 400
    assert res.lead.reservations.count() == 1


def test_copy_view_requires_login(client):
    res = _shuttle()

    resp = _post(client, res, ["2026-09-09"])

    assert resp.status_code == 302
    assert "/login" in resp.url
    assert res.lead.reservations.count() == 1
