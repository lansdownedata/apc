from datetime import time
from decimal import Decimal

import pytest

from apps.leads.factories import LeadFactory, VehicleTypeFactory
from apps.reservations import drafts
from apps.reservations.drafts import DraftError, save_reservation_from_draft
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db


def _payload(**over):
    base = {
        "tripType": "transfer",
        "service": "Airport transfer",
        "date": "2026-07-04",
        "time": "15:00",
        "vehicle": "",
        "pax": 3,
        "baseRate": 185,
        "hours": 0,
        "hourlyRate": 0,
        "minHours": 0,
        "stops": [{"address": "Dulles", "note": ""}, {"address": "The Ritz", "note": ""}],
    }
    base.update(over)
    return base


def test_parse_rejects_bad_trip_type():
    with pytest.raises(drafts.DraftError):
        drafts.parse_draft(_payload(tripType="boat"))


def test_parse_rejects_too_few_stops():
    with pytest.raises(drafts.DraftError):
        drafts.parse_draft(_payload(stops=[{"address": "only one"}]))


def test_parse_rejects_negative_money():
    with pytest.raises(drafts.DraftError):
        drafts.parse_draft(_payload(baseRate=-5))


def test_parse_rejects_bad_date():
    with pytest.raises(drafts.DraftError):
        drafts.parse_draft(_payload(date="Jul 4"))


def test_save_transfer_creates_reservation_with_stops():
    lead = LeadFactory()
    res = drafts.save_reservation_from_draft(lead, _payload())
    assert res.lead == lead
    assert res.line_total == Decimal("185.00")
    assert [s.address for s in res.ordered_stops] == ["Dulles", "The Ritz"]


def test_save_hourly_applies_minimum():
    lead = LeadFactory()
    res = drafts.save_reservation_from_draft(
        lead, _payload(tripType="hourly", hours=3, hourlyRate=295, minHours=5, baseRate=0)
    )
    assert res.trip_type == Reservation.TripType.HOURLY
    assert res.line_total == Decimal("1475.00")  # max(3, 5) * 295


def test_save_multi_stop_orders_stops():
    lead = LeadFactory()
    res = drafts.save_reservation_from_draft(
        lead,
        _payload(stops=[{"address": "A"}, {"address": "B", "note": "photos"}, {"address": "C"}]),
    )
    assert [s.sequence for s in res.ordered_stops] == [0, 1, 2]
    assert res.ordered_stops[1].note == "photos"


def test_save_assigns_vehicle_and_updates_in_place():
    lead = LeadFactory()
    veh = VehicleTypeFactory(name="Sprinter Van")
    res = drafts.save_reservation_from_draft(lead, _payload(vehicle=veh.pk))
    assert res.vehicle == veh
    again = drafts.save_reservation_from_draft(
        lead, _payload(service="Renamed", vehicle=veh.pk), instance=res
    )
    assert again.pk == res.pk
    assert again.service == "Renamed"
    assert again.stops.count() == 2  # replaced, not duplicated


def test_stop_name_and_time_round_trip():
    lead = LeadFactory()
    payload = {
        "tripType": "transfer",
        "pax": 4,
        "baseRate": "250.00",
        "stops": [
            {
                "address": "111 Byte Drive, Frederick, MD 21702",
                "name": "SpringHill Suites by Marriott Frederick",
                "time": "11:00",
                "note": "",
            },
            {
                "address": "8529 Liberty Road, Frederick, MD 21701",
                "name": "Ceresville Mansion",
                "time": "11:30",
                "note": "",
            },
        ],
    }
    res = save_reservation_from_draft(lead, payload)
    stops = list(res.stops.all())
    assert stops[0].name == "SpringHill Suites by Marriott Frederick"
    assert stops[0].scheduled_time == time(11, 0)
    assert stops[1].name == "Ceresville Mansion"
    assert stops[1].scheduled_time == time(11, 30)


def test_stop_without_name_or_time_is_fine():
    lead = LeadFactory()
    payload = {
        "tripType": "transfer",
        "pax": 2,
        "baseRate": "100.00",
        "stops": [{"address": "A St"}, {"address": "B St"}],
    }
    res = save_reservation_from_draft(lead, payload)
    stops = list(res.stops.all())
    assert stops[0].name == ""
    assert stops[0].scheduled_time is None


def test_a_bad_stop_time_is_rejected():
    lead = LeadFactory()
    payload = {
        "tripType": "transfer",
        "pax": 2,
        "baseRate": "100.00",
        "stops": [{"address": "A St", "time": "25:99"}, {"address": "B St"}],
    }
    with pytest.raises(DraftError):
        save_reservation_from_draft(lead, payload)
