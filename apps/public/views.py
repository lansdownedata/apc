import hashlib

from django.conf import settings
from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.addresses.search import MIN_VENUE_QUERY, VENUE_RESULT_LIMIT, search_venues
from apps.core.net import client_ip
from apps.integrations.calendly import parse_start_time
from apps.integrations.geocoding import autocomplete as locationiq_autocomplete
from apps.integrations.geocoding import merged_autocomplete
from apps.leads.models import Lead

from .forms import (
    BookingRequestForm,
    WeddingRequestForm,
    occasion_options,
    service_slug_ids,
    service_type_for_slug,
)
from .services import (
    create_lead_from_booking,
    create_lead_from_wedding,
    make_wedding_token,
    read_wedding_token,
    send_wedding_confirmation,
)

# Coarse per-IP throttle for the unauthenticated bookings POST — caps mass Lead
# creation from scripted/bot traffic that skips the honeypot field entirely.
BOOKING_THROTTLE_LIMIT = 5
BOOKING_THROTTLE_WINDOW_SECONDS = 60 * 60  # 1 hour


def _booking_throttle_key(request) -> str:
    """Rolling-window per-IP counter key using Django's default cache (LocMemCache
    in dev — per-process, so this throttle is per-worker until a shared cache like
    Redis/Memcached is configured for prod; acceptable baseline for now).

    The caller comes from `core.net.client_ip`, not straight off REMOTE_ADDR: behind
    Heroku's router every visitor presents as the router itself, so one bucket covered
    the whole site and real booking requests were being rejected once a handful of
    people had submitted that hour.
    """
    return f"bookings-throttle:{client_ip(request)}"


def _booking_throttle_exceeded(request) -> bool:
    """Peek at the rolling-window counter without incrementing it."""
    return cache.get(_booking_throttle_key(request), 0) >= BOOKING_THROTTLE_LIMIT


def _booking_throttle_increment(request) -> None:
    """Count this request toward the per-IP limit.

    Only called for outcomes that are actually abusive-or-successful — a Lead
    that got created, or a tripped honeypot (spam) — never for an ordinary
    validation failure. Otherwise a legitimate visitor who mis-fills the form a
    few times in a row (wrong date format, forgot a phone number) gets locked
    out for an hour on their own fumbles.
    """
    key = _booking_throttle_key(request)
    cache.set(key, cache.get(key, 0) + 1, BOOKING_THROTTLE_WINDOW_SECONDS)


# Public autocomplete proxy — generous per-IP cap so a fast typist isn't blocked,
# but scripted scraping of LocationIQ (which we pay for) is bounded.
GEOCODE_THROTTLE_LIMIT = 60
GEOCODE_THROTTLE_WINDOW_SECONDS = 60


def _geocode_throttle_exceeded(request, scope: str = "geocode") -> bool:
    """Rolling per-IP cap on an unauthenticated autocomplete proxy.

    `scope` gives each proxy its own counter: the venue typeahead and the address
    typeahead are separate surfaces, and spending one's budget must never lock a
    visitor out of the other.
    """
    key = f"{scope}-throttle:{client_ip(request)}"
    count = cache.get(key, 0) + 1
    cache.set(key, count, GEOCODE_THROTTLE_WINDOW_SECONDS)
    return count > GEOCODE_THROTTLE_LIMIT


def home(request):
    return render(
        request,
        "public/home.html",
        {
            "form": BookingRequestForm(),
            "occasion_options": occasion_options(),
            "service_slugs": service_slug_ids(),
        },
    )


def bookings(request):
    if request.method == "POST":
        form = BookingRequestForm(request.POST)
        # The honeypot field: real visitors never see or fill it, so a filled
        # `company` is spam regardless of whether the rest of the form validates.
        is_spam = bool(request.POST.get("company"))
        if _booking_throttle_exceeded(request):
            form.add_error(None, "Too many requests — please call (202) 424-2600.")
        elif form.is_valid():
            create_lead_from_booking(form.cleaned_data)
            _booking_throttle_increment(request)
            return redirect("public:booking_thanks")
        elif is_spam:
            _booking_throttle_increment(request)
    else:
        # `?service=` is what the hero picker's cards fall back to with no JavaScript:
        # the same occasion, preselected server-side. An unknown slug preselects
        # nothing rather than erroring — it is a URL a visitor can type.
        service = service_type_for_slug(request.GET.get("service", ""))
        form = BookingRequestForm(initial={"service_type": service.pk} if service else None)
    return render(
        request, "public/bookings.html", {"form": form, "occasion_options": occasion_options()}
    )


# Calendly truncates nothing on its side; cap what we echo back so a hand-built URL
# can't stretch the layout with a kilobyte "name".
CALENDLY_NAME_MAXLEN = 80


def schedule_thanks(request):
    """Where Calendly sends an invitee after they book the discovery call.

    Both values come from the query string Calendly appends ("Pass event details to
    your redirected page"), which any visitor can forge — so they are DISPLAY ONLY.
    Nothing here is looked up or written. The Lead is created by the signed webhook
    at /webhooks/calendly/, never by this page.

    Arriving with no params at all is normal, not an error: our own popup redirects
    here from the parent window, which cannot read the iframe's cross-origin URL.
    """
    starts_at = parse_start_time(request.GET.get("event_start_time", ""))
    return render(
        request,
        "public/schedule_thanks.html",
        {
            "invitee_name": request.GET.get("invitee_full_name", "").strip()[:CALENDLY_NAME_MAXLEN],
            # localtime() renders in settings.TIME_ZONE; the template prints the
            # abbreviation alongside it.
            "starts_at": timezone.localtime(starts_at) if starts_at else None,
        },
    )


def booking_thanks(request):
    """One thanks route for both forms.

    `?w=<signed token>` is what a wedding submission redirects with; it turns the page
    into the wedding variant listing the confirmed movements and the quote reference.
    The token is opaque and carries only the lead id — a wedding reaching this page must
    never put the couple's details in a URL. A stale or forged token silently degrades
    to the ordinary thanks page rather than erroring at the finish line.
    """
    lead = _wedding_lead(request.GET.get("w", ""))
    reservations = (
        lead.reservations.prefetch_related("stops").order_by("sort_order", "id") if lead else None
    )
    return render(
        request,
        "public/booking_thanks.html",
        {"wedding_lead": lead, "reservations": reservations},
    )


def _wedding_lead(token: str):
    """The Lead behind a signed wedding token, or None when it can't be trusted."""
    if not token:
        return None
    try:
        return read_wedding_token(token)
    except (BadSignature, SignatureExpired, Lead.DoesNotExist):
        return None


def about(request):
    return render(request, "public/about.html")


def fleet(request):
    return render(request, "public/fleet.html")


def contact(request):
    return render(
        request,
        "public/contact.html",
        {"form": BookingRequestForm(), "occasion_options": occasion_options()},
    )


def privacy(request):
    return render(request, "public/privacy.html")


def services(request):
    return render(request, "public/services.html")


def service_airport(request):
    return render(request, "public/services/airport.html")


def service_corporate(request):
    return render(request, "public/services/corporate.html")


def service_weddings(request):
    return render(request, "public/services/weddings.html")


def service_personal(request):
    return render(request, "public/services/personal.html")


def reviews(request):
    return render(request, "public/reviews.html")


def rates(request):
    return render(request, "public/rates.html")


def blog_index(request):
    return render(request, "public/blog_index.html")


def _post(template):
    """View factory for a static blog post — each post is a plain template
    render, no model/DB involved (content is hand-ported from the WP export).
    """

    def view(request):
        return render(request, f"public/blog/{template}")

    return view


@require_GET
def geocode(request):
    """Unauthenticated LocationIQ autocomplete proxy for the public booking widget.

    Mirrors integrations:geocode_autocomplete's response shape but needs no login;
    the API key stays server-side. Throttled per IP and short-cached to cap spend.
    """
    if _geocode_throttle_exceeded(request):
        return JsonResponse({"results": [], "degraded": False}, status=429)
    q = request.GET.get("q", "")
    if len(q.strip()) < 3:
        return JsonResponse({"results": [], "degraded": False})
    lat, lon = request.GET.get("lat"), request.GET.get("lon")
    # Key bumped to v2: entries cached before airports existed hold LocationIQ-only lists.
    cache_key = "geocode-ac2:" + hashlib.md5(f"{q}:{lat}:{lon}".encode()).hexdigest()
    results = cache.get(cache_key)
    if results is None:
        results = merged_autocomplete(q, lat=lat, lon=lon)
        cache.set(cache_key, results, 300)
    return JsonResponse({"results": results, "degraded": not settings.LOCATIONIQ_API_KEY})


def _locationiq_venue_results(q: str) -> list[dict]:
    """LocationIQ results reshaped into venue-typeahead rows.

    Only reached when the curated directory has nothing: a couple marrying at a barn we
    have never run to must still get a real address, not a dead end. `id` is None, which
    is what tells the form to post `venue_name` instead of `venue_id`.
    """
    rows = []
    for item in locationiq_autocomplete(q)[:VENUE_RESULT_LIMIT]:
        name = item.get("landmark_name") or item.get("line1") or item.get("display_name") or ""
        if not name:
            continue
        rows.append(
            {
                "id": None,
                "name": name[:160],
                "kind": "",
                "address": (item.get("line1") or "")[:255],
                "city": item.get("city") or "",
                "state": (item.get("state") or "")[:2],
                "location_line": ", ".join(
                    p for p in (item.get("line1"), item.get("city"), item.get("state")) if p
                ),
                "vehicle_cap": None,
                "cap_note": "",
                "lead_hits": 0,
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
                "source": "locationiq",
            }
        )
    return rows


@require_GET
def venue_search(request):
    """Typeahead over the curated Venue directory for the wedding intake (spec §6.1).

    Throttled like `public:geocode` — it is an unauthenticated endpoint that can reach a
    paid API — but on its own counter (see `_geocode_throttle_exceeded`).
    """
    if _geocode_throttle_exceeded(request, scope="venues"):
        return JsonResponse({"results": [], "degraded": False}, status=429)
    q = request.GET.get("q", "")
    results = search_venues(q, request.GET.get("kind", ""))
    if not results and len(q.strip()) >= MIN_VENUE_QUERY:
        results = _locationiq_venue_results(q)
    return JsonResponse({"results": results, "degraded": not settings.LOCATIONIQ_API_KEY})


def wedding_plan(request, token: str = ""):
    """The wedding intake (spec 2026-08-30 §5) — seven client-side steps, one POST.

    No wizard routes and no server-side step state: the whole plan lives in the Alpine
    component until the customer confirms the itinerary, exactly like the hero widget's
    two-step disclosure. `token` is the emailed resume link, which rehydrates the saved
    answers and rebuilds that same lead instead of creating a second one.
    """
    lead = None
    if token:
        try:
            lead = read_wedding_token(token)
        except (BadSignature, SignatureExpired, Lead.DoesNotExist) as e:
            raise Http404("That link has expired.") from e

    if request.method == "POST":
        form = WeddingRequestForm(request.POST)
        is_spam = bool(request.POST.get("company"))
        if _booking_throttle_exceeded(request):
            form.add_error(None, "Too many requests — please call (202) 424-2600.")
        elif form.is_valid():
            lead = create_lead_from_wedding(form.cleaned_data, lead=lead)
            _booking_throttle_increment(request)
            send_wedding_confirmation(lead, base_url=settings.PUBLIC_BASE_URL)
            thanks = reverse("public:booking_thanks")
            return redirect(f"{thanks}?w={make_wedding_token(lead)}")
        elif is_spam:
            _booking_throttle_increment(request)
    else:
        form = WeddingRequestForm()

    return render(
        request,
        "public/wedding_plan.html",
        {"form": form, "resume": _resume_state(lead) if token else None, "resume_token": token},
    )


def _resume_state(lead) -> dict:
    """What the Alpine planner needs to open where the couple left off.

    Prefers the payload the lead was created from; falls back to rebuilding the
    itinerary out of the reservations themselves, so a lead an agent has since edited
    (or one created before the payload existed) still resumes instead of 500ing.
    """
    payload = dict(lead.intake_payload or {})
    if not payload.get("legs"):
        payload = {**payload, **_payload_from_reservations(lead)}
    payload["resume"] = True
    payload["quote_no"] = lead.quote_no
    return payload


def _payload_from_reservations(lead) -> dict:
    legs = []
    for res in lead.reservations.prefetch_related("stops").order_by("sort_order", "id"):
        stops = list(res.stops.all())
        if len(stops) < 2:
            continue
        origin, destination = stops[0], stops[-1]
        legs.append(
            {
                "id": f"res-{res.pk}",
                "time": res.pickup_time.strftime("%H:%M") if res.pickup_time else "",
                # The customer-facing title lived only in the browser; rebuild a
                # readable one from the trip itself rather than showing "Movement".
                "title": f"{origin.name} → {destination.name}".strip(" →"),
                "from": origin.name,
                "from_sub": origin.address,
                "to": destination.name,
                "to_sub": destination.address,
                "pax": res.passengers,
                "optional": False,
            }
        )
    first = lead.reservations.order_by("sort_order", "id").first()
    return {
        "name": lead.contact.name,
        "email": lead.contact.email,
        "phone": lead.contact.phone,
        "wedding_date": first.pickup_date.isoformat() if first and first.pickup_date else "",
        "legs": legs,
    }
