"""Deploy-time guards for the accounts app."""

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def two_factor_on_in_production(app_configs, **kwargs) -> list[Error]:
    """`TWO_FACTOR_ENABLED` off means a password alone signs staff and planners in. It exists
    for local development and the e2e rig only, so a release with it off must not ship."""
    if settings.DEBUG or settings.TWO_FACTOR_ENABLED:
        return []
    return [
        Error(
            "TWO_FACTOR_ENABLED is off — a password alone signs staff and planners in.",
            hint="Unset TWO_FACTOR_ENABLED (it defaults to on) or set it to True.",
            id="accounts.E001",
        )
    ]
