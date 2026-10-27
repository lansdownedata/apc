"""Settings overrides of checklist due timing, read as one lookup."""

from .models import TaskSchedule


def load() -> dict[tuple[str, str], tuple[int, str]]:
    """Every override as (kind, variant) -> (amount, unit). One query."""
    rows = TaskSchedule.objects.values_list("kind", "variant", "amount", "unit")
    return {(kind, variant): (amount, unit) for kind, variant, amount, unit in rows}
