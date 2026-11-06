"""Deploy-time guards for the integrations app."""

from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def gnet_callback_secret_set(app_configs, **kwargs):
    """A blank GNET_CALLBACK_SECRET makes `/api/gnet/farm-in` refuse every delivery.
    A Warning, not an Error: the farm-out callback still works without it."""
    if settings.DEBUG or settings.GNET_CALLBACK_SECRET:
        return []
    return [
        Warning(
            "GNET_CALLBACK_SECRET is unset — every GNet farm-in is refused and the "
            "farm-out callback accepts unsigned requests.",
            hint="Set GNET_CALLBACK_SECRET to the shared secret configured in the gateway.",
            id="integrations.W001",
        )
    ]
