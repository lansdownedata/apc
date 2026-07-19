"""Production settings."""

from .base import *  # noqa: F401,F403

DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["leads.allprocharter.com"])  # noqa: F405

# Static files via WhiteNoise (installed from requirements/prod.txt)
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")  # noqa: F405
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# WhiteNoise serves STATIC_ROOT only, so without this nothing serves MEDIA_URL in prod and
# every uploaded vehicle photo 404s on the customer-facing quote page. SERVE_MEDIA lets the
# app serve MEDIA_ROOT itself (see config/urls.py) — correct at this volume (a handful of
# vehicle photos), but set SERVE_MEDIA=False and add an nginx/CDN alias for /media/ if
# traffic grows. Either way MEDIA_ROOT needs a persistent volume: an ephemeral filesystem
# loses every upload on redeploy.
SERVE_MEDIA = env.bool("SERVE_MEDIA", default=True)  # noqa: F405

# Security hardening (served behind HTTPS / a proxy)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
