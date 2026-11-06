import calendar
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (CHARGE_KINDS, COST_CATEGORIES, PAYMENT_METHOD_LABELS, PAYMENT_METHODS, SYSTEM_CHARGE_KINDS, Charge, Expense, Payment,
                      Setting, Unit, guess_cost_category)
from ..utils import admin_required, can_access_unit, parse_date, parse_money

# Shown as "Accounting"; the blueprint keeps its old name so existing links keep working.
bp = Blueprint("billing", __name__, url_prefix="/accounting")
legacy = Blueprint("billing_legacy", __name__)


@legacy.route("/billing/", defaults={"rest": ""})
@legacy.route("/billing/<path:rest>")
def old_address(rest):
    """Billing was renamed Accounting; keep old links working."""
    query = request.query_string.decode()
    return redirect(f"/accounting/{rest}" + (f"?{query}" if query else ""), code=301)


def resolve_period(args, today=None):
    """Read ?view=year|month&year=&month= into a period dict with start/end dates and neighbours."""
    today = today or date.today()
    view = "month" if args.get("view") == "month" else "year"
    try:
        year = int(args.get("year", today.year))
        month = int(args.get("month", today.month))
        if not (1900 < year < 2200 and 1 <= month <= 12):
            raise ValueError
    except ValueError:
        year, month = today.year, today.month
    if view == "year":
        start, end = date(year, 1, 1), date(year, 12, 31)
        label = str(year)
        prev, nxt = {"view": "year", "year": year - 1}, {"view": "year", "year": year + 1}
    else:
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        label = f"{calendar.month_name[month]} {year}"
        py, pm = (year, month - 1) if month > 1 else (year - 1, 12)
        ny, nm = (year, month + 1) if month < 12 else (year + 1, 1)
        prev, nxt = {"view": "month", "year": py, "month": pm}, {"view": "month", "year": ny, "month": nm}
    return {"view": view, "year": year, "month": month, "start": start, "end": end, "label": label,
            "prev": prev, "next": nxt}


class OwnerPaidCost:
    """A purchase credit shown in the Costs panel: an owner paid a building cost themselves."""
    origin = "credit"
    approved_by = None

    def __init__(self, payment):
        self.payment = payment
        self.spent_on = payment.paid_on
        self.sort_id = -payment.id
        text = payment.notes or "Purchase"
        self.description = text.split(" — ", 1)[1] if " — " in text else text
        self.cost_cents = payment.amount_cents
        self.category_key = guess_cost_category(self.description)
        if self.category_key == "other":
            self.category_key = "maintenance"
        self.category_label = COST_CATEGORIES[self.category_key]
        self.paid_by = f"Paid by owner of unit {payment.unit.number}, credited to their account"


def _sum(column, *where):
    return db.session.scalar(db.select(db.func.coalesce(db.func.sum(column), 0)).where(*where))


def generate_dues(year, month, due_day=1):
    """Create a monthly dues charge for every unit with a fee. Idempotent per period.

    Returns (created_count, skipped_count).
    """
    period = f"{year:04d}-{month:02d}"
    due = date(year, month, min(due_day, calendar.monthrange(year, month)[1]))
    label = f"Monthly dues — {calendar.month_name[month]} {year}"
    existing = set(db.session.scalars(
        db.select(Charge.unit_id).where(Charge.kind == "dues", Charge.period == period)
    ))
    created = skipped = 0
    for unit in db.session.scalars(db.select(Unit).where(Unit.monthly_fee_cents > 0)):
        if unit.id in existing:
            skipped += 1
            continue
        db.session.add(Charge(unit_id=unit.id, kind="dues", period=period, description=label,
                              amount_cents=unit.monthly_fee_cents, due_date=due))
        created += 1
    db.session.commit()
    return created, skipped


@bp.route("/")
@admin_required
def index():
    period = resolve_period(request.args)
    start, end = period["start"], period["end"]
    payments = db.session.scalars(db.select(Payment).where(Payment.paid_on.between(start, end))
                                  .order_by(Payment.paid_on.desc(), Payment.id.desc())).all()
    costs = db.session.scalars(db.select(Expense).where(Expense.spent_on.between(start, end))
                               .order_by(Expense.spent_on.desc(), Expense.id.desc())).all()
    # An owner who buys something for the building is credited on their unit: it's a payment from the
    # unit *and* a building cost. Listing it in both keeps the net equal to the real cash movement.
    owner_paid = [OwnerPaidCost(p) for p in payments if p.method == "purchase credit"]
    costs = sorted(list(costs) + owner_paid, key=lambda x: (x.spent_on, x.sort_id), reverse=True)
    paid_total = sum(p.amount_cents for p in payments)
    cost_total = sum(x.cost_cents for x in costs)
    by_category = {}
    for x in costs:
        by_category[x.category_key] = by_category.get(x.category_key, 0) + x.cost_cents
    categories = [(COST_CATEGORIES[k], v) for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])]

    months = []
    if period["view"] == "year":
        for m in range(1, 13):
            ms, me = date(period["year"], m, 1), date(period["year"], m, calendar.monthrange(period["year"], m)[1])
            inc = sum(p.amount_cents for p in payments if ms <= p.paid_on <= me)
            out = sum(x.cost_cents for x in costs if ms <= x.spent_on <= me)
            months.append({"month": m, "name": calendar.month_name[m], "payments": inc, "costs": out, "net": inc - out})

    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all()
    rows = []
    for u in units:
        rows.append({"unit": u,
                     "billed": _sum(Charge.amount_cents, Charge.unit_id == u.id, Charge.kind != "opening",
                                    Charge.due_date.between(start, end)),
                     "paid": sum(p.amount_cents for p in payments if p.unit_id == u.id),
                     "balance": u.balance})
    outstanding = sum(r["balance"] for r in rows if r["balance"] > 0)
    sheet = Setting.get("sheet") or {}
    from ..models import Reimbursement
    pending = db.session.scalar(db.select(db.func.count(Reimbursement.id)).where(
        Reimbursement.status.in_(("submitted", "approved"))))
    return render_template("billing/index.html", period=period, pending_reimbursements=pending, payments=payments, costs=costs,
                           paid_total=paid_total, cost_total=cost_total, categories=categories, months=months,
                           rows=rows, outstanding=outstanding, today=date.today(), sheet=sheet,
                           last_sync=Setting.get("sheet_last_sync"))


@bp.route("/costs/new", methods=["GET", "POST"])
@admin_required
def new_cost():
    if request.method == "POST":
        errors = []
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "other")
        refund = request.form.get("kind") == "refund"
        if not description:
            errors.append("Describe the cost.")
        if category not in COST_CATEGORIES:
            errors.append("Choose a category.")
        try:
            amount = parse_money(request.form.get("amount"))
        except ValueError as e:
            errors.append(str(e))
        try:
            spent_on = parse_date(request.form.get("spent_on"), "Date")
        except ValueError as e:
            errors.append(str(e))
        if not errors:
            db.session.add(Expense(spent_on=spent_on, category=category, description=description[:255],
                                   amount_cents=amount if refund else -amount,
                                   approved_by=request.form.get("approved_by", "").strip()[:255] or None,
                                   source=request.form.get("source", "").strip()[:255] or None,
                                   notes=request.form.get("notes", "").strip() or None))
            db.session.commit()
            flash("Refund recorded." if refund else "Cost recorded.", "success")
            return redirect(url_for("billing.index", view="month", year=spent_on.year, month=spent_on.month))
        for e in errors:
            flash(e, "error")
        return render_template("billing/cost_form.html", form=request.form, categories=COST_CATEGORIES), 400
    return render_template("billing/cost_form.html", categories=COST_CATEGORIES,
                           form={"spent_on": date.today().isoformat(), "category": "maintenance", "kind": "cost"})


@bp.route("/costs/<int:cost_id>/delete", methods=["POST"])
@admin_required
def delete_cost(cost_id):
    cost = db.session.get(Expense, cost_id) or abort(404)
    if cost.origin == "sheet":
        flash("This cost comes from the Google Sheet. Change it there; the next sync updates the app.", "error")
    else:
        db.session.delete(cost)
        db.session.commit()
        flash("Cost removed.", "success")
    return redirect(request.referrer or url_for("billing.index"))


@bp.route("/generate", methods=["POST"])
@admin_required
def generate():
    try:
        year, month = (int(x) for x in request.form.get("period", "").split("-"))
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        flash("Choose a valid billing month.", "error")
        return redirect(url_for("billing.index"))
    created, skipped = generate_dues(year, month)
    msg = f"Generated dues for {created} unit(s)."
    if skipped:
        msg += f" {skipped} unit(s) were already billed for that month."
    flash(msg, "success")
    return redirect(url_for("billing.index"))


def _manual_kinds():
    return {k: v for k, v in CHARGE_KINDS.items() if k not in SYSTEM_CHARGE_KINDS}


def _units():
    return db.session.scalars(db.select(Unit).order_by(Unit.number)).all()


def _unit_from_form(errors):
    raw = request.form.get("unit_id", "")
    unit = db.session.get(Unit, int(raw)) if raw.isdigit() else None
    if unit is None:
        errors.append("Select a unit.")
    return unit


@bp.route("/charges/new", methods=["GET", "POST"])
@admin_required
def new_charge():
    if request.method == "POST":
        errors = []
        unit = _unit_from_form(errors)
        kind = request.form.get("kind", "other")
        if kind not in CHARGE_KINDS or kind in SYSTEM_CHARGE_KINDS:
            errors.append("Choose a charge type.")
        description = request.form.get("description", "").strip()
        if not description:
            errors.append("Description is required.")
        try:
            amount = parse_money(request.form.get("amount"))
        except ValueError as e:
            errors.append(str(e))
        try:
            due = parse_date(request.form.get("due_date"), "Due date")
        except ValueError as e:
            errors.append(str(e))
        if not errors:
            db.session.add(Charge(unit=unit, kind=kind, description=description,
                                  amount_cents=amount, due_date=due))
            db.session.commit()
            flash(f"Charge added to unit {unit.number}.", "success")
            return redirect(url_for("billing.ledger", unit_id=unit.id))
        for e in errors:
            flash(e, "error")
        return render_template("billing/charge_form.html", units=_units(), kinds=_manual_kinds(),
                               form=request.form), 400
    form = {"unit_id": request.args.get("unit_id", ""), "due_date": date.today().isoformat()}
    return render_template("billing/charge_form.html", units=_units(), kinds=_manual_kinds(), form=form)


@bp.route("/payments/new", methods=["GET", "POST"])
@admin_required
def new_payment():
    if request.method == "POST":
        errors = []
        unit = _unit_from_form(errors)
        method = request.form.get("method", "check")
        if method not in PAYMENT_METHODS:
            errors.append("Choose a payment method.")
        try:
            amount = parse_money(request.form.get("amount"))
        except ValueError as e:
            errors.append(str(e))
        try:
            paid_on = parse_date(request.form.get("paid_on"), "Payment date")
        except ValueError as e:
            errors.append(str(e))
        if not errors:
            db.session.add(Payment(unit=unit, amount_cents=amount, paid_on=paid_on, method=method,
                                   reference=request.form.get("reference", "").strip()[:80] or None,
                                   notes=request.form.get("notes", "").strip()[:255] or None,
                                   recorded_by_id=current_user.id))
            db.session.commit()
            flash(f"Payment recorded for unit {unit.number}.", "success")
            return redirect(url_for("billing.ledger", unit_id=unit.id))
        for e in errors:
            flash(e, "error")
        return render_template("billing/payment_form.html", units=_units(), methods=PAYMENT_METHOD_LABELS,
                               form=request.form), 400
    form = {"unit_id": request.args.get("unit_id", ""), "paid_on": date.today().isoformat(), "method": "bank transfer"}
    return render_template("billing/payment_form.html", units=_units(), methods=PAYMENT_METHOD_LABELS, form=form)


@bp.route("/charges/<int:charge_id>/delete", methods=["POST"])
@admin_required
def delete_charge(charge_id):
    charge = db.session.get(Charge, charge_id) or abort(404)
    unit_id = charge.unit_id
    if charge.origin == "sheet":
        flash("This entry comes from the Google Sheet. Change it there; the next sync updates the app.", "error")
        return redirect(url_for("billing.ledger", unit_id=unit_id))
    db.session.delete(charge)
    db.session.commit()
    flash("Charge removed.", "success")
    return redirect(url_for("billing.ledger", unit_id=unit_id))


@bp.route("/payments/<int:payment_id>/delete", methods=["POST"])
@admin_required
def delete_payment(payment_id):
    payment = db.session.get(Payment, payment_id) or abort(404)
    unit_id = payment.unit_id
    if payment.origin == "sheet":
        flash("This entry comes from the Google Sheet. Change it there; the next sync updates the app.", "error")
        return redirect(url_for("billing.ledger", unit_id=unit_id))
    db.session.delete(payment)
    db.session.commit()
    flash("Payment removed.", "success")
    return redirect(url_for("billing.ledger", unit_id=unit_id))


@bp.route("/unit/<int:unit_id>")
@login_required
def ledger(unit_id):
    if not can_access_unit(unit_id):
        abort(403)
    unit = db.session.get(Unit, unit_id) or abort(404)
    rows = unit.ledger()
    rows.reverse()  # newest first
    return render_template("billing/ledger.html", unit=unit, rows=rows)


@bp.route("/my")
@login_required
def my_account():
    if current_user.unit_id is None:
        flash("Your account is not linked to a unit.", "info")
        return redirect(url_for("dashboard.index"))
    return redirect(url_for("billing.ledger", unit_id=current_user.unit_id))
