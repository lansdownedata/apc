import io

from openpyxl import Workbook


def make_xlsx(
    headers: list, rows: list[list], *, sheet="Contacts", extra_sheets=(), active=None
) -> io.BytesIO:
    """Build an in-memory workbook. `extra_sheets` are names added after `sheet`."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers)
    for row in rows:
        ws.append(row)
    for name in extra_sheets:
        wb.create_sheet(name)
    if active:
        wb.active = wb.sheetnames.index(active)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
