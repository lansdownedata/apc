"""Production settings."""

from .base import *  # noqa: F401,F403

DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["leads.allprocharter.com"])  # noqa: F405

# Static files via WhiteNoise (installed from requirements/prod.txt)
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")  # noqa: F405

# ---------------------------------------------------------------- media storage
# Heroku's filesystem is ephemeral: every redeploy and dyno restart wipes MEDIA_ROOT, so
# FileSystemStorage silently destroys uploads — vehicle-type photos, and (worse) the vendor
# W-9s and insurance certificates in apps/vendors. Media therefore lives in S3.
#
# The switch is keyed on the bucket name rather than being unconditional so that shipping
# this code before the AWS env vars are set leaves prod on the old behaviour instead of
# failing to boot. Set AWS_STORAGE_BUCKET_NAME to activate S3.
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")  # noqa: F405

if AWS_STORAGE_BUCKET_NAME:
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="us-east-1")  # noqa: F405
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")  # noqa: F405
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")  # noqa: F405
    # None defers to the bucket's Object Ownership setting. Never "public-read": the bucket
    # holds vendor tax documents, so objects are reached only via expiring signed URLs.
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = True
    AWS_QUERYSTRING_EXPIRE = env.int("AWS_QUERYSTRING_EXPIRE", default=3600)  # noqa: F405
    AWS_S3_SIGNATURE_VERSION = "s3v4"
    # Django already suffixes clashing names; letting S3 overwrite would defeat that and let
    # one vendor's w9.pdf replace another's.
    AWS_S3_FILE_OVERWRITE = False
    _MEDIA_STORAGE = {"BACKEND": "storages.backends.s3.S3Storage"}
    # S3 serves the objects directly; routing /media/ through the dyno would be dead weight.
    SERVE_MEDIA = False
else:
    _MEDIA_STORAGE = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    # Without a bucket the app must keep serving MEDIA_ROOT itself (see config/urls.py) or
    # every photo 404s on the customer-facing quote page. Uploads are still lost on redeploy.
    SERVE_MEDIA = env.bool("SERVE_MEDIA", default=True)  # noqa: F405

STORAGES = {
    "default": _MEDIA_STORAGE,
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Security hardening (served behind HTTPS / a proxy)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Heroku's router is exactly one hop and appends the connecting peer to X-Forwarded-For.
# Without this every visitor presents as the router and they all share one per-IP throttle
# bucket — which silently rejected real booking requests. Overridable for another host.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=1)  # noqa: F405
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
