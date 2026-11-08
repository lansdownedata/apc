"""Read and write .xlsx workbooks for the Contacts / Vendors import and export.

Pure module: no models. The reader hands a `tablib.Dataset` to the resources; the writer builds
the template and export workbooks. Spec: APC-141 design §3.3, §3.5, §3.6.
"""

import datetime
import io
from collections.abc import Sequence
from dataclasses import dataclass, field

import tablib
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ACCEPTED_SHEET = "Accepted values"
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")
ROW_COLUMN = "__row__"

NOT_XLSX = "This isn't an Excel file we can read. Upload a .xlsx file."
NO_NAME = "This file has no Name column. Use the template's header row."
NOTES_LIMIT = 32_000
NOTES_TOO_LONG = "Too long for Excel — left out; a blank cell leaves the saved notes unchanged."
MAX_WIDTH = 50
LAST_ROW = 1048576


@dataclass(frozen=True)
class Column:
    name: str
    required: bool = False
    text: bool = False
    choices: str = ""


class FileError(Exception):
    """The file can't be used at all; str(e) is the user-facing sentence."""


@dataclass
class ParsedSheet:
    dataset: tablib.Dataset
    examples: int = 0
    ignored_columns: list[str] = field(default_factory=list)
    row_errors: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class SheetSpec:
    sheet: str
    columns: tuple[Column, ...]
    accepted: tuple[tuple[str, tuple[str, ...]], ...]
    rules: tuple[str, ...]


def _norm(header) -> str:
    return str(header or "").strip().rstrip("*").strip().lower()


def _coerce(value, column: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        text = str(int(value))
        if column == "ZIP" and len(text) < 5:
            text = text.zfill(5)
        return text
    return str(value).strip()


def _is_example(email: str) -> bool:
    return email.lower().endswith(tuple("@" + d for d in EXAMPLE_DOMAINS))


def read_rows(file, *, sheet: str, columns: Sequence[Column]) -> ParsedSheet:
    try:
        wb = load_workbook(file, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl/zipfile raise many types for a bad file
        raise FileError(NOT_XLSX) from exc
    try:
        try:
            if sheet in wb.sheetnames:
                ws = wb[sheet]
            else:
                names = [n for n in wb.sheetnames if n != ACCEPTED_SHEET]
                if not names:
                    raise FileError(NO_NAME)
                ws = wb[names[0]]
            rows = ws.iter_rows(values_only=True)
            header = next(rows, None)
            if header is None:
                raise FileError(NO_NAME)
            by_norm = {_norm(c.name): c.name for c in columns}
            positions: list[tuple[int, str]] = []
            ignored: list[str] = []
            for idx, h in enumerate(header):
                if h is None or str(h).strip() == "":
                    continue
                canon = by_norm.get(_norm(h))
                if canon and canon not in [n for _, n in positions]:
                    positions.append((idx, canon))
                elif not canon:
                    ignored.append(str(h).strip())
            if "Name" not in [n for _, n in positions]:
                raise FileError(NO_NAME)

            dataset = tablib.Dataset(headers=[n for _, n in positions] + [ROW_COLUMN])
            parsed = ParsedSheet(dataset=dataset, ignored_columns=ignored)
            for excel_row, raw in enumerate(rows, start=2):
                raw = tuple(raw)
                cells = {name: raw[idx] if idx < len(raw) else None for idx, name in positions}
                texts = {n: _coerce(v, n) for n, v in cells.items()}
                if not any(texts.values()):
                    continue
                if _is_example(texts.get("Email", "")):
                    parsed.examples += 1
                    continue
                bad = next(
                    (
                        n
                        for n, v in cells.items()
                        if isinstance(v, (datetime.datetime, datetime.date, datetime.time))
                    ),
                    None,
                )
                if bad:
                    parsed.row_errors.append(
                        {
                            "row": excel_row,
                            "outcome": "error",
                            "label": texts.get("Name", ""),
                            "key": texts.get("Email") or texts.get("Phone", ""),
                            "reason": f"Unexpected date in {bad}",
                            "values": {
                                n: (str(v) if v is not None else "") for n, v in cells.items()
                            },
                        }
                    )
                    continue
                dataset.append([texts[n] for _, n in positions] + [excel_row])
            return parsed
        except FileError:
            raise
        except Exception as exc:
            raise FileError(NOT_XLSX) from exc
    finally:
        wb.close()


def _store(cell, value) -> None:
    """Assign a value; strings that look like formulas are stored as text, never evaluated."""
    cell.value = value
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        cell.data_type = "s"


def write_workbook(spec: SheetSpec, dataset: tablib.Dataset) -> bytes:
    by_name = {c.name: c for c in spec.columns}
    wb = Workbook()
    ws = wb.active
    ws.title = spec.sheet
    headers = list(dataset.headers or [])
    bold = Font(bold=True)
    widths: list[int] = []
    for col_idx, h in enumerate(headers, start=1):
        col = by_name.get(h)
        text = f"{h}*" if col and col.required else str(h)
        cell = ws.cell(row=1, column=col_idx, value=text)
        cell.font = bold
        widths.append(len(text))
    for row_idx, row in enumerate(dataset, start=2):
        for col_idx, value in enumerate(row, start=1):
            h = headers[col_idx - 1]
            col = by_name.get(h)
            if h == "Notes" and isinstance(value, str) and len(value) > NOTES_LIMIT:
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.comment = Comment(NOTES_TOO_LONG, "All Pro Charter")
                continue
            if value is None:
                value = ""
            cell = ws.cell(row=row_idx, column=col_idx)
            _store(cell, value)
            if col and col.text:
                cell.number_format = "@"
            widths[col_idx - 1] = max(widths[col_idx - 1], len(str(value)))
    for col_idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = min(w + 2, MAX_WIDTH)
    ws.freeze_panes = "A2"

    acc = wb.create_sheet(ACCEPTED_SHEET)
    counts: dict[str, int] = {}
    for col_idx, (title, values) in enumerate(spec.accepted, start=1):
        letter = get_column_letter(col_idx)
        acc.cell(row=1, column=col_idx, value=title).font = bold
        for r, v in enumerate(values, start=2):
            acc.cell(row=r, column=col_idx, value=v)
        counts[letter] = len(values)
    acc.cell(row=1, column=4, value="Rules").font = bold
    for r, rule in enumerate(spec.rules, start=2):
        acc.cell(row=r, column=4, value=rule)

    for col_idx, h in enumerate(headers, start=1):
        col = by_name.get(h)
        if col and col.choices:
            n = counts.get(col.choices, 0)
            dv = DataValidation(
                type="list",
                formula1=f"'{ACCEPTED_SHEET}'!${col.choices}$2:${col.choices}${n + 1}",
                allow_blank=True,
                showErrorMessage=False,
            )
            letter = get_column_letter(col_idx)
            dv.add(f"{letter}2:{letter}{LAST_ROW}")
            ws.add_data_validation(dv)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
