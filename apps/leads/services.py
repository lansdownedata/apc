"""Quote-send orchestration: create the deposit plan + link and deliver it.

External-API calls (Stripe, Podium) are composed here per the services.py rule;
the view stays thin.
"""

from __future__ import annotations

from django.core import signing

_DEPOSIT_SALT = "quote-deposit"


def make_deposit_token(lead) -> str:
    """An opaque, signed token encoding the lead id for the public deposit pages."""
    return signing.dumps({"lead": lead.pk}, salt=_DEPOSIT_SALT)


def read_deposit_token(token: str):
    """Return the Lead for a signed token. Raises BadSignature if forged/tampered."""
    from .models import Lead

    data = signing.loads(token, salt=_DEPOSIT_SALT)
    return Lead.objects.get(pk=data["lead"])
