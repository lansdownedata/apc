import hashlib
import logging
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC as dt_timezone_utc
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.addresses.search import (
    MIN_VENUE_QUERY,
    VENUE_RESULT_LIMIT,
    VENUE_STRONG_MATCH_TARGET,
    search_venues,
)
from apps.core.net import client_ip
from apps.core.phone import to_e164
from apps.integrations import calendly
from apps.integrations.calendly import parse_start_time
from apps.integrations.geocoding import autocomplete as locationiq_autocomplete
from apps.integrations.geocoding import merged_autocomplete
from apps.leads.models import Lead
from apps.reservations import groups

from .forms import (
    BookingRequestForm,
    WeddingRequestForm,
    occasion_options,
    service_slug_ids,
    service_type_for_slug,
)
from .models import BookingConsent, SlotHold
from .services import (
    create_lead_from_booking,
    create_lead_from_wedding,
    make_booking_token,
    make_wedding_token,
    read_booking_token,
    read_wedding_token,
    send_wedding_confirmation,
)

logger = logging.getLogger(__name__)

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
    booked = _booking_from_token(request.GET.get("b", ""))
    if booked is not None:
        return render(request, "public/schedule_thanks.html", booked)

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


def _booking_from_token(token: str) -> dict | None:
    """Context for a booking made through our own form, or None to fall through.

    The token is the trusted half of this page: a booking that came through us gets a
    real confirmation, where the query-string values Calendly appends are forgeable
    and display-only. A tampered or expired token degrades to the generic page rather
    than erroring — this is unauthenticated, and a broken link is not an incident.

    The time renders in the zone the VISITOR booked in, which is the one place this
    site deliberately does not use TIME_ZONE. The confirmation has to agree with the
    slot they clicked and with the calendar invite Calendly sent them, both of which
    are in their zone; 2:30 PM EDT shown to someone who booked 11:30 AM PDT reads as a
    different appointment.
    """
    if not token:
        return None
    try:
        payload = read_booking_token(token)
    except (BadSignature, SignatureExpired):
        return None

    starts_at = parse_start_time(payload.get("start_time", ""))
    if starts_at is not None:
        try:
            starts_at = starts_at.astimezone(ZoneInfo(payload.get("timezone") or ""))
        except (ZoneInfoNotFoundError, ValueError):
            # A zone we cannot resolve is not worth a 500 on a confirmation page.
            starts_at = timezone.localtime(starts_at)
    return {
        "invitee_name": (payload.get("name") or "").strip()[:CALENDLY_NAME_MAXLEN],
        "starts_at": starts_at,
    }


def booking_thanks(request):
    """One thanks route for both forms.

    `?w=<signed token>` is what a wedding submission redirects with; it turns the page
    into the wedding variant listing the confirmed movements and the quote reference.
    The token is opaque and carries only the lead id — a wedding reaching this page must
    never put the couple's details in a URL. A stale or forged token silently degrades
    to the ordinary thanks page rather than erroring at the finish line.
    """
    lead = _wedding_lead(request.GET.get("w", ""))
    # Movements, not vehicles: a 105-guest run takes two coaches, but the couple asked for
    # one movement carrying 105 guests and that is what they must read back (APC-14). The
    # coach count rides along as ours to arrange, never as a split of their guest list.
    lines = (
        groups.as_lines(lead.reservations.prefetch_related("stops").order_by("sort_order", "id"))
        if lead
        else None
    )
    return render(
        request,
        "public/booking_thanks.html",
        {"wedding_lead": lead, "movements": lines},
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


def _strong_directory_matches(q: str, rows: list[dict]) -> int:
    """How many directory rows matched on the venue *name* (not just the town).

    A city-only hit is real but weak — "Leesburg" surfaces a dozen rows and none of
    them is what the couple typed. Only name matches count toward "the directory has
    this covered, skip the paid lookup".
    """
    needle = q.strip().lower()
    return sum(1 for r in rows if needle in (r.get("name") or "").lower())


def _merge_locationiq_venues(q: str, directory: list[dict]) -> list[dict]:
    """Directory rows first, then LocationIQ rows the directory didn't already have.

    De-duplicated on (name, city) case-insensitively so a venue we curate doesn't also
    appear as a raw geocoder hit; capped at VENUE_RESULT_LIMIT.
    """
    seen = {
        ((r.get("name") or "").strip().lower(), (r.get("city") or "").strip().lower())
        for r in directory
    }
    merged = list(directory)
    for row in _locationiq_venue_results(q):
        if len(merged) >= VENUE_RESULT_LIMIT:
            break
        key = ((row.get("name") or "").strip().lower(), (row.get("city") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged[:VENUE_RESULT_LIMIT]


@require_GET
def venue_search(request):
    """Typeahead over the curated Venue directory for the wedding intake (spec §6.1).

    The directory is the primary source; LocationIQ is merged in — not used as an
    either/or fallback — whenever the directory's name matches are thin, so a venue we
    have never run to (Congressional, District Winery, …) still surfaces alongside the
    curated rows (reconciliation §A1).

    Throttled like `public:geocode` — it is an unauthenticated endpoint that can reach a
    paid API — but on its own counter (see `_geocode_throttle_exceeded`).
    """
    if _geocode_throttle_exceeded(request, scope="venues"):
        return JsonResponse({"results": [], "degraded": False}, status=429)
    q = request.GET.get("q", "")
    kind = request.GET.get("kind", "")
    results = search_venues(q, kind)
    if len(q.strip()) >= MIN_VENUE_QUERY:
        if kind:
            # The hotel / ceremony-site typeaheads stay single-kind: only reach for
            # LocationIQ when the directory has nothing, and never mix in unkinded rows.
            if not results:
                results = _locationiq_venue_results(q)
        elif _strong_directory_matches(q, results) < VENUE_STRONG_MATCH_TARGET:
            results = _merge_locationiq_venues(q, results)
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


# --- our own booking UI over Calendly -------------------------------------------

# Calendly caps a single availability call at 7 days, so a longer view is several
# calls. Capped so a hand-built ?days=3650 cannot turn one unauthenticated GET into
# hundreds of upstream requests against a rate limit that is tighter than documented.
# The panel opens with one rolling fetch wide enough to find the first month that has
# anything in it, so it never pays for a round trip that only proves a month is spent.
SLOT_DAYS_DEFAULT = 45
SLOT_DAYS_MAX = 62
# Enough to put a month's five chunks in flight at once without hammering an API whose
# rate limits are tighter than documented.
SLOT_FETCH_WORKERS = 5
# Calendly rejects a start_time in the past, and `now` is already past by the time the
# request reaches them.
SLOT_WINDOW_LEAD = timedelta(minutes=2)


def _slot_days(raw: str) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return SLOT_DAYS_DEFAULT
    return max(1, min(days, SLOT_DAYS_MAX))


SLOT_GENERATION_KEY = "calendly-slots:generation"


def _slot_generation() -> int:
    return cache.get(SLOT_GENERATION_KEY) or 0


def _expire_slots() -> None:
    """Drop every cached day-range at once.

    Called when Calendly tells us a slot we were offering is already filled: the
    cached grid demonstrably contains a slot that is gone, and which `days` range a
    given visitor asked for is not knowable from here. Bumping a generation
    invalidates all of them without enumerating keys.
    """
    cache.set(SLOT_GENERATION_KEY, _slot_generation() + 1, None)


# How far ahead the month arrows may reach. Calendly's own event types stop offering
# dates long before this; the cap exists so a hand-built ?month=2999-01 cannot fan out.
SLOT_MONTHS_AHEAD = 12
# A month's slots are attributed to LOCAL days by the browser, and a visitor may be up
# to ~14 hours either side of UTC. Padding a day at each end means the 1st and the 31st
# are complete for everyone rather than losing an evening depending on who is looking.
MONTH_EDGE_PADDING = timedelta(days=1)


def _month_window(raw: str) -> tuple[datetime, datetime] | None:
    """(start, end) for a `YYYY-MM` request, or None if it isn't one.

    Returns a start later than the end for a month that has already passed — callers
    treat that as "nothing to fetch" rather than asking Calendly about the past.
    """
    try:
        year, month = (int(part) for part in raw.split("-", 1))
        first = datetime(year, month, 1, tzinfo=dt_timezone_utc)
    except (TypeError, ValueError):
        return None
    days_in_month = monthrange(year, month)[1]
    start = first - MONTH_EDGE_PADDING
    end = first + timedelta(days=days_in_month) + MONTH_EDGE_PADDING
    # Calendly rejects a start_time in the past, so a month already under way starts now.
    return max(start, timezone.now() + SLOT_WINDOW_LEAD), end


def _month_is_reachable(start: datetime, end: datetime) -> bool:
    horizon = timezone.now() + timedelta(days=31 * SLOT_MONTHS_AHEAD)
    return start < end and start < horizon


def _fetch_window(start: datetime, end: datetime, *, key: str) -> list[str]:
    """Every bookable start time in [start, end), as raw Calendly timestamps.

    Calendly caps one call at 7 days, so anything longer is several contiguous calls
    concatenated. Cached as-is and WITHOUT hold state: holds change between one poll
    and the next, so they are overlaid per request rather than baked into the cache.
    """
    cache_key = f"calendly-slots:v2:{_slot_generation()}:{key}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    chunks = []
    window_start = start
    while window_start < end:
        window_end = min(window_start + calendly.MAX_SLOT_RANGE, end)
        chunks.append((window_start, window_end))
        window_start = window_end

    # Calendly's 7-day cap means a month is five calls, and run end to end that was
    # three seconds of a visitor watching a spinner. The chunks do not depend on each
    # other, so they go out together. `map` preserves order and re-raises the first
    # exception, so a CalendlyAPIError still reaches the 503 handler intact.
    starts: list[str] = []
    with ThreadPoolExecutor(max_workers=SLOT_FETCH_WORKERS) as pool:
        for slots in pool.map(lambda w: calendly.available_times(start=w[0], end=w[1]), chunks):
            for slot in slots:
                if slot.get("status") and slot["status"] != "available":
                    continue
                if slot.get("start_time"):
                    starts.append(slot["start_time"])

    cache.set(cache_key, starts, settings.CALENDLY_SLOT_CACHE_SECONDS)
    return starts


def _fetch_slots(days: int) -> list[str]:
    """The rolling default window, used when no month is asked for."""
    start = timezone.now() + SLOT_WINDOW_LEAD
    return _fetch_window(start, start + timedelta(days=days), key=f"d{days}")


@require_GET
def schedule_slots(request):
    """Bookable slots plus the live custom-question list, for our own booking form.

    Times go out as UTC ISO and nothing else. The visitor is not necessarily in
    Eastern, the server cannot know their zone, and a formatted wall-clock time from
    here would be wrong for anyone outside it — the browser localises.

    A 503 rather than a 500 on any upstream trouble: the UI treats it as a signal to
    fall back to the Calendly popup, which needs a parseable answer.
    """
    raw_month = request.GET.get("month", "").strip()
    window = _month_window(raw_month) if raw_month else None
    days = _slot_days(request.GET.get("days", ""))
    try:
        if window is None:
            starts = _fetch_slots(days)
        elif _month_is_reachable(*window):
            starts = _fetch_window(*window, key=raw_month)
        else:
            # A month behind us or past the horizon. Not an error — the calendar just
            # has nothing there — and emphatically not worth an upstream call.
            starts = []
        questions = calendly.event_type_questions()
    except (calendly.CalendlyAPIError, calendly.CalendlyNotConfigured) as exc:
        logger.warning("Calendly availability unavailable: %s", exc)
        return JsonResponse(
            {"slots": [], "questions": [], "error": "Availability is unavailable right now."},
            status=503,
        )

    return JsonResponse(
        {
            "slots": _with_holds(starts),
            "questions": questions,
            "month": raw_month if window is not None else "",
            "error": "",
        }
    )


def _with_holds(starts: list[str]) -> list[dict]:
    """Slot timestamps annotated with live hold state, newest hold data every time."""
    held = SlotHold.objects.held_start_times()
    slots = []
    for raw in starts:
        moment = parse_start_time(raw)
        if moment is None:
            continue
        slots.append(
            {
                "start": moment.astimezone(dt_timezone_utc).isoformat().replace("+00:00", "Z"),
                "held": moment in held,
            }
        )
    return slots


# The session remembers what it has already booked, keyed by slot. Without it a
# double-click books twice: claim() deliberately lets a session re-take its own hold,
# so nothing upstream of the Calendly call stops the second submit.
BOOKED_SESSION_KEY = "calendly_booked"


def _slots_for_refresh(raw_month: str) -> list[dict]:
    """The grid to hand back with a 409, in the same shape the caller is looking at.

    The booking POST carries the month the calendar is showing so a lost race can
    re-render it in place. Without that we would answer a month view with a rolling
    two-week window and the calendar would quietly lose most of its month.
    """
    window = _month_window(raw_month) if raw_month else None
    if window is not None and _month_is_reachable(*window):
        return _with_holds(_fetch_window(*window, key=raw_month))
    return _with_holds(_fetch_slots(SLOT_DAYS_DEFAULT))


def _booking_errors(*, name: str, email: str, phone: str, start) -> dict[str, str]:
    errors = {}
    if not name:
        errors["name"] = "Tell us your name."
    if not email or "@" not in email:
        errors["email"] = "We need a valid email for the calendar invite."
    if not phone:
        # Phone becomes location.location — the number the host dials. Calendly's own
        # complaint about a bad one comes back as a location failure, which names
        # nothing the visitor can act on, so it is caught here instead.
        errors["phone"] = "We call you at this number, so we need a valid one."
    if start is None:
        errors["start_time"] = "Pick a time."
    elif start <= timezone.now():
        errors["start_time"] = "That time has passed — pick another."
    return errors


def _collect_answers(request, questions: list[dict]) -> tuple[list[dict], list[str]]:
    """Answers in Calendly's shape, plus the names of any required ones left blank.

    Driven entirely by the live question list. `position` is what the API matches on
    and it is the client's to reorder in Calendly whenever he likes — a hardcoded
    position keeps working right up until he does, then silently posts answers to the
    wrong questions.
    """
    answers, missing = [], []
    for question in questions:
        position = question.get("position")
        value = request.POST.get(f"q{position}", "").strip()
        if not value:
            if question.get("required"):
                missing.append(question.get("name", ""))
            continue
        answers.append(
            {"question": question.get("name", ""), "answer": value, "position": position}
        )
    return answers, missing


@require_POST
def schedule_book(request):
    """Book the discovery call the visitor picked, through Calendly's API.

    Creates NO Lead and NO Contact. `invitee.created` already does that, idempotently,
    with the attach heuristic and reschedule correlation — and it fires for an API
    booking exactly as for one made in Calendly's own UI (probed against the live
    account, 2026-08-31). Doing it here as well would race the webhook and duplicate.
    """
    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key

    name = request.POST.get("name", "").strip()[:CALENDLY_NAME_MAXLEN]
    email = request.POST.get("email", "").strip()
    phone = to_e164(request.POST.get("phone", ""))
    visitor_tz = request.POST.get("timezone", "").strip() or settings.TIME_ZONE
    start = parse_start_time(request.POST.get("start_time", ""))

    errors = _booking_errors(name=name, email=email, phone=phone, start=start)
    if errors:
        return JsonResponse({"errors": errors}, status=400)

    start_iso = start.astimezone(dt_timezone_utc).isoformat().replace("+00:00", "Z")

    # Already booked by this very session — a double-click, not a second booking.
    booked = request.session.get(BOOKED_SESSION_KEY) or {}
    if start_iso in booked:
        return JsonResponse({"redirect": booked[start_iso]})

    # Calendly's SMS-reminder prompt stays switched on for this event type; we fill it
    # from the one phone field we already validate rather than asking twice. Only ever
    # when explicitly opted in — an unticked box sends nothing at all, and the visitor
    # still gets the call, which is the service they actually asked for.
    sms_consent = request.POST.get("sms_consent", "").strip() not in ("", "0", "false")

    try:
        questions = calendly.event_type_questions()
    except (calendly.CalendlyAPIError, calendly.CalendlyNotConfigured) as exc:
        logger.warning("Calendly questions unavailable: %s", exc)
        return JsonResponse({"error": "We couldn't reach the calendar."}, status=502)

    answers, missing = _collect_answers(request, questions)
    if missing:
        return JsonResponse(
            {"errors": {"questions": [f"{n} is required." for n in missing]}}, status=400
        )

    if SlotHold.objects.claim(start, session_key) is None:
        return JsonResponse(
            {
                "code": "slot_taken",
                "error": "Someone else is booking that time right now.",
                "slots": _slots_for_refresh(request.POST.get("month", "").strip()),
            },
            status=409,
        )

    try:
        calendly.create_invitee(
            start_time=start_iso,
            name=name,
            email=email,
            timezone=visitor_tz,
            phone=phone,
            answers=answers,
            text_reminder_number=phone if sms_consent else "",
        )
    except calendly.CalendlySlotTaken:
        # The authoritative signal, and the only thing that earns a 409: the slot went
        # somewhere we cannot see — calendly.com, or the host's own calendar.
        SlotHold.objects.release(start, session_key)
        _expire_slots()
        slots = []
        try:
            slots = _slots_for_refresh(request.POST.get("month", "").strip())
        except (calendly.CalendlyAPIError, calendly.CalendlyNotConfigured):
            logger.warning("Could not refresh slots after a lost race.")
        return JsonResponse(
            {
                "code": "slot_taken",
                "error": "That time just went — here are the next available.",
                "slots": slots,
            },
            status=409,
        )
    except (calendly.CalendlyAPIError, calendly.CalendlyNotConfigured) as exc:
        # Not an availability problem, so the hold must not be left behind greying the
        # slot out for everyone else for the rest of the hold window.
        SlotHold.objects.release(start, session_key)
        logger.exception("Calendly booking failed: %s", exc)
        return JsonResponse({"error": "We couldn't complete that booking."}, status=502)

    SlotHold.objects.release(start, session_key)
    if sms_consent:
        BookingConsent.objects.record(
            name=name,
            email=email,
            phone=phone,
            start_time=start,
            ip_address=client_ip(request),
        )
    token = make_booking_token(name=name, start_time=start_iso, timezone=visitor_tz)
    redirect_to = f"{reverse('public:schedule_thanks')}?b={token}"
    request.session[BOOKED_SESSION_KEY] = {**booked, start_iso: redirect_to}
    return JsonResponse({"redirect": redirect_to})
