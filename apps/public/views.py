from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse

from .forms import SERVICE_TYPE_CHOICES, BookingRequestForm
from .services import create_lead_from_booking

# Coarse per-IP throttle for the unauthenticated bookings POST — caps mass Lead
# creation from scripted/bot traffic that skips the honeypot field entirely.
BOOKING_THROTTLE_LIMIT = 5
BOOKING_THROTTLE_WINDOW_SECONDS = 60 * 60  # 1 hour


def _booking_throttle_exceeded(request) -> bool:
    """Rolling-window per-IP counter using Django's default cache (LocMemCache in
    dev — per-process, so this throttle is per-worker until a shared cache like
    Redis/Memcached is configured for prod; acceptable baseline for now).

    NOTE: REMOTE_ADDR is trusted as-is; behind a reverse proxy in prod this needs
    X-Forwarded-For handling (e.g. via django-xff or a trusted-proxy header parse)
    at deploy time.
    """
    ip = request.META.get("REMOTE_ADDR", "unknown")
    key = f"bookings-throttle:{ip}"
    count = cache.get(key, 0)
    if count >= BOOKING_THROTTLE_LIMIT:
        return True
    cache.set(key, count + 1, BOOKING_THROTTLE_WINDOW_SECONDS)
    return False


def home(request):
    return render(
        request,
        "public/home.html",
        {"form": BookingRequestForm(), "service_options": SERVICE_TYPE_CHOICES},
    )


def bookings(request):
    if request.method == "POST":
        form = BookingRequestForm(request.POST)
        if _booking_throttle_exceeded(request):
            form.add_error(None, "Too many requests — please call (202) 424-2600.")
        elif form.is_valid():
            create_lead_from_booking(form.cleaned_data)
            return redirect("public:booking_thanks")
    else:
        form = BookingRequestForm()
    return render(
        request, "public/bookings.html", {"form": form, "service_options": SERVICE_TYPE_CHOICES}
    )


def booking_thanks(request):
    return render(request, "public/booking_thanks.html")


def about(request):
    return render(request, "public/about.html")


def fleet(request):
    return render(request, "public/fleet.html")


def contact(request):
    canonical_url = request.build_absolute_uri(reverse("public:contact"))
    return render(
        request,
        "public/contact.html",
        {
            "form": BookingRequestForm(),
            "service_options": SERVICE_TYPE_CHOICES,
            "canonical_url": canonical_url,
        },
    )


def privacy(request):
    return render(request, "public/privacy.html")
