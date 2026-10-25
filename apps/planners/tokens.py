"""Signed, expiring links for planner registration and password reset.

Django's reset-token machinery over a `PlannerAccount`. The hash covers the password (so a
link dies once a password is set), the last sign-in, when the invite was sent (so Resend
kills the previous link) and the revocation stamp (so Revoke kills every outstanding one).
Lifetime is `PASSWORD_RESET_TIMEOUT`, the same as the staff invite.
"""

from django.contrib.auth.tokens import PasswordResetTokenGenerator


class PlannerTokenGenerator(PasswordResetTokenGenerator):
    key_salt = "apps.planners.tokens.PlannerTokenGenerator"

    def _make_hash_value(self, account, timestamp: int) -> str:
        def stamp(value) -> str:
            return "" if value is None else value.isoformat()

        return (
            f"{account.pk}{account.password}{stamp(account.last_login)}{timestamp}"
            f"{account.email}{stamp(account.invited_at)}{stamp(account.revoked_at)}"
        )


planner_token = PlannerTokenGenerator()
