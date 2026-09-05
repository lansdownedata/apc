"""Deploy-time guards for the payments app."""

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def stripe_webhook_secret_set(app_configs, **kwargs):
    """A blank STRIPE_WEBHOOK_SECRET fails signature verification on every Stripe event,
    silently, and webhooks are the only path that reconciles money we didn't hear about
    inline. Turn that into a failed release instead of payments that quietly don't land.

    Since APC-26 the endpoint needs **two** events, not one:
    `payment_intent.amount_capturable_updated` (the deposit hold at checkout — under manual
    capture `succeeded` no longer fires there) and `payment_intent.succeeded` (the capture,
    days later). A subscription missing the first is not detectable from here — the secret
    is set and signatures verify — so it shows up as engaged orders that never appear.
    `reconcile-payments` is the backstop; the hint below is the prevention."""
    if settings.DEBUG or settings.STRIPE_WEBHOOK_SECRET:
        return []
    return [
        Error(
            "STRIPE_WEBHOOK_SECRET is unset — every Stripe webhook will fail signature "
            "verification, so no payment will reconcile.",
            hint=(
                "Set it from the endpoint's signing secret in the Stripe Dashboard, in the "
                "same account and mode as STRIPE_SECRET_KEY. While you're there, confirm the "
                "endpoint subscribes to payment_intent.amount_capturable_updated AND "
                "payment_intent.succeeded — without the first, deposits authorize and the "
                "order never reaches the confirmation queue."
            ),
            id="payments.E001",
        )
    ]
