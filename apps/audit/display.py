"""How audit values read on screen (spec §6.2). Pure functions, no queries."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def money(value: Any) -> str:
    """`1250` -> `$1,250.00`; blank reads as `$0.00`."""
    return f"${Decimal(str(value or 0)):,.2f}"
