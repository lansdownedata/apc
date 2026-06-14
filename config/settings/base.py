"""
Base settings — shared across every environment.

All Pro Charter — Lead Manager (config.settings.base)
Environment-specific overrides live in dev.py / prod.py.
"""

from pathlib import Path

import environ
from celery.schedules import crontab

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
]
THIRD_PARTY_APPS = [
    "rest_framework",
]
# Local apps — the Lead Manager domain (see docs ERD/scope).
LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.contacts",
    "apps.leads",
    "apps.reservations",
    "apps.payments",
    "apps.messaging",
    "apps.integrations",
    "apps.notifications",
    "apps.portal",
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

# ---------------------------------------------------------------- i18n / tz
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", default="America/New_York")
USE_I18N = True
USE_TZ = True

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

# ---------------------------------------------------------------- Celery
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_TRACK_STARTED = True
CELERY_BEAT_SCHEDULE = {
    "charge-due-balances": {
        "task": "apps.payments.tasks.charge_due_balances",
        "schedule": crontab(hour=6, minute=0),  # daily at 6am — balance charges due
    },
    "recognize-due-revenue": {
        "task": "apps.payments.tasks.recognize_due_revenue",
        "schedule": crontab(hour=2, minute=0),  # nightly — recognize completed trips
    },
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

# Zapier → LimoAnywhere
ZAPIER_LEAD_HOOK_URL = env("ZAPIER_LEAD_HOOK_URL", default="")

# Inbound capture API
LEAD_INBOUND_API_KEY = env("LEAD_INBOUND_API_KEY", default="")

# ---------------------------------------------------------------- logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", default="INFO")},
}
