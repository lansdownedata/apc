"""Deploy-time guards for the integrations app."""

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=True)
def field_encryption_key_set(app_configs, **kwargs):
    """Stored secrets (QuickBooks tokens and company id, LA passwords) are encrypted with
    FIELD_ENCRYPTION_KEY, kept apart from SECRET_KEY as Intuit requires. An Error, so a
    release without a usable key never ships."""
    keys = settings.FIELD_ENCRYPTION_KEY
    for key in keys:
        try:
            Fernet(key)
        except (ValueError, TypeError):
            return [
                Error(
                    "FIELD_ENCRYPTION_KEY holds a value that is not a Fernet key.",
                    hint="Generate one with Fernet.generate_key(); separate keys with commas.",
                    id="integrations.E002",
                )
            ]
    if keys or settings.DEBUG:
        return []
    return [
        Error(
            "FIELD_ENCRYPTION_KEY is unset — stored secrets would be encrypted with a key "
            "derived from SECRET_KEY.",
            hint="Set FIELD_ENCRYPTION_KEY, then run `manage.py reencrypt_fields` once.",
            id="integrations.E001",
        )
    ]


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
