import calendar
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import CHARGE_KINDS, PAYMENT_METHODS, Charge, Payment, Unit
from ..utils import admin_required, can_access_unit, parse_date, parse_money

bp = Blueprint("billing", __name__, url_prefix="/billing")


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
    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all()
    rows = [{"unit": u, "charged": u.total_charged, "paid": u.total_paid} for u in units]
    for r in rows:
        r["balance"] = r["charged"] - r["paid"]
    totals = {k: sum(r[k] for r in rows) for k in ("charged", "paid", "balance")}
    recent_payments = db.session.scalars(
        db.select(Payment).order_by(Payment.paid_on.desc(), Payment.id.desc()).limit(10)
    ).all()
    today = date.today()
    return render_template("billing/index.html", rows=rows, totals=totals,
                           recent_payments=recent_payments, today=today)


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
        if kind not in CHARGE_KINDS or kind == "dues":
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
        return render_template("billing/charge_form.html", units=_units(), kinds=CHARGE_KINDS,
                               form=request.form), 400
    form = {"unit_id": request.args.get("unit_id", ""), "due_date": date.today().isoformat()}
    return render_template("billing/charge_form.html", units=_units(), kinds=CHARGE_KINDS, form=form)


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
                                   reference=request.form.get("reference", "").strip() or None,
                                   recorded_by_id=current_user.id))
            db.session.commit()
            flash(f"Payment recorded for unit {unit.number}.", "success")
            return redirect(url_for("billing.ledger", unit_id=unit.id))
        for e in errors:
            flash(e, "error")
        return render_template("billing/payment_form.html", units=_units(), methods=PAYMENT_METHODS,
                               form=request.form), 400
    form = {"unit_id": request.args.get("unit_id", ""), "paid_on": date.today().isoformat()}
    return render_template("billing/payment_form.html", units=_units(), methods=PAYMENT_METHODS, form=form)


@bp.route("/charges/<int:charge_id>/delete", methods=["POST"])
@admin_required
def delete_charge(charge_id):
    charge = db.session.get(Charge, charge_id) or abort(404)
    unit_id = charge.unit_id
    db.session.delete(charge)
    db.session.commit()
    flash("Charge removed.", "success")
    return redirect(url_for("billing.ledger", unit_id=unit_id))


@bp.route("/payments/<int:payment_id>/delete", methods=["POST"])
@admin_required
def delete_payment(payment_id):
    payment = db.session.get(Payment, payment_id) or abort(404)
    unit_id = payment.unit_id
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
