import pytest
from django.urls import reverse

from apps.accounts.factories import UserFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead
from apps.leads.services import make_deposit_token
from apps.reservations.factories import PassengerFactory, ReservationFactory

pytestmark = pytest.mark.django_db
QUERY_BUDGET = 43  # lead detail, 5 trips x 3 passengers, measured before this task


@pytest.fixture
def staff(client):
    client.force_login(UserFactory(is_staff=True, is_superuser=True))
    return client


def _lead_with_trips(trips=5, people=3):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    for i in range(trips):
        res = ReservationFactory(lead=lead, passengers=people)
        for j in range(people):
            PassengerFactory(reservation=res, name=f"Guest {i}-{j}", sort=j, is_lead=(j == 0))
    return lead


def test_trip_line_shows_passenger_summary(staff):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    res = ReservationFactory(lead=lead, passengers=3)
    PassengerFactory(
        reservation=res, name="Dana Whitfield", mobile="+12025550147", is_lead=True, sort=0
    )
    PassengerFactory(reservation=res, name="Marcus Reed", sort=1)
    PassengerFactory(reservation=res, name="Priya Shah", sort=2)
    body = staff.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "Dana Whitfield · lead, +2 named" in body
    assert "data-passenger-summary" in body


def test_trip_line_has_no_passenger_text_without_names(staff):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    ReservationFactory(lead=lead, passengers=3)
    body = staff.get(reverse("lead_detail", args=[lead.pk])).content.decode()
    assert "data-passenger-summary" not in body


def test_lead_detail_passenger_queries_do_not_grow_per_trip(staff, django_assert_max_num_queries):
    lead = _lead_with_trips()
    with django_assert_max_num_queries(QUERY_BUDGET):
        assert staff.get(reverse("lead_detail", args=[lead.pk])).status_code == 200


def test_quote_page_lists_each_trips_names_without_mobiles(client):
    lead = LeadFactory(status=Lead.Status.QUOTED)
    one = ReservationFactory(lead=lead)
    two = ReservationFactory(lead=lead)
    PassengerFactory(reservation=one, name="Shane Thomas", mobile="+12025550147", sort=0)
    PassengerFactory(reservation=one, name="Ana Ruiz", sort=1)
    PassengerFactory(reservation=two, name="Lee Park", mobile="+12025550148", sort=0)
    body = client.get(reverse("quote_page", args=[make_deposit_token(lead)])).content.decode()
    assert "Shane Thomas, Ana Ruiz" in body
    assert "Lee Park" in body
    assert "+1202" not in body
