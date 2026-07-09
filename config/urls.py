"""URL configuration — All Pro Charter Lead Manager."""

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.core.cron import run_job
from apps.integrations.views import podium_webhook
from apps.payments.views import stripe_webhook


def healthz(_request):
    return JsonResponse({"status": "ok", "service": "apc-lead-manager"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="health"),
    # --- web portal (session auth) ---
    path("", include("django.contrib.auth.urls")),  # login/, logout/, password_*
    path("", include("apps.portal.urls")),  # dashboard (home)
    path("leads/", include("apps.leads.urls")),
    path("users/", include("apps.accounts.urls")),
    path("orders/", include("apps.payments.urls")),
    path("reservations/", include("apps.reservations.urls")),
    # --- integrations + webhooks ---
    path("integrations/", include("apps.integrations.urls")),
    path("webhooks/podium/", podium_webhook, name="podium_webhook"),
    path("webhooks/stripe/", stripe_webhook, name="stripe_webhook"),
    path("cron/<slug:job>/", run_job, name="cron_job"),
    # API routes are mounted here as apps expose routers, e.g.:
    # path("api/", include("config.api")),
]

if settings.DEBUG:
    try:
        import debug_toolbar  # noqa: F401

        urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
    except ImportError:
        pass
