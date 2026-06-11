from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Owner/admin and agent accounts for the Lead Manager.

    Custom user defined up front so AUTH_USER_MODEL is stable before the first
    migration (swapping it later is painful).
    """

    class Role(models.TextChoices):
        OWNER_ADMIN = "owner_admin", "Owner / Admin"
        AGENT = "agent", "Agent"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.AGENT)
    phone = models.CharField(max_length=32, blank=True)
    two_factor_enabled = models.BooleanField(default=False)

    @property
    def is_owner_admin(self) -> bool:
        return self.role == self.Role.OWNER_ADMIN

    def __str__(self) -> str:
        return self.get_full_name() or self.username
