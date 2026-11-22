"""Signed, expiring links for customer invites and password reset.

Django's reset-token machinery over a `CustomerAccount`, as `apps/planners/tokens.py`. The
hash covers the password (so a link dies once a password is set), the last sign-in, when the
invite was sent (so Resend kills the previous link), the revocation stamp (so Revoke kills
every outstanding one) and the contact's live email (so a link sent to an old address dies
when staff change it). Invite links live `PASSWORD_RESET_TIMEOUT`; password-reset links have
their own salt, so one can never stand in for the other, and their own life of 7 days.
"""

from datetime import datetime, timedelta

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.http import base36_to_int

from .models import CustomerAccount


class CustomerTokenGenerator(PasswordResetTokenGenerator):
    key_salt = "apps.customers.tokens.CustomerTokenGenerator"

    def _make_hash_value(self, account: CustomerAccount, timestamp: int) -> str:
        def stamp(value: datetime | None) -> str:
            return "" if value is None else value.isoformat()

        return (
            f"{account.pk}{account.password}{stamp(account.last_login)}{timestamp}"
            f"{account.email}{stamp(account.invited_at)}{stamp(account.revoked_at)}"
        )


class CustomerResetTokenGenerator(CustomerTokenGenerator):
    key_salt = "apps.customers.tokens.CustomerResetTokenGenerator"
    timeout = timedelta(days=7)

    def check_token(self, account: CustomerAccount | None, token: str | None) -> bool:
        """The base check (signature, and the `PASSWORD_RESET_TIMEOUT` setting), then this
        link's own life, so the email's promise holds whatever the setting says."""
        if not super().check_token(account, token):
            return False
        try:
            issued = base36_to_int(token.split("-")[0])
        except ValueError:
            return False
        return self._num_seconds(self._now()) - issued <= self.timeout.total_seconds()


customer_token = CustomerTokenGenerator()
customer_reset_token = CustomerResetTokenGenerator()
