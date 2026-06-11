from django.db import models


class Channel(models.TextChoices):
    """Lead source channel — shared by Contact and Lead."""

    WEBSITE = "website", "Website"
    WEDDING_PRO = "wedding_pro", "Wedding Pro"
    PHONE = "phone", "Phone"
    API = "api", "API"
