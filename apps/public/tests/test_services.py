from apps.public.services import create_lead_from_booking


def _data(**over):
    d = {"name": "Jane Rider", "email": "jane@example.com", "passengers": 2, "service": "Airport Transfer", "stops": []}
    d.update(over)
    return d


def test_stops_written_in_sequence(db):
    lead = create_lead_from_booking(_data(stops=[
        {"address": "123 Main St", "suite": "Apt 4", "lat": 38.9, "lng": -77.4},
        {"address": "Reston Town Center", "suite": "", "lat": None, "lng": None},
        {"address": "Dulles Intl (IAD)", "suite": "", "lat": 38.95, "lng": -77.45},
    ]))
    res = lead.reservations.get()
    stops = list(res.stops.order_by("sequence"))
    assert [s.sequence for s in stops] == [0, 1, 2]
    assert [s.address for s in stops] == ["123 Main St", "Reston Town Center", "Dulles Intl (IAD)"]
    assert stops[0].note == "Apt 4"
    assert float(stops[0].latitude) == 38.9


def test_no_stops_creates_no_stop_rows(db):
    lead = create_lead_from_booking(_data(stops=[]))
    res = lead.reservations.get()
    assert res.stops.count() == 0
    assert lead.contact.name == "Jane Rider"
