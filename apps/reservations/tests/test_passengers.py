import pytest

from apps.reservations.drafts import DraftError
from apps.reservations.factories import PassengerFactory, ReservationFactory
from apps.reservations.passengers import parse_passengers, save_passengers, split_names

pytestmark = pytest.mark.django_db


def test_parse_normalises_mobile_and_drops_empty_rows():
    rows = parse_passengers(
        [
            {"name": " Dana Whitfield ", "mobile": "(202) 555-0147", "lead": True},
            {"name": "", "mobile": "", "lead": False},
            {"name": "Priya Shah", "mobile": "", "lead": False},
        ]
    )
    assert rows == [
        {"name": "Dana Whitfield", "mobile": "+12025550147", "is_lead": True, "sort": 0},
        {"name": "Priya Shah", "mobile": "", "is_lead": False, "sort": 1},
    ]


def test_parse_absent_key_means_leave_alone():
    assert parse_passengers(None) is None


def test_parse_rejects_bad_mobile():
    with pytest.raises(DraftError, match="Passenger 1's mobile isn't a valid phone number."):
        parse_passengers([{"name": "A", "mobile": "12", "lead": False}])


def test_lead_without_mobile_is_not_lead():
    assert parse_passengers([{"name": "A", "mobile": "", "lead": True}])[0]["is_lead"] is False


def test_two_leads_rejected():
    with pytest.raises(DraftError, match="Only one passenger can be the lead."):
        parse_passengers(
            [
                {"name": "A", "mobile": "2025550147", "lead": True},
                {"name": "B", "mobile": "2025550182", "lead": True},
            ]
        )


def test_parse_rejects_non_list():
    with pytest.raises(DraftError):
        parse_passengers("nope")


def test_save_replaces_the_list():
    res = ReservationFactory()
    PassengerFactory(reservation=res, name="Old")
    save_passengers(res, [{"name": "New", "mobile": "", "is_lead": False, "sort": 0}])
    assert list(res.named_passengers.values_list("name", flat=True)) == ["New"]


def test_save_rejects_two_leads():
    res = ReservationFactory()
    rows = [
        {"name": "A", "mobile": "+12025550147", "is_lead": True, "sort": 0},
        {"name": "B", "mobile": "+12025550182", "is_lead": True, "sort": 1},
    ]
    with pytest.raises(DraftError):
        save_passengers(res, rows)


def test_summary():
    res = ReservationFactory(passengers=3)
    assert res.passenger_summary == ""
    PassengerFactory(reservation=res, name="Dana Whitfield", sort=0)
    PassengerFactory(reservation=res, name="Marcus Reed", sort=1)
    PassengerFactory(reservation=res, name="Priya Shah", sort=2)
    assert res.passenger_summary == "3 named"
    res.named_passengers.filter(name="Dana Whitfield").update(is_lead=True)
    assert res.passenger_summary == "Dana Whitfield · lead, +2 named"
    res.named_passengers.exclude(name="Dana Whitfield").delete()
    assert res.passenger_summary == "Dana Whitfield · lead"


def test_split_names():
    assert split_names("Shane Thomas, Ana Ruiz\nLee;") == ["Shane Thomas", "Ana Ruiz", "Lee"]


def test_data_migration_copies_names_to_first_trip(capsys):
    """`Lead.passenger_names` is dropped (leads.0020), so the historical lead rows are stood in
    for by real leads carrying the old free text; trips and passengers are the real models."""
    from importlib import import_module

    from apps.leads.factories import LeadFactory

    migration = import_module("apps.reservations.migrations.0029_passengers_from_names")
    lead = LeadFactory()
    lead.passenger_names = "Shane Thomas, Ana Ruiz"
    first = ReservationFactory(lead=lead, pickup_date="2026-12-01")
    ReservationFactory(lead=lead, pickup_date="2026-11-01")
    first_by_date = lead.reservations.order_by("pickup_date").first()
    empty = LeadFactory()
    empty.passenger_names = "Nobody"

    class _Leads:
        def exclude(self, **kwargs):
            return self

        def iterator(self):
            return iter([lead, empty])

    class _Apps:
        def get_model(self, app, name):
            if name == "Lead":
                return type("FakeLead", (), {"objects": _Leads()})
            return django_apps.get_model(app, name)

    from django.apps import apps as django_apps

    migration.names_to_passengers(_Apps(), None)
    assert list(first_by_date.named_passengers.values_list("name", flat=True)) == [
        "Shane Thomas",
        "Ana Ruiz",
    ]
    assert not first.named_passengers.exists()
    assert f"passenger_names dropped for lead {empty.pk}: no trips" in capsys.readouterr().out
