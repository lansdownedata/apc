from django.db import models


class MoneyField(models.DecimalField):
    """USD money — fixed 2 decimal places. Reusable across pricing models."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 10)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("default", 0)
        super().__init__(*args, **kwargs)
