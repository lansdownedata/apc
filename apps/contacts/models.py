from django.db import models

from apps.core.choices import Channel
from apps.core.models import TimeStampedModel


class Contact(TimeStampedModel):
    """A customer (person or company) — the LimoAnywhere Account."""

    name = models.CharField(max_length=200)
    company = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.WEBSITE)
    la_account_id = models.CharField("LimoAnywhere account", max_length=64, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"{self.name} · {self.company}" if self.company else self.name
