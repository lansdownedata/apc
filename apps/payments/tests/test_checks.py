"""`payments.E001` — a blank STRIPE_WEBHOOK_SECRET must fail the deploy, not the payments.

After the card-only work `payment_intent.succeeded` is the only webhook success path, so an
unset secret means every event 400s on signature verify and nothing reconciles — silently.
`manage.py check --deploy` on the Procfile release line turns that into a failed release.
"""

import pytest

from apps.payments.checks import stripe_webhook_secret_set

pytestmark = pytest.mark.django_db


def test_flags_a_blank_secret_on_deploy(settings):
    settings.DEBUG = False
    settings.STRIPE_WEBHOOK_SECRET = ""
    errors = stripe_webhook_secret_set(app_configs=None)
    assert [e.id for e in errors] == ["payments.E001"]


def test_no_error_when_the_secret_is_set(settings):
    settings.DEBUG = False
    settings.STRIPE_WEBHOOK_SECRET = "whsec_live_x"
    assert stripe_webhook_secret_set(app_configs=None) == []


def test_no_error_in_debug(settings):
    """Dev must stay runnable without the secret."""
    settings.DEBUG = True
    settings.STRIPE_WEBHOOK_SECRET = ""
    assert stripe_webhook_secret_set(app_configs=None) == []
