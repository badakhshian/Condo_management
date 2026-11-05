from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import abort
from flask_login import current_user, login_required


def money(cents):
    """Format integer cents as a dollar string, e.g. -1250 -> '-$12.50'."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def parse_money(value):
    """Parse a user-entered amount ('1,234.50', '$20') into integer cents.

    Raises ValueError for empty, malformed or non-positive input.
    """
    cleaned = (value or "").strip().replace(",", "").replace("$", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Enter a valid amount.")
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Amount can have at most two decimal places.")
    return int(amount * 100)


def parse_date(value, field="Date"):
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise ValueError(f"{field} must be a valid date (YYYY-MM-DD).")


def parse_datetime(value, field="Time"):
    try:
        return datetime.fromisoformat((value or "").strip())
    except ValueError:
        raise ValueError(f"{field} must be a valid date and time.")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def can_access_unit(unit_id):
    return current_user.is_admin or (unit_id is not None and current_user.unit_id == unit_id)
