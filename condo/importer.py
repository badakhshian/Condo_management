"""Read unit data from the condo's "Financial Simulator" Excel workbook.

Only the "Cotisations proprietaires" (owner contributions) sheet is used. Its
layout: row 1 holds the year above each January column, row 2 the month names
(plus a "Sum" column after each December), and column A labels blocks of eight
unit rows (unit number "#161" in column B, ownership share in column C) that
ends with a "Total" row.

Blocks read:
  "Includs Interest"            balance owed per unit, including late interest
  "Interest Accumulated"        late interest built up per unit
  "Monthly Condo fee received"  payments received (used to find the last month with data)
  "Monthly Condo fee"           monthly fee per unit

Balances are positive when the unit owes money and negative when it is in credit.
"""
import calendar
import re
from datetime import date

SHEET = "Cotisations proprietaires"
MONTHS = [m[:3] for m in calendar.month_abbr[1:]]
UNIT_RE = re.compile(r"^#?\s*(\d+)\s*$")
MAX_ROWS = 150


class ImportError_(ValueError):
    """The workbook doesn't have the expected layout."""


def _cents(value):
    return int(round(float(value or 0) * 100))


def _label(value):
    return str(value).strip() if value is not None else ""


def _load_rows(fileobj):
    from openpyxl import load_workbook

    try:
        wb = load_workbook(fileobj, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types for bad files
        raise ImportError_("That file couldn't be read as an Excel workbook (.xlsx).") from exc
    if SHEET not in wb.sheetnames:
        raise ImportError_(f'The workbook has no "{SHEET}" sheet.')
    rows = [list(r) for r in wb[SHEET].iter_rows(min_row=1, max_row=MAX_ROWS, values_only=True)]
    wb.close()
    return rows


def _cell(rows, r, c):
    """1-based access that tolerates short rows."""
    if r - 1 >= len(rows):
        return None
    row = rows[r - 1]
    return row[c - 1] if c - 1 < len(row) else None


def _month_columns(rows):
    """Map (year, month) -> 1-based column index."""
    columns = {}
    width = max(len(r) for r in rows[:2])
    for c in range(1, width + 1):
        year = _cell(rows, 1, c)
        if isinstance(year, (int, float)) and 2000 < year < 2100:
            for m in range(12):
                if _label(_cell(rows, 2, c + m))[:3].title() == MONTHS[m]:
                    columns[(int(year), m + 1)] = c + m
    if not columns:
        raise ImportError_("Couldn't find the year and month header rows.")
    return columns


def _block(rows, label, after=0):
    """Return {unit_number: row_index} for the block labelled `label` in column A."""
    for r in range(after + 1, len(rows) + 1):
        if _label(_cell(rows, r, 1)).lower() == label.lower():
            units = {}
            for rr in range(r + 1, min(r + 15, len(rows)) + 1):
                a, b = _label(_cell(rows, rr, 1)), _label(_cell(rows, rr, 2))
                if "total" in (a.lower(), b.lower()):
                    break
                m = UNIT_RE.match(b)
                if m:
                    units[m.group(1)] = rr
                elif units and a:
                    break  # next block's label: this block had no Total row
            if units:
                return r, units
    raise ImportError_(f'Couldn\'t find the "{label}" block on the "{SHEET}" sheet.')


def _latest_month_with_payments(rows, columns, received_rows, today):
    for (year, month) in sorted(columns, reverse=True):
        if (year, month) > (today.year, today.month):
            continue
        col = columns[(year, month)]
        if any(_cell(rows, r, col) not in (None, 0) for r in received_rows.values()):
            return year, month
    raise ImportError_("Couldn't find any month with payments recorded.")


def parse_workbook(fileobj, month=None, today=None):
    """Parse the workbook. `month` is an optional (year, month) cut-off; by default
    the latest month (not in the future) with any payment recorded is used."""
    rows = _load_rows(fileobj)
    columns = _month_columns(rows)

    _, balance_rows = _block(rows, "Includs Interest")
    _, interest_rows = _block(rows, "Interest Accumulated")
    _, received_rows = _block(rows, "Monthly Condo fee received")
    _, fee_rows = _block(rows, "Monthly Condo fee")

    if month is None:
        month = _latest_month_with_payments(rows, columns, received_rows, today or date.today())
    if month not in columns:
        raise ImportError_(f"The workbook has no column for {calendar.month_name[month[1]]} {month[0]}.")
    col = columns[month]

    units = []
    for number, r in balance_rows.items():
        share = _cell(rows, r, 3)
        fee_row = fee_rows.get(number)
        name_source = fee_row or r
        units.append({
            "number": number,
            "share": float(share) if isinstance(share, (int, float)) else None,
            "fee_cents": _cents(_cell(rows, fee_row, col)) if fee_row else 0,
            "owner": _label(_cell(rows, name_source, 1)).title(),
            "balance_cents": _cents(_cell(rows, r, col)),
            "interest_cents": _cents(_cell(rows, interest_rows[number], col)) if number in interest_rows else 0,
        })
    units.sort(key=lambda u: int(u["number"]))
    return {"month": f"{month[0]:04d}-{month[1]:02d}", "units": units,
            "loaded": {"units": None, "owners": None, "balances": None}}


def month_end(period):
    year, month = (int(x) for x in period.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


def month_label(period):
    year, month = (int(x) for x in period.split("-"))
    return f"{calendar.month_name[month]} {year}"
