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


@register(Tags.security, deploy=True)
def gnet_api_key_set_for_farm_in(app_configs, **kwargs):
    """With the farm-in receiver on, a blank GNET_API_KEY means no decision reaches GNet:
    accept is refused, and a decline's REJECT fails at once."""
    if not settings.GNET_FARM_IN_ENABLED or settings.GNET_API_KEY:
        return []
    return [
        Warning(
            "GNET_FARM_IN_ENABLED is on but GNET_API_KEY is unset — farm-ins cannot be "
            "accepted and no status reaches GNet.",
            hint="Set GNET_API_KEY to the gateway API key, or turn GNET_FARM_IN_ENABLED off.",
            id="integrations.W002",
        )
    ]
