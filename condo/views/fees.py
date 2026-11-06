"""Accounting > Fees & contributions: annual condo-fee rates per unit, and special contributions
with deadlines and per-unit payment progress."""
from datetime import date, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import notify
from ..extensions import db
from ..models import (FEE_BASES, SPECIAL_STATUS_LABELS, add_months, SPLIT_METHODS, Charge, FeeOverride, FeeSchedule, SpecialContribution,
                      SpecialShare, Unit, split_by_weight, utcnow)
from ..utils import parse_date, parse_money, permission_required

bp = Blueprint("fees", __name__, url_prefix="/accounting")


def _units():
    return db.session.scalars(db.select(Unit).order_by(Unit.number)).all()


def _money(cents):
    return f"${cents / 100:,.2f}"


def _period_defaults(year, previous):
    """Start and end dates offered for a new period: right after the previous one, else the calendar year."""
    start = previous.end + timedelta(days=1) if previous and previous.end.year in (year - 1, year) else date(year, 1, 1)
    return start, add_months(start, 12) - timedelta(days=1)


def check_renewals(today=None):
    """Tell the board, once, when a fee period ends within two months and no next period is set yet."""
    today = today or date.today()
    sent = 0
    schedules = db.session.scalars(db.select(FeeSchedule).order_by(FeeSchedule.year)).all()
    for s in schedules:
        if s.renewal_notified_at or not (s.renewal_date <= today <= s.end + timedelta(days=31)):
            continue
        if any(o.start > s.end - timedelta(days=1) for o in schedules if o.id != s.id):
            continue  # the next period is already set
        nxt = s.end + timedelta(days=1)
        notify.send(notify.board(), f"Condo fees: the current rates end on {s.end:%b %d, %Y}",
                    url=url_for("fees.index", year=nxt.year),
                    body=f"Set the condo fees for the period starting {nxt:%b %d, %Y} and apply them to the units.",
                    kind="fees", include_actor=True)
        s.renewal_notified_at = utcnow()
        sent += 1
    if sent:
        db.session.commit()
    return sent


@bp.route("/fees")
@permission_required("accounting")
def index():
    check_renewals()
    years = db.session.scalars(db.select(FeeSchedule).order_by(FeeSchedule.year.desc())).all()
    try:
        year = int(request.args.get("year", date.today().year))
    except ValueError:
        year = date.today().year
    schedule = db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year == year))
    previous = db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year < year).order_by(FeeSchedule.year.desc()))
    units = _units()
    rows = schedule.unit_fees(units) if schedule else []
    prev_rows = {r["unit"].id: r for r in previous.unit_fees(units)} if previous else {}
    missing_shares = [u.number for u in units if not u.ownership_share]
    specials = db.session.scalars(db.select(SpecialContribution).order_by(SpecialContribution.deadline.desc())).all()
    if schedule:
        starts, ends, basis, amount = schedule.start, schedule.end, schedule.basis, schedule.amount_cents
    else:
        starts, ends = _period_defaults(year, previous)
        basis = previous.basis if previous else "annual"
        amount = previous.amount_cents if previous else None
    form = {"starts_on": starts.isoformat(), "ends_on": ends.isoformat(), "basis": basis,
            "amount": f"{amount / 100:.2f}" if amount is not None else ""}
    today = date.today()
    renewal_due = (schedule is not None and schedule.renewal_date <= today <= schedule.end
                   and not any(y.start > schedule.end - timedelta(days=1) for y in years if y.id != schedule.id))
    return render_template("fees/index.html", year=year, schedule=schedule, previous=previous, rows=rows,
                           prev_rows=prev_rows, years=years, units=units, missing_shares=missing_shares,
                           specials=specials, labels=SPECIAL_STATUS_LABELS, today=today, form=form,
                           bases=FEE_BASES, renewal_due=renewal_due,
                           next_year=(schedule.end + timedelta(days=1)).year if schedule else year + 1)


@bp.route("/fees/<int:year>", methods=["POST"])
@permission_required("accounting_edit")
def save_schedule(year):
    errors, dates = [], {}
    basis = request.form.get("basis", "annual")
    if basis not in FEE_BASES:
        errors.append("Choose annual total or monthly total.")
    try:
        total = parse_money(request.form.get("amount"))
    except ValueError as e:
        errors.append(f"{FEE_BASES.get(basis, 'Total')}: {e}")
        total = 0
    for field, label in (("starts_on", "Start date"), ("ends_on", "End date")):
        try:
            dates[field] = parse_date(request.form.get(field), label)
        except ValueError as e:
            errors.append(str(e))
    schedule = db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year == year))
    if len(dates) == 2:
        if dates["ends_on"] <= dates["starts_on"]:
            errors.append("The end date must be after the start date.")
        else:
            for other in db.session.scalars(db.select(FeeSchedule)):
                if other is schedule:
                    continue
                if other.year == dates["starts_on"].year:
                    errors.append(f"There are already fees for a period starting in {other.year}.")
                elif other.start <= dates["ends_on"] and other.end >= dates["starts_on"]:
                    errors.append(f"These dates overlap the period {other.start:%b %d, %Y} – {other.end:%b %d, %Y}.")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("fees.index", year=year))
    if schedule is None:
        schedule = FeeSchedule(amount_cents=total)
        db.session.add(schedule)
    if schedule.id and schedule.end != dates["ends_on"]:
        schedule.renewal_notified_at = None  # a new end date gets its own reminder
    schedule.year = dates["starts_on"].year
    schedule.starts_on, schedule.ends_on = dates["starts_on"], dates["ends_on"]
    basis_changed = schedule.id is not None and schedule.basis != basis
    schedule.basis = basis
    schedule.amount_cents = total
    schedule.notes = request.form.get("notes", "").strip() or None
    # Per-unit fixed amounts, in the same terms as the total (a year or a month).
    existing = {o.unit_id: o for o in schedule.overrides}
    for unit in _units():
        raw = request.form.get(f"override_{unit.id}", "").strip()
        if raw and not basis_changed:
            try:
                cents = parse_money(raw)
            except ValueError as e:
                flash(f"Unit {unit.number}: {e}", "error")
                db.session.rollback()
                return redirect(url_for("fees.index", year=year))
            if unit.id in existing:
                existing[unit.id].amount_cents = cents
            else:
                schedule.overrides.append(FeeOverride(unit_id=unit.id, amount_cents=cents))
        elif unit.id in existing:
            schedule.overrides.remove(existing[unit.id])
    db.session.flush()
    if sum(o.amount_cents for o in schedule.overrides) > total:
        db.session.rollback()
        flash("The fixed unit amounts add up to more than the total.", "error")
        return redirect(url_for("fees.index", year=year))
    db.session.commit()
    flash(f"Condo fees saved for {schedule.start:%b %d, %Y} – {schedule.end:%b %d, %Y}: "
          f"{_money(schedule.annual_total_cents)} in total, {_money(schedule.monthly_total_cents)} a month."
          + (" Fixed unit amounts were cleared because you switched between annual and monthly." if basis_changed
             and existing else ""), "success")
    return redirect(url_for("fees.index", year=schedule.year))


@bp.route("/fees/<int:year>/apply", methods=["POST"])
@permission_required("accounting_edit")
def apply_schedule(year):
    schedule = db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year == year)) or abort(404)
    changed = 0
    for row in schedule.unit_fees(_units()):
        if row["unit"].monthly_fee_cents != row["monthly"]:
            row["unit"].monthly_fee_cents = row["monthly"]
            changed += 1
    schedule.applied_at = utcnow()
    db.session.commit()
    flash(f"Monthly fees updated on {changed} unit(s). Monthly dues billed from now on use the {year} rates.", "success")
    return redirect(url_for("fees.index", year=year))


# --- special contributions ---------------------------------------------------------

def allocate(total, method, units, custom=None):
    if method == "custom":
        return {u.id: (custom or {}).get(u.id, 0) for u in units}
    if method == "equal":
        return split_by_weight(total, {u.id: 1 for u in units})
    return split_by_weight(total, {u.id: (u.ownership_share or 0) for u in units})


@bp.route("/specials/new", methods=["GET", "POST"])
@permission_required("accounting_edit")
def new_special():
    units = _units()
    if request.method == "POST":
        errors, custom = [], {}
        title = request.form.get("title", "").strip()
        method = request.form.get("method", "share")
        if not title:
            errors.append("Give the contribution a title, e.g. “Window replacement 2027”.")
        if method not in SPLIT_METHODS:
            errors.append("Choose how to split it.")
        try:
            deadline = parse_date(request.form.get("deadline"), "Deadline")
        except ValueError as e:
            errors.append(str(e))
        if method == "custom":
            for u in units:
                raw = request.form.get(f"amount_{u.id}", "").strip()
                if raw:
                    try:
                        custom[u.id] = parse_money(raw)
                    except ValueError as e:
                        errors.append(f"Unit {u.number}: {e}")
            total = sum(custom.values())
            if not total:
                errors.append("Enter the amount for at least one unit.")
        else:
            try:
                total = parse_money(request.form.get("total"))
            except ValueError as e:
                errors.append(f"Total: {e}")
                total = 0
            if method == "share" and any(not u.ownership_share for u in units):
                errors.append("Some units have no ownership share; split equally or enter amounts per unit instead.")
        if not errors:
            sc = SpecialContribution(title=title[:200], description=request.form.get("description", "").strip() or None,
                                     total_cents=total, deadline=deadline, method=method, created_by_id=current_user.id)
            for unit_id, cents in allocate(total, method, units, custom).items():
                if cents:
                    sc.shares.append(SpecialShare(unit_id=unit_id, amount_cents=cents))
            db.session.add(sc)
            db.session.commit()
            flash("Special contribution created. Check the amounts, then bill the units.", "success")
            return redirect(url_for("fees.special", special_id=sc.id))
        for e in errors:
            flash(e, "error")
        return render_template("fees/special_form.html", units=units, form=request.form, methods=SPLIT_METHODS), 400
    return render_template("fees/special_form.html", units=units, methods=SPLIT_METHODS,
                           form={"method": "share", "deadline": ""})


def _get(special_id):
    return db.session.get(SpecialContribution, special_id) or abort(404)


@bp.route("/specials/<int:special_id>")
@permission_required("accounting")
def special(special_id):
    sc = _get(special_id)
    return render_template("fees/special.html", sc=sc, rows=sc.status_rows(), labels=SPECIAL_STATUS_LABELS,
                           methods=SPLIT_METHODS, today=date.today())


@bp.route("/specials/<int:special_id>/bill", methods=["POST"])
@permission_required("accounting_edit")
def bill_special(special_id):
    sc = _get(special_id)
    if sc.billed_at:
        flash("Already billed.", "info")
        return redirect(url_for("fees.special", special_id=sc.id))
    for sh in sc.shares:
        charge = Charge(unit_id=sh.unit_id, kind="special", due_date=sc.deadline, amount_cents=sh.amount_cents,
                        description=f"Special contribution — {sc.title} (due {sc.deadline:%b %d, %Y})"[:255])
        db.session.add(charge)
        db.session.flush()
        sh.charge_id = charge.id
        notify.send(notify.unit_owner_accounts(sh.unit_id),
                    f"Special contribution: {_money(sh.amount_cents)} due by {sc.deadline:%b %d, %Y}",
                    url=url_for("billing.index"), body=sc.title, kind="special")
    sc.billed_at = utcnow()
    db.session.commit()
    flash(f"Billed {len(sc.shares)} unit(s). Owners have been notified of their amount and the deadline.", "success")
    return redirect(url_for("fees.special", special_id=sc.id))


@bp.route("/specials/<int:special_id>/deadline", methods=["POST"])
@permission_required("accounting_edit")
def change_deadline(special_id):
    sc = _get(special_id)
    try:
        new = parse_date(request.form.get("deadline"), "Deadline")
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("fees.special", special_id=sc.id))
    old, sc.deadline = sc.deadline, new
    for sh in sc.shares:
        if sh.charge_id:
            charge = db.session.get(Charge, sh.charge_id)
            if charge:
                charge.due_date = new
                charge.description = f"Special contribution — {sc.title} (due {new:%b %d, %Y})"[:255]
    if sc.billed_at and new != old:
        notify.send([u for sh in sc.shares for u in notify.unit_owner_accounts(sh.unit_id)],
                    f"New deadline for “{sc.title}”: {new:%b %d, %Y}", url=url_for("billing.index"), kind="special")
    db.session.commit()
    flash(f"Deadline moved to {new:%B %d, %Y}.", "success")
    return redirect(url_for("fees.special", special_id=sc.id))


@bp.route("/specials/<int:special_id>/delete", methods=["POST"])
@permission_required("accounting_edit")
def delete_special(special_id):
    sc = _get(special_id)
    if sc.billed_at:
        flash("This contribution has been billed; it can't be deleted.", "error")
        return redirect(url_for("fees.special", special_id=sc.id))
    db.session.delete(sc)
    db.session.commit()
    flash("Special contribution deleted.", "success")
    return redirect(url_for("fees.index"))


def unit_summary(unit, year=None):
    """An owner's view: their annual fee for the year and their special contributions."""
    today = date.today()
    if year:
        schedule = db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year == year))
    else:  # the period running today, else the one for this calendar year
        schedule = next((s for s in db.session.scalars(db.select(FeeSchedule)) if s.start <= today <= s.end), None) \
            or db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year == today.year))
        year = schedule.year if schedule else today.year
    fee = None
    if schedule:
        fee = next((r for r in schedule.unit_fees(_units()) if r["unit"].id == unit.id), None)
    specials = []
    for sc in db.session.scalars(db.select(SpecialContribution).where(SpecialContribution.billed_at.is_not(None))
                                 .order_by(SpecialContribution.deadline.desc())):
        row = next((r for r in sc.status_rows() if r["unit"].id == unit.id), None)
        if row:
            specials.append((sc, row))
    return {"year": year, "fee": fee, "specials": specials, "schedule": schedule}
