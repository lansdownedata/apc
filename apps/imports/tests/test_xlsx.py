import datetime
import io

import pytest
import tablib
from openpyxl import Workbook, load_workbook

from apps.imports.xlsx import (
    ACCEPTED_SHEET,
    Column,
    FileError,
    SheetSpec,
    read_rows,
    write_workbook,
)

from .helpers import make_xlsx

COLS = (
    Column("Name", required=True),
    Column("Email"),
    Column("Phone", text=True),
    Column("ZIP", text=True),
    Column("USDOT number", text=True),
    Column("Channel", choices="A"),
    Column("Notes"),
)


def read(f):
    return read_rows(f, sheet="Contacts", columns=COLS)


def test_reads_named_sheet_even_when_accepted_values_is_active():
    wb = Workbook()
    ws = wb.active
    ws.title = ACCEPTED_SHEET
    ws.append(["Channel"])
    ws2 = wb.create_sheet("Contacts")
    ws2.append(["Name"])
    ws2.append(["Jordan"])
    wb.active = 0
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    parsed = read(buf)
    assert parsed.dataset["Name"] == ["Jordan"]


def test_falls_back_to_first_non_accepted_sheet():
    buf = make_xlsx(["Name"], [["Jordan"]], sheet="Sheet1")
    assert read(buf).dataset["Name"] == ["Jordan"]


def test_header_matching_ignores_case_spaces_and_star():
    buf = make_xlsx([" name* ", "EMAIL"], [["Jo", "jo@acme.test"]])
    assert read(buf).dataset.headers == ["Name", "Email", "__row__"]


def test_missing_name_header_is_a_file_error():
    with pytest.raises(FileError, match="no Name column"):
        read(make_xlsx(["Email"], [["a@acme.test"]]))


def test_not_an_xlsx_is_a_file_error():
    with pytest.raises(FileError, match="isn't an Excel file"):
        read(io.BytesIO(b"hello"))


def test_unknown_columns_listed():
    parsed = read(make_xlsx(["Name", "Fax", "Tier"], [["Jo", "1", "2"]]))
    assert parsed.ignored_columns == ["Fax", "Tier"]
    assert parsed.dataset.headers == ["Name", "__row__"]


def test_float_phone_and_usdot_become_digits():
    buf = make_xlsx(["Name", "Phone", "USDOT number"], [["Jo", 2025550143.0, 1234567]])
    ds = read(buf).dataset
    assert ds["Phone"] == ["2025550143"]
    assert ds["USDOT number"] == ["1234567"]


def test_numeric_zip_is_zero_padded():
    buf = make_xlsx(["Name", "ZIP"], [["A", 2134], ["B", "2134"]])
    assert read(buf).dataset["ZIP"] == ["02134", "2134"]


def test_date_cell_is_a_row_error():
    buf = make_xlsx(
        ["Name", "Email", "Phone"],
        [["Jo", "jo@acme.test", datetime.datetime(2026, 1, 2)], ["Ok", "ok@acme.test", "1"]],
    )
    parsed = read(buf)
    err = parsed.row_errors[0]
    assert err["reason"] == "Unexpected date in Phone"
    assert err["row"] == 2
    assert err["outcome"] == "error"
    assert err["label"] == "Jo"
    assert err["key"] == "jo@acme.test"
    assert err["values"]["Name"] == "Jo"
    assert parsed.dataset["__row__"] == [3]


def test_date_error_values_use_coerced_strings():
    buf = make_xlsx(["Name", "Phone", "ZIP"], [["Jo", 2025550143.0, datetime.datetime(2026, 1, 2)]])
    values = read(buf).row_errors[0]["values"]
    assert values["Phone"] == "2025550143"
    assert values["ZIP"].startswith("2026-01-02")


def test_programming_error_in_row_loop_propagates(monkeypatch):
    from apps.imports import xlsx

    def boom(*a, **k):
        raise TypeError("bug")

    monkeypatch.setattr(xlsx, "_coerce", boom)
    with pytest.raises(TypeError):
        read(make_xlsx(["Name"], [["Jo"]]))


def test_blank_rows_dropped():
    buf = make_xlsx(["Name"], [["A"], [None], ["  "], ["B"]])
    assert read(buf).dataset["Name"] == ["A", "B"]


def test_rows_carry_excel_row_numbers():
    buf = make_xlsx(["Name"], [["A"], [None], ["B"]])
    assert read(buf).dataset["__row__"] == [2, 4]


def test_example_rows_dropped_and_counted():
    buf = make_xlsx(
        ["Name", "Email"],
        [
            ["a", "jordan@example.com"],
            ["b", "x@EXAMPLE.org"],
            ["c", "y@example.net"],
            ["d", "real@acme.test"],
        ],
    )
    parsed = read(buf)
    assert parsed.examples == 3
    assert parsed.dataset["Name"] == ["d"]


# --- writer ---

ACCEPTED = (("Channel", ("Phone", "Web", "Walk-in")), ("Phone label", ("Mobile", "Office")))
SPEC = SheetSpec(
    sheet="Contacts", columns=COLS, accepted=ACCEPTED, rules=("Name is required", "Blank = keep")
)


def write(rows, headers=None):
    ds = tablib.Dataset(headers=headers or [c.name for c in COLS])
    for r in rows:
        ds.append(r)
    wb = load_workbook(io.BytesIO(write_workbook(SPEC, ds)))
    return wb


def row_of(**kw):
    return [kw.get(c.name, "") for c in COLS]


def test_formula_is_written_as_text():
    wb = write([row_of(Name="Jo", Notes="=SUM(A1)", Phone="+12025550143")])
    ws = wb["Contacts"]
    assert ws["G2"].data_type == "s"
    assert ws["G2"].value == "=SUM(A1)"
    assert ws["C2"].data_type == "s"
    assert ws["C2"].value == "+12025550143"


def test_text_format_on_phone_zip_usdot():
    ws = write([row_of(Name="Jo")])["Contacts"]
    assert ws["C2"].number_format == "@"
    assert ws["D2"].number_format == "@"
    assert ws["E2"].number_format == "@"
    assert ws["A2"].number_format != "@"


def test_text_format_applies_to_whole_column_with_no_data():
    ws = write([])["Contacts"]
    assert ws.column_dimensions["C"].number_format == "@"
    assert ws.column_dimensions["D"].number_format == "@"
    assert ws.column_dimensions["E"].number_format == "@"
    assert ws.column_dimensions["A"].number_format != "@"


def test_minus_prefix_is_written_as_text():
    ws = write([row_of(Name="-cmd|calc", Notes="-1+1")])["Contacts"]
    assert ws["A2"].data_type == "s"
    assert ws["G2"].data_type == "s"
    assert ws["G2"].value == "-1+1"


def test_dropdown_on_choice_columns():
    ws = write([row_of(Name="Jo")])["Contacts"]
    dvs = ws.data_validations.dataValidation
    assert len(dvs) == 1
    dv = dvs[0]
    assert "F2:F1048576" in str(dv.sqref)
    assert dv.formula1 == "'Accepted values'!$A$2:$A$4"
    assert dv.showErrorMessage is False
    assert dv.allow_blank is True


def test_accepted_values_sheet_contents():
    ws = write([])[ACCEPTED_SHEET]
    assert [ws["A1"].value, ws["B1"].value, ws["D1"].value] == ["Channel", "Phone label", "Rules"]
    assert [ws.cell(r, 1).value for r in (2, 3, 4)] == ["Phone", "Web", "Walk-in"]
    assert [ws.cell(r, 2).value for r in (2, 3)] == ["Mobile", "Office"]
    assert [ws.cell(r, 4).value for r in (2, 3)] == ["Name is required", "Blank = keep"]


def test_long_notes_written_blank_with_comment():
    ws = write([row_of(Name="Jo", Notes="x" * 33000)])["Contacts"]
    assert ws["G2"].value is None
    assert ws["G2"].comment.text.startswith("Too long for Excel")


def test_required_header_has_star_bold_and_frozen():
    ws = write([])["Contacts"]
    assert ws["A1"].value == "Name*"
    assert ws["B1"].value == "Email"
    assert ws["A1"].font.bold
    assert ws.freeze_panes == "A2"


def test_unknown_header_written_plain():
    ws = write([["Jo", "no thanks"]], headers=["Name", "Reason"])["Contacts"]
    assert ws["B1"].value == "Reason"
    assert ws["B2"].value == "no thanks"
