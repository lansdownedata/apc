"""Deploy-time guards for the payments app."""

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def stripe_webhook_secret_set(app_configs, **kwargs):
    """A blank STRIPE_WEBHOOK_SECRET fails signature verification on every Stripe event,
    silently — and `payment_intent.succeeded` is now the only webhook success path. Turn
    that into a failed release instead of payments that quietly do not reconcile."""
    if settings.DEBUG or settings.STRIPE_WEBHOOK_SECRET:
        return []
    return [
        Error(
            "STRIPE_WEBHOOK_SECRET is unset — every Stripe webhook will fail signature "
            "verification, so no payment will reconcile.",
            hint=(
                "Set it from the endpoint's signing secret in the Stripe Dashboard, in the "
                "same account and mode as STRIPE_SECRET_KEY."
            ),
            id="payments.E001",
        )
    ]
