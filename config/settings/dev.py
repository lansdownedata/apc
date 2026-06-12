"""Local development settings."""

from .base import *  # noqa: F401,F403

DEBUG = True

# Allow the ngrok tunnel host (set NGROK_HOST in .env) so Podium OAuth callbacks
# and webhooks reach the local server over HTTPS.
NGROK_HOST = env("NGROK_HOST", default="")  # noqa: F405
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", *([NGROK_HOST] if NGROK_HOST else [])]
CSRF_TRUSTED_ORIGINS = [f"https://{NGROK_HOST}"] if NGROK_HOST else []

# django-debug-toolbar
INSTALLED_APPS += ["debug_toolbar"]  # noqa: F405
MIDDLEWARE = ["debug_toolbar.middleware.DebugToolbarMiddleware", *MIDDLEWARE]  # noqa: F405
INTERNAL_IPS = ["127.0.0.1"]

# Run Celery tasks inline in dev — no broker/worker needed locally.
CELERY_TASK_ALWAYS_EAGER = True

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
