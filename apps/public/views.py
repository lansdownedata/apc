import hashlib

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from apps.integrations.geocoding import merged_autocomplete

from .forms import OCCASION_CHOICES, BookingRequestForm
from .services import create_lead_from_booking

# Coarse per-IP throttle for the unauthenticated bookings POST — caps mass Lead
# creation from scripted/bot traffic that skips the honeypot field entirely.
BOOKING_THROTTLE_LIMIT = 5
BOOKING_THROTTLE_WINDOW_SECONDS = 60 * 60  # 1 hour


def _booking_throttle_key(request) -> str:
    """Rolling-window per-IP counter key using Django's default cache (LocMemCache
    in dev — per-process, so this throttle is per-worker until a shared cache like
    Redis/Memcached is configured for prod; acceptable baseline for now).

    NOTE: REMOTE_ADDR is trusted as-is; behind a reverse proxy in prod this needs
    X-Forwarded-For handling (e.g. via django-xff or a trusted-proxy header parse)
    at deploy time.
    """
    ip = request.META.get("REMOTE_ADDR", "unknown")
    return f"bookings-throttle:{ip}"


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


def _geocode_throttle_exceeded(request) -> bool:
    ip = request.META.get("REMOTE_ADDR", "unknown")
    key = f"geocode-throttle:{ip}"
    count = cache.get(key, 0) + 1
    cache.set(key, count, GEOCODE_THROTTLE_WINDOW_SECONDS)
    return count > GEOCODE_THROTTLE_LIMIT


def home(request):
    return render(
        request,
        "public/home.html",
        {"form": BookingRequestForm(), "occasion_options": OCCASION_CHOICES},
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
        form = BookingRequestForm()
    return render(
        request, "public/bookings.html", {"form": form, "occasion_options": OCCASION_CHOICES}
    )


def booking_thanks(request):
    return render(request, "public/booking_thanks.html")


def about(request):
    return render(request, "public/about.html")


def fleet(request):
    return render(request, "public/fleet.html")


def contact(request):
    return render(
        request,
        "public/contact.html",
        {"form": BookingRequestForm(), "occasion_options": OCCASION_CHOICES},
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
