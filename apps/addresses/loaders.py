"""Load the committed airport CSV into the Airport table.

Shared by the 0003 data migration and `manage.py seed_airports`. The model class is a
parameter so the migration can pass its own historical model; `supported` filters the
mapped fields down to what that model actually has, so adding a field to Airport later
cannot break an already-applied migration.
"""

import csv
from decimal import Decimal
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent / "data" / "airports.csv"


def _int(value):
    value = (value or "").strip()
    return int(value) if value else None


def _row_to_fields(row: dict) -> dict:
    return {
        "ourairports_id": int(row["id"]),
        "iata": (row["iata_code"] or "").strip(),
        "icao": (row["icao_code"] or "").strip(),
        "size": (row["type"] or "").strip(),
        "name": (row["name"] or "").strip(),
        "city": (row["city"] or "").strip(),
        "state": (row["State"] or "").strip(),
        "country": (row["country"] or "US").strip(),
        "latitude": Decimal(row["latitude_deg"]),
        "longitude": Decimal(row["longitude_deg"]),
        "elevation_ft": _int(row["elevation_ft"]),
    }


def load_airports(model, path: Path | str | None = None) -> tuple[int, int]:
    """Upsert every CSV row keyed on `ident`. Returns (created, updated).

    Only the columns present in the CSV are written, so enrichment data added later by
    `enrich_airports` survives a reload untouched.
    """
    path = Path(path) if path else CSV_PATH
    supported = {f.name for f in model._meta.get_fields()}
    created = updated = 0
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ident = (row["ident"] or "").strip()
            if not ident:
                continue
            defaults = {k: v for k, v in _row_to_fields(row).items() if k in supported}
            _, was_created = model.objects.update_or_create(ident=ident, defaults=defaults)
            if was_created:
                created += 1
            else:
                updated += 1
    return created, updated
