"""APC-66: the planner's sign-in pages, the wedding list, and the wall between planners and
the staff portal."""

from datetime import date, time, timedelta
from unittest.mock import patch

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.factories import UserFactory
from apps.contacts.factories import ContactFactory
from apps.leads.factories import LeadFactory
from apps.leads.models import Lead, LeadContact
from apps.planners import services
from apps.planners.auth import SESSION_KEY
from apps.planners.models import PlannerAccount
from apps.planners.tokens import planner_token
from apps.planners.views import TOKEN_PLACEHOLDER
from apps.reservations.factories import ReservationFactory
from apps.reservations.models import Reservation

pytestmark = pytest.mark.django_db

PASSWORD = "a-long-pass-phrase-77"


@pytest.fixture
def account():
    contact = ContactFactory(name="Dana Planner", email="dana@example.com")
    with patch("apps.planners.services.send_html_email", return_value=True):
        account, _ = services.invite(contact, actor=UserFactory())
    return account


@pytest.fixture
def registered(account):
    return services.register(account, PASSWORD)


@pytest.fixture
def planner_client(client, registered):
    response = client.post(
        reverse("planners:login"), {"email": "dana@example.com", "password": PASSWORD}
    )
    assert response.status_code == 302
    return client


def _register_url(account):
    return reverse(
        "planners:register",
        kwargs={
            "uidb64": urlsafe_base64_encode(force_bytes(account.pk)),
            "token": planner_token.make_token(account),
        },
    )


def _wedding(contact, *, status=Lead.Status.BOOKED, days=30, name="Whitfield & Okafor"):
    lead = LeadFactory(status=status, wedding_name=name)
    ReservationFactory(
        lead=lead,
        pickup_date=timezone.localdate() + timedelta(days=days),
        pickup_time=time(15, 0),
        pickup_timezone="America/New_York",
    )
    LeadContact.objects.create(lead=lead, contact=contact, role=LeadContact.Role.LEAD_PLANNER)
    return lead


# --- registration + sign-in ----------------------------------------------------------


def test_an_invited_planner_registers_and_lands_signed_in(client, account):
    url = _register_url(account)
    hop = client.get(url)
    # like Django's reset view: the token moves into the session and out of the address bar
    assert hop.status_code == 302
    assert TOKEN_PLACEHOLDER in hop.url and planner_token.make_token(account) not in hop.url
    page = client.get(hop.url)
    assert page.status_code == 200
    assert "dana@example.com" in page.content.decode()

    response = client.post(hop.url, {"new_password1": PASSWORD, "new_password2": PASSWORD})
    assert response.status_code == 302
    assert response.url == reverse("planners:weddings")
    account.refresh_from_db()
    assert account.status == PlannerAccount.Status.REGISTERED
    assert client.get(reverse("planners:weddings")).status_code == 200


def test_the_token_less_address_needs_the_session_token(client, account):
    hop = client.get(_register_url(account)).url
    from django.test import Client

    assert Client().get(hop).status_code == 400


def test_a_bad_or_used_registration_link_is_refused(client, account):
    url = _register_url(account)
    hop = client.get(url).url
    client.post(hop, {"new_password1": PASSWORD, "new_password2": PASSWORD})
    assert client.get(url).status_code == 400
    bogus = reverse("planners:register", kwargs={"uidb64": "MTIz", "token": "nope"})
    assert client.get(bogus).status_code == 400


def test_planner_pages_carry_no_marketing_embeds_or_footer(client, settings):
    settings.CALENDLY_URL = "https://calendly.com/allprocharter/chat"
    html = client.get(reverse("planners:login")).content.decode()
    assert "calendly" not in html.lower()
    assert "openScheduler" not in html
    assert "Service Area" not in html  # the marketing footer
    home = client.get("/").content.decode()
    assert "calendly" in home.lower()  # the public site is unchanged


def test_an_uninvited_contact_has_no_way_in(client):
    ContactFactory(email="stranger@example.com")
    response = client.post(
        reverse("planners:login"), {"email": "stranger@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert "don't match" in response.content.decode()
    assert SESSION_KEY not in client.session


def test_sign_in_and_sign_out(client, registered):
    response = client.post(
        reverse("planners:login"), {"email": "Dana@Example.com", "password": PASSWORD}
    )
    assert response.status_code == 302
    registered.refresh_from_db()
    assert registered.last_login is not None
    assert client.get(reverse("planners:weddings")).status_code == 200

    assert client.get(reverse("planners:logout")).status_code == 405  # POST only
    client.post(reverse("planners:logout"))
    response = client.get(reverse("planners:weddings"))
    assert response.status_code == 302
    assert response.url.startswith(reverse("planners:login"))


def test_signed_out_visitors_are_sent_to_the_planner_sign_in(client):
    response = client.get(reverse("planners:weddings"))
    assert response.status_code == 302
    assert response.url == f"{reverse('planners:login')}?next=/planner/"


def test_the_sign_in_page_uses_the_public_shell(client):
    html = client.get(reverse("planners:login")).content.decode()
    assert "Lead Manager" not in html  # the staff auth shell's wordmark
    assert 'name="email"' in html


def test_a_safe_next_is_honoured_and_an_offsite_one_is_not(client, registered):
    data = {"email": "dana@example.com", "password": PASSWORD}
    good = client.post(f"{reverse('planners:login')}?next=/planner/", data)
    assert good.url == "/planner/"
    client.post(reverse("planners:logout"))
    bad = client.post(f"{reverse('planners:login')}?next=https://evil.example/", data)
    assert bad.url == reverse("planners:weddings")


def test_repeated_failures_are_throttled(client, registered):
    data = {"email": "dana@example.com", "password": "wrong"}
    for _ in range(10):
        client.post(reverse("planners:login"), data)
    response = client.post(
        reverse("planners:login"), {"email": "dana@example.com", "password": PASSWORD}
    )
    assert response.status_code == 429
    assert SESSION_KEY not in client.session


def test_revoking_ends_an_open_session_at_once(planner_client, registered):
    services.revoke(registered, actor=UserFactory())
    response = planner_client.get(reverse("planners:weddings"))
    assert response.status_code == 302
    assert SESSION_KEY not in planner_client.session


def test_a_new_password_ends_other_sessions(planner_client, registered):
    services.register(registered, "a-different-phrase-99")
    assert planner_client.get(reverse("planners:weddings")).status_code == 302


def test_page_views_are_logged(planner_client, registered):
    planner_client.get(reverse("planners:weddings"))
    planner_client.get(reverse("planners:weddings"))
    registered.refresh_from_db()
    assert registered.view_count == 2
    assert registered.last_seen_at is not None


def test_password_reset_sends_a_link_only_to_someone_who_can_sign_in(client, registered):
    with patch("apps.planners.services.send_html_email", return_value=True) as send:
        page = client.post(reverse("planners:password_reset"), {"email": "dana@example.com"})
        client.post(reverse("planners:password_reset"), {"email": "nobody@example.com"})
    assert page.status_code == 200
    assert send.call_count == 1
    assert send.call_args.kwargs["template"] == "planner_password_reset"
    assert send.call_args.kwargs.get("internal", False) is False


# --- the wedding list ------------------------------------------------------------------


def test_the_list_shows_current_weddings_then_past_ones_archived(planner_client, registered):
    contact = registered.contact
    current = _wedding(contact, days=30, name="Current Couple")
    past = _wedding(contact, days=-20, name="Past Couple")
    Reservation.objects.filter(lead=past).update(trip_status=Reservation.TripStatus.DONE)
    _wedding(ContactFactory(), name="Not Mine")

    html = planner_client.get(reverse("planners:weddings")).content.decode()
    assert "Current Couple" in html and "Past Couple" in html
    assert "Not Mine" not in html
    assert html.index("Current Couple") < html.index("Past Couple")
    assert "Archived" in html
    assert reverse("planners:wedding", args=[current.pk]) in html
    assert reverse("planners:wedding", args=[past.pk]) in html


def test_the_list_has_an_empty_state(planner_client):
    html = planner_client.get(reverse("planners:weddings")).content.decode()
    assert "No weddings yet" in html


def test_a_past_wedding_still_renders(planner_client, registered):
    past = _wedding(registered.contact, days=-20)
    Reservation.objects.filter(lead=past).update(trip_status=Reservation.TripStatus.DONE)
    assert planner_client.get(reverse("planners:wedding", args=[past.pk])).status_code == 200


def test_date_label_uses_the_wedding_day(planner_client, registered):
    lead = _wedding(registered.contact, days=0)
    lead.reservations.update(pickup_date=date(2026, 11, 14))
    html = planner_client.get(reverse("planners:weddings")).content.decode()
    assert "Saturday, November 14, 2026" in html


# --- the wall between planners and staff ------------------------------------------------


def _sample(pattern) -> str:
    """A concrete path for a route, with throwaway values for its converters."""
    route = str(pattern.pattern)
    out, i = "", 0
    while i < len(route):
        if route[i] == "<":
            j = route.index(">", i)
            spec = route[i + 1 : j]
            kind = spec.split(":")[0] if ":" in spec else "str"
            out += {"int": "1", "uuid": "00000000-0000-0000-0000-000000000000"}.get(kind, "x")
            i = j + 1
        else:
            out += route[i]
            i += 1
    return out


def _routes(resolver: URLResolver, prefix: str = ""):
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            if entry.pattern.regex.pattern.startswith("^") and "(?P" in str(entry.pattern):
                continue  # re_path includes (media) — not staff routes
            yield from _routes(entry, prefix + _sample(entry))
        elif isinstance(entry, URLPattern):
            if hasattr(entry.pattern, "_route"):
                yield prefix + _sample(entry)


def _staff_paths() -> list[str]:
    paths = [p for p in _routes(get_resolver()) if p.startswith(("portal/", "admin/"))]
    # The staff sign-in pages answer anyone by design; they hold no staff data.
    public = {
        "portal/login/",
        "portal/logout/",
        "portal/password_reset/",
        "portal/password_reset/done/",
        "portal/reset/x/x/",
        "portal/reset/done/",
        "admin/login/",
        "admin/logout/",
    }
    return [f"/{p}" for p in paths if p not in public]


def test_the_staff_route_list_is_real():
    paths = _staff_paths()
    assert len(paths) > 100
    assert "/portal/" in paths and "/portal/contacts/1/" in paths


def test_a_planner_session_cannot_load_any_staff_route(planner_client, registered):
    _wedding(registered.contact)
    leaked = []
    for path in _staff_paths():
        for method in (planner_client.get, planner_client.post):
            response = method(path)
            if response.status_code == 200 or (
                response.status_code == 302 and "/login/" not in response.url
            ):
                leaked.append((method.__name__, path, response.status_code))
    assert leaked == []


def test_planner_sign_in_never_creates_a_staff_session(planner_client):
    assert "_auth_user_id" not in planner_client.session
    assert planner_client.get("/portal/").status_code == 302


def test_planner_credentials_do_not_open_the_staff_sign_in(client, registered):
    response = client.post(reverse("login"), {"username": "dana@example.com", "password": PASSWORD})
    assert response.status_code == 200  # the form again, with its error
    assert "_auth_user_id" not in client.session


def test_a_staff_session_does_not_open_the_planner_area(client):
    client.force_login(UserFactory())
    response = client.get(reverse("planners:weddings"))
    assert response.status_code == 302
    assert response.url.startswith(reverse("planners:login"))


def test_the_wedding_list_costs_the_same_for_one_wedding_or_ten(planner_client, registered):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.tasks.models import TaskConfig

    TaskConfig.load()

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert planner_client.get(reverse("planners:weddings")).status_code == 200
        return len(ctx.captured_queries)

    _wedding(registered.contact)
    one = count()
    for _ in range(9):
        _wedding(registered.contact)
    assert count() == one
