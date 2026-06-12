"""URL configuration — All Pro Charter Lead Manager."""

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.integrations.views import podium_webhook
from apps.payments.views import stripe_webhook


def healthz(_request):
    return JsonResponse({"status": "ok", "service": "apc-lead-manager"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="health"),
    path("integrations/", include("apps.integrations.urls")),
    path("webhooks/podium/", podium_webhook, name="podium_webhook"),
    path("webhooks/stripe/", stripe_webhook, name="stripe_webhook"),
    # API routes are mounted here as apps expose routers, e.g.:
    # path("api/", include("config.api")),
]

if settings.DEBUG:
    try:
        import debug_toolbar  # noqa: F401

        urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
    except ImportError:
        pass
