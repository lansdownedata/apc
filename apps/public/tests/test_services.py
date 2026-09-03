from apps.public.services import create_lead_from_booking


def _data(**over):
    d = {
        "name": "Jane Rider",
        "email": "jane@example.com",
        "passengers": 2,
        "service": "Airport Transfer",
        "stops": [],
    }
    d.update(over)
    return d


def test_stops_written_in_sequence(db):
    lead = create_lead_from_booking(
        _data(
            stops=[
                {"address": "123 Main St", "lat": 38.9, "lng": -77.4},
                {"address": "Reston Town Center", "lat": None, "lng": None},
                {"address": "Dulles Intl (IAD)", "lat": 38.95, "lng": -77.45},
            ]
        )
    )
    res = lead.reservations.get()
    stops = list(res.stops.order_by("sequence"))
    assert [s.sequence for s in stops] == [0, 1, 2]
    assert [s.address for s in stops] == ["123 Main St", "Reston Town Center", "Dulles Intl (IAD)"]
    assert float(stops[0].latitude) == 38.9


def test_no_stops_creates_no_stop_rows(db):
    lead = create_lead_from_booking(_data(stops=[]))
    res = lead.reservations.get()
    assert res.stops.count() == 0
    assert lead.contact.name == "Jane Rider"


def test_booking_resolves_timezone_from_pickup_coordinates(db):
    lead = create_lead_from_booking(
        _data(
            stops=[
                {"address": "Santa Monica Blvd", "lat": 34.0194, "lng": -118.4912},
                {"address": "LAX", "lat": 33.9416, "lng": -118.4085},
            ]
        )
    )
    assert lead.reservations.get().pickup_timezone == "America/Los_Angeles"


# --- APC-14: a website wedding builds the coaches it told the couple about --------------


def test_a_public_wedding_builds_one_trip_per_coach(db):
    """The couple is shown "2 × 56-passenger coach" on the itinerary; the quote the
    office picks up has to be those two coaches, not one trip for 105 people."""
    from apps.public.forms import WeddingRequestForm
    from apps.public.services import create_lead_from_wedding
    from apps.public.tests.test_wedding_form import _post

    form = WeddingRequestForm(_post())
    assert form.is_valid(), form.errors

    lead = create_lead_from_wedding(form.cleaned_data)

    coaches = list(lead.reservations.filter(source_leg_id="guests-in").order_by("sort_order", "id"))
    assert len(coaches) == 2
    assert coaches[0].group_key is not None
    assert coaches[0].group_key == coaches[1].group_key
    assert [c.passengers for c in coaches] == [53, 52]
    for coach in coaches:
        assert [s.name for s in coach.ordered_stops] == [
            "Hampton Inn Leesburg",
            "The Oak Barn at Loyalty",
        ]


def test_a_public_wedding_leaves_a_small_leg_unlinked(db):
    import json

    from apps.public.forms import WeddingRequestForm
    from apps.public.services import create_lead_from_wedding
    from apps.public.tests.test_wedding_form import _legs, _post

    legs = _legs()
    for leg in legs:
        leg["pax"] = 10
    form = WeddingRequestForm(_post(legs_json=json.dumps(legs)))
    assert form.is_valid(), form.errors

    lead = create_lead_from_wedding(form.cleaned_data)

    assert lead.reservations.count() == 2
    assert set(lead.reservations.values_list("group_key", flat=True)) == {None}


def test_the_notification_counts_movements_not_coaches(db):
    """ "5 movements" is what the office reads on the alert — a wedding that needs nine
    coaches is still the same day's work to look at."""
    from apps.notifications.models import Notification
    from apps.public.forms import WeddingRequestForm
    from apps.public.services import create_lead_from_wedding
    from apps.public.tests.test_wedding_form import _post

    form = WeddingRequestForm(_post())
    assert form.is_valid(), form.errors

    lead = create_lead_from_wedding(form.cleaned_data)

    alert = Notification.objects.get(lead=lead, kind=Notification.Kind.NEW_LEAD)
    assert "2 movements" in alert.detail
