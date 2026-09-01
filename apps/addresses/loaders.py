"""Load the committed airport and airline CSVs into their tables.

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


def _bool(value) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


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
        "timezone": (row.get("timezone") or "").strip(),
        # Both default false on a CSV that predates these columns — a fixture missing them
        # must never silently grant ground-transport or Verify eligibility (2026-08-29).
        "serves_ground_transport": _bool(row.get("serves_ground_transport")),
        "has_scheduled_service": _bool(row.get("has_scheduled_service")),
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


AIRLINES_CSV_PATH = Path(__file__).resolve().parent / "data" / "airlines.csv"


def load_airlines(model, path: Path | str | None = None) -> tuple[int, int]:
    """Upsert every row of the airline CSV keyed on `iata`. Returns (created, updated).

    Same contract as `load_airports`: the model is a parameter so the 0004 data
    migration can pass its historical model, and `supported` filters the mapped fields
    down to what that model actually has, so a future field removal from `Airline`
    cannot break an already-applied migration.
    """
    path = Path(path) if path else AIRLINES_CSV_PATH
    supported = {f.name for f in model._meta.get_fields()}
    created = updated = 0
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            iata = (row["iata"] or "").strip().upper()
            if not iata:
                continue
            defaults = {
                k: v
                for k, v in {
                    "icao": (row["icao"] or "").strip().upper(),
                    "name": (row["name"] or "").strip(),
                }.items()
                if k in supported
            }
            _, was_created = model.objects.update_or_create(iata=iata, defaults=defaults)
            if was_created:
                created += 1
            else:
                updated += 1
    return created, updated


VENUES_CSV_PATH = Path(__file__).resolve().parent / "data" / "venues.csv"


def load_venues(model, path: Path | str | None = None) -> tuple[int, int]:
    """Upsert every row of the venue CSV keyed on (name, kind). Returns (created, updated).

    Same contract as `load_airports` / `load_airlines`: the model is a parameter so a data
    migration can pass its own historical model, and `supported` filters the mapped fields
    down to what that model actually has.

    (name, kind) rather than name alone because a place can legitimately be two things —
    Lansdowne Resort is both a reception venue and a room-block hotel, and the two rows
    carry different cap notes and rank in different typeaheads.

    `address` is deliberately never written: the seed carries names and towns only, and
    the street line belongs to LocationIQ.
    """
    path = Path(path) if path else VENUES_CSV_PATH
    supported = {f.name for f in model._meta.get_fields()}
    created = updated = 0
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            kind = (row.get("kind") or "venue").strip()
            defaults = {
                k: v
                for k, v in {
                    "city": (row.get("city") or "").strip(),
                    "state": (row.get("state") or "").strip().upper(),
                    "vehicle_cap": _int(row.get("vehicle_cap")),
                    "cap_note": (row.get("cap_note") or "").strip(),
                    "lead_hits": _int(row.get("lead_hits")) or 0,
                }.items()
                if k in supported
            }
            _, was_created = model.objects.update_or_create(name=name, kind=kind, defaults=defaults)
            if was_created:
                created += 1
            else:
                updated += 1
    return created, updated


def load_venue_caps(model, path: Path | str) -> tuple[int, list[tuple[str, str]]]:
    """Update ONLY `vehicle_cap` / `cap_note` on existing venues, keyed on (name, kind).

    The companion to `load_venues` for the client's venue -> vehicle-limit list (APC-9):
    it never creates a row and never touches any other field, so a partial list can be
    re-run against the directory without disturbing names, towns, `lead_hits`, or the
    enrichment data. A blank cell writes the empty value (an explicit way to clear a cap
    the client has withdrawn), matching `load_venues`.

    CSV columns: `name,kind,vehicle_cap,cap_note` (`kind` blank -> "venue"). Returns
    `(rows_updated, unmatched)` where `unmatched` is the (name, kind) pairs with no
    directory row, for the caller to report.
    """
    path = Path(path)
    updated = 0
    unmatched: list[tuple[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            kind = (row.get("kind") or "venue").strip() or "venue"
            fields: dict = {}
            if "vehicle_cap" in row:
                fields["vehicle_cap"] = _int(row.get("vehicle_cap"))
            if "cap_note" in row:
                fields["cap_note"] = (row.get("cap_note") or "").strip()
            hits = model.objects.filter(name=name, kind=kind).update(**fields) if fields else 0
            if hits:
                updated += hits
            else:
                unmatched.append((name, kind))
    return updated, unmatched
