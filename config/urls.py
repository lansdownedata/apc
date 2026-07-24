"""URL configuration — All Pro Charter Lead Manager."""

from django.conf import settings
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.http import JsonResponse
from django.urls import include, path, re_path
from django.views.generic import TemplateView

from apps.core.cron import run_job
from apps.integrations.views import la_webhook, podium_webhook
from apps.leads.views import pipeline, quote_book, quote_page
from apps.messaging.views import review_list
from apps.payments.views import stripe_webhook
from apps.public.sitemaps import StaticViewSitemap


def healthz(_request):
    return JsonResponse({"status": "ok", "service": "apc-lead-manager"})


staff_patterns = [
    path("", include("apps.portal.urls")),  # dashboard (name="dashboard")
    path("pipeline/", pipeline, name="pipeline"),
    path("leads/", include("apps.leads.urls")),
    path("contacts/", include("apps.contacts.urls")),
    path("vendors/", include("apps.vendors.urls")),
    path("users/", include("apps.accounts.urls")),
    path("settings/", include("apps.settings.urls")),
    path("orders/", include("apps.payments.urls")),
    path("reservations/", include("apps.reservations.urls")),
    path("inbox/", include("apps.messaging.urls")),
    path("reviews/", review_list, name="review_list"),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz, name="health"),
    path("portal/", include("django.contrib.auth.urls")),  # /portal/login/, /portal/logout/, ...
    path("portal/", include(staff_patterns)),  # ← entire staff portal, auth-gated
    # public customer-facing quote page (token-keyed, no login) — unchanged
    path("quote/<str:token>/", quote_page, name="quote_page"),
    path("quote/<str:token>/book/", quote_book, name="quote_book"),
    # integrations + webhooks + cron — unchanged
    path("integrations/", include("apps.integrations.urls")),
    path("webhooks/podium/", podium_webhook, name="podium_webhook"),
    path("webhooks/limoanywhere/<str:token>/", la_webhook, name="la_webhook"),
    path("webhooks/stripe/", stripe_webhook, name="stripe_webhook"),
    path("cron/<slug:job>/", run_job, name="cron_job"),
    # API routes are mounted here as apps expose routers, e.g.:
    # path("api/", include("config.api")),
    # SEO: sitemap.xml + robots.txt for the public marketing site
    path(
        "sitemap.xml",
        sitemap,
        {"sitemaps": {"static": StaticViewSitemap}},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path(
        "robots.txt",
        TemplateView.as_view(template_name="robots.txt", content_type="text/plain"),
        name="robots",
    ),
    # public marketing site at root — added in Task 2 (MUST stay last)
    path("", include("apps.public.urls")),  # public marketing site — keep last
]

# Uploaded media (vehicle-type photos). django.conf.urls.static.static() is a no-op when
# DEBUG is False, and WhiteNoise serves STATIC_ROOT only — so prod needs an explicit route or
# every photo 404s on the customer-facing quote page. prod.py sets SERVE_MEDIA; turn it off
# once a real file server or CDN fronts /media/.
if settings.DEBUG or getattr(settings, "SERVE_MEDIA", False):
    from django.views.static import serve as _serve_media

    urlpatterns += [
        re_path(
            rf"^{settings.MEDIA_URL.lstrip('/')}(?P<path>.*)$",
            _serve_media,
            {"document_root": settings.MEDIA_ROOT},
        )
    ]

if settings.DEBUG:
    try:
        import debug_toolbar  # noqa: F401

        urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
    except ImportError:
        pass
