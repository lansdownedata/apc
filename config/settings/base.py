"""
Base settings — shared across every environment.

All Pro Charter — Lead Manager (config.settings.base)
Environment-specific overrides live in dev.py / prod.py.
"""

from pathlib import Path

import environ

# config/settings/base.py -> project root is three parents up.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(DJANGO_DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / ".env")

# ---------------------------------------------------------------- core
SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-override-me")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

# ---------------------------------------------------------------- apps
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.sitemaps",
]
THIRD_PARTY_APPS = [
    "rest_framework",
    "anymail",  # ESP email backend (Postmark) — see the email section below
]
# Local apps — the Lead Manager domain (see docs ERD/scope).
LOCAL_APPS = [
    "apps.core",
    "apps.addresses",
    "apps.accounts",
    "apps.contacts",
    "apps.vendors",
    "apps.fleet",
    "apps.leads",
    "apps.reservations",
    "apps.dispatch",
    "apps.payments",
    "apps.messaging",
    "apps.integrations",
    "apps.notifications",
    "apps.portal",
    "apps.settings",
    "apps.public",
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.portal.context_processors.chrome",
                "apps.public.context_processors.canonical",
                "apps.public.context_processors.social_card",
                "apps.public.context_processors.site_settings",
            ],
        },
    },
]

# ---------------------------------------------------------------- database
# Env-driven. Dev falls back to SQLite; prod sets DATABASE_URL=mysql://...
DATABASES = {"default": env.db_url("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}

# ---------------------------------------------------------------- auth
AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Session-auth web portal entry points.
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Staff invite links and password-reset links both use Django's default_token_generator,
# which reads this single global timeout — there is no separate invite setting. 7 days
# gives a new hire a working week to accept.
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24 * 7

# ---------------------------------------------------------------- i18n / tz
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", default="America/New_York")
USE_I18N = True
USE_TZ = True

# Django 6.0 flips URLField's assumed scheme from http to https; opt in now so
# scheme-less URLField input (e.g. Vendor.website) is stored as https:// per that
# forthcoming default, silencing RemovedInDjango60Warning ahead of the upgrade.
FORMS_URLFIELD_ASSUME_HTTPS = True

# ---------------------------------------------------------------- static / media
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------- DRF
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

# ---------------------------------------------------------------- integrations
# Populated from env (.env); blank until real credentials are set.

# Stripe — deposits + balance
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")
STRIPE_DEPOSIT_PCT = env.int("STRIPE_DEPOSIT_PCT", default=50)
BALANCE_CHARGE_DAYS_BEFORE = env.int("BALANCE_CHARGE_DAYS_BEFORE", default=30)

# Podium — messaging (OAuth 2.0 authorization-code; base host api.podium.com)
PODIUM_API_BASE = env("PODIUM_API_BASE", default="https://api.podium.com")
PODIUM_CLIENT_ID = env("PODIUM_CLIENT_ID", default="")
PODIUM_CLIENT_SECRET = env("PODIUM_CLIENT_SECRET", default="")
PODIUM_REDIRECT_URI = env("PODIUM_REDIRECT_URI", default="")
PODIUM_ORGANIZATION_UID = env("PODIUM_ORGANIZATION_UID", default="")
PODIUM_LOCATION_UID = env("PODIUM_LOCATION_UID", default="")
PODIUM_WEBHOOK_SECRET = env("PODIUM_WEBHOOK_SECRET", default="")
PODIUM_SCOPES = env.list(
    "PODIUM_SCOPES",
    default=[
        "read_messages",
        "write_messages",
        "read_contacts",
        "write_contacts",
        "read_reviews",
        "write_reviews",
        "read_locations",
    ],
)

# LimoAnywhere Customer API (docs/la-api/). Preview mode (no sends) unless BOTH LA_ACTIVE is
# true AND the credentials below are set — credentials alone are not consent to book. A send
# creates a real reservation in the client's LimoAnywhere account, and LA_BASE_URL defaults to
# PRODUCTION, so LA_ACTIVE defaults False and must be armed deliberately (Phase 1). While it is
# off, every skipped send is recorded as a PREVIEW ZapEvent and logged — see la_sync._is_preview.
LA_ACTIVE = env.bool("LA_ACTIVE", default=False)
LA_BASE_URL = env("LA_BASE_URL", default="https://api.mylimobiz.com")
LA_CLIENT_ID = env("LA_CLIENT_ID", default="")
LA_CLIENT_SECRET = env("LA_CLIENT_SECRET", default="")
LA_COMPANY_ALIAS = env("LA_COMPANY_ALIAS", default="")
LA_PAYMENT_TYPE_ID = env.int("LA_PAYMENT_TYPE_ID", default=0)  # non-charging type; see smoke test
LA_WEBHOOK_BASE_URL = env("LA_WEBHOOK_BASE_URL", default="")  # e.g. https://<NGROK_HOST>
LOCATIONIQ_API_KEY = env("LOCATIONIQ_API_KEY", default="")

# aviationstack — flight verification (spec 2026-08-29). Blank key = Verify hidden everywhere
# and the endpoint answers 503 `not_configured`. Needs the Basic plan or higher for
# /v1/flightsFuture. Documented as 1 request per 10 s, but a live probe (2026-08-29) hit 429
# with an 11 s gap between calls — treat the real limit as tighter than advertised.
AVIATIONSTACK_API_KEY = env("AVIATIONSTACK_API_KEY", default="")
AVIATIONSTACK_BASE_URL = env("AVIATIONSTACK_BASE_URL", default="https://api.aviationstack.com")

# GNet farm-out gateway (Lansdowne relay in front of the real GNet partner network —
# docs/... GNET-CONNECTION-GUIDE.md §5). Preview mode (no sends) unless BOTH GNET_ACTIVE
# is true AND GNET_API_KEY is set — apps/dispatch/gnet_sync.py gates on this. The gateway
# is LIVE against real GNet — GNET_ACTIVE=True arms real bookings with a real affiliate
# operator, so it defaults to False and must be switched on deliberately.
GNET_ACTIVE = env.bool("GNET_ACTIVE", default=False)
GNET_GATEWAY_URL = env("GNET_GATEWAY_URL", default="https://lansdownedata.com")
GNET_API_KEY = env("GNET_API_KEY", default="")
GNET_CALLBACK_SECRET = env("GNET_CALLBACK_SECRET", default="")

# Public marketing site — client-provided embeds (blank = feature hidden/degraded).
CALENDLY_URL = env("CALENDLY_URL", default="")
# Raw HTML/JS the client pastes from his WeddingWire/The Knot storefront dashboard.
WEDDINGWIRE_WIDGET = env("WEDDINGWIRE_WIDGET", default="")

# Geolocation & address bias
from apps.core.geo import parse_latlon  # noqa: E402 settings-safe (no Django imports)

ADDRESS_BIAS_CENTER = parse_latlon(
    env("ADDRESS_BIAS_CENTER", default="38.9531,-77.4565"), (38.9531, -77.4565)
)  # (lat, lon); default = IAD / DMV service area
ADDRESS_BIAS_RADIUS_DEG = env.float("ADDRESS_BIAS_RADIUS_DEG", default=0.75)

# Cron — HTTP-triggered scheduled jobs (cron-job.org)
CRON_SECRET = env("CRON_SECRET", default="")  # X-Cron-Key header for /cron/<job>/ endpoints

# Inbound capture API
LEAD_INBOUND_API_KEY = env("LEAD_INBOUND_API_KEY", default="")

# ---------------------------------------------------------------- touch-points / messaging
QUOTE_EXPIRY_DAYS_BEFORE_PICKUP = env.int("QUOTE_EXPIRY_DAYS_BEFORE_PICKUP", default=14)
TOUCHPOINTS_ENABLED = env.bool("TOUCHPOINTS_ENABLED", default=False)  # dev safety: off by default
COMPANY_NAME = env("COMPANY_NAME", default="All Pro Charter")
# How many reverse proxies sit in front of the app. 0 = none, so per-IP limits key off
# REMOTE_ADDR. See apps.core.net.client_ip for why the count matters and why reading
# X-Forwarded-For positionally (from the right) is the only safe way to use it.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)

COMPANY_PHONE = env("COMPANY_PHONE", default="")
COMPANY_EMAIL = env("COMPANY_EMAIL", default="reservations@allprocharter.com")
# Daily unpaid-deposit report (cron job `deposit-report`); one email per address.
DEPOSIT_REPORT_EMAILS = env.list("DEPOSIT_REPORT_EMAILS", default=[COMPANY_EMAIL])
# Public https base of the portal, e.g. https://<NGROK_HOST> in dev; required for touch-point
# quote links (distinct from LA_WEBHOOK_BASE_URL, which is LimoAnywhere-specific).
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", default="")

# ---------------------------------------------------------------- email (Postmark / Anymail)
# Sent through Postmark's HTTP API via django-anymail. One credential: the Postmark Server
# API Token (POSTMARK_SERVER_TOKEN in .env). Anymail is a drop-in EMAIL_BACKEND, so the
# send_html_email primitive is unchanged. The allprocharter.com domain is DKIM +
# Return-Path verified in Postmark, so reservations@allprocharter.com sends freely.
# Dev defaults to the console backend (config/settings/dev.py) until EMAIL_BACKEND is
# flipped to the Anymail backend.
EMAIL_BACKEND = env("EMAIL_BACKEND", default="anymail.backends.postmark.EmailBackend")
ANYMAIL = {
    "POSTMARK_SERVER_TOKEN": env("POSTMARK_SERVER_TOKEN", default=""),
    "REQUESTS_TIMEOUT": 10,  # seconds; bound the API call so a hung request can't hang a worker
}
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL", default="All Pro Charter <reservations@allprocharter.com>"
)

# ---------------------------------------------------------------- logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
}
