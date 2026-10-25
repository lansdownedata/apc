"""Signed, expiring links for planner registration and password reset.

Django's reset-token machinery over a `PlannerAccount`. The hash covers the password (so a
link dies once a password is set), the last sign-in, when the invite was sent (so Resend
kills the previous link) and the revocation stamp (so Revoke kills every outstanding one).
Invite links live `PASSWORD_RESET_TIMEOUT` (7 days, the same as the staff invite);
password-reset links have their own salt and a shorter life, so one can never stand in
for the other.
"""

from datetime import timedelta

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.http import base36_to_int


class PlannerTokenGenerator(PasswordResetTokenGenerator):
    key_salt = "apps.planners.tokens.PlannerTokenGenerator"

    def _make_hash_value(self, account, timestamp: int) -> str:
        def stamp(value) -> str:
            return "" if value is None else value.isoformat()

        return (
            f"{account.pk}{account.password}{stamp(account.last_login)}{timestamp}"
            f"{account.email}{stamp(account.invited_at)}{stamp(account.revoked_at)}"
        )


class PlannerResetTokenGenerator(PlannerTokenGenerator):
    key_salt = "apps.planners.tokens.PlannerResetTokenGenerator"
    timeout = timedelta(days=3)

    def check_token(self, account, token) -> bool:
        """The base check (signature, and the 7-day setting), then this link's own life."""
        if not super().check_token(account, token):
            return False
        try:
            issued = base36_to_int(token.split("-")[0])
        except ValueError:
            return False
        return self._num_seconds(self._now()) - issued <= self.timeout.total_seconds()


planner_token = PlannerTokenGenerator()
planner_reset_token = PlannerResetTokenGenerator()
