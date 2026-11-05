from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Unit
from ..utils import admin_required, money, parse_money

bp = Blueprint("units", __name__, url_prefix="/units")


def _int_or_none(value, field, errors):
    value = (value or "").strip()
    if not value:
        return None
    try:
        n = int(value)
    except ValueError:
        errors.append(f"{field} must be a whole number.")
        return None
    if n < 0:
        errors.append(f"{field} cannot be negative.")
    return n


def _apply_form(unit, form):
    errors = []
    number = form.get("number", "").strip()
    if not number:
        errors.append("Unit number is required.")
    else:
        existing = db.session.scalar(db.select(Unit).where(Unit.number == number))
        if existing and existing.id != unit.id:
            errors.append(f"Unit {number} already exists.")
    fee_raw = form.get("monthly_fee", "").strip()
    fee = 0
    if fee_raw and fee_raw not in ("0", "0.00"):
        try:
            fee = parse_money(fee_raw)
        except ValueError as e:
            errors.append(f"Monthly fee: {e}")
    floor = _int_or_none(form.get("floor"), "Floor", errors)
    bedrooms = _int_or_none(form.get("bedrooms"), "Bedrooms", errors)
    area = _int_or_none(form.get("area_sqft"), "Area", errors)
    if errors:
        return errors
    unit.number = number
    unit.floor = floor
    unit.bedrooms = bedrooms
    unit.area_sqft = area
    unit.parking_spot = form.get("parking_spot", "").strip() or None
    unit.owner_name = form.get("owner_name", "").strip()[:200] or None
    unit.monthly_fee_cents = fee
    unit.notes = form.get("notes", "").strip() or None
    return []


@bp.route("/")
@admin_required
def index():
    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all()
    return render_template("units/index.html", units=units)


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new():
    unit = Unit()
    if request.method == "POST":
        errors = _apply_form(unit, request.form)
        if not errors:
            db.session.add(unit)
            db.session.commit()
            flash(f"Unit {unit.number} created.", "success")
            return redirect(url_for("units.detail", unit_id=unit.id))
        for e in errors:
            flash(e, "error")
        return render_template("units/form.html", unit=unit, form=request.form), 400
    return render_template("units/form.html", unit=unit, form={})


def _get_unit(unit_id):
    unit = db.session.get(Unit, unit_id)
    if unit is None:
        abort(404)
    return unit


@bp.route("/<int:unit_id>")
@admin_required
def detail(unit_id):
    unit = _get_unit(unit_id)
    return render_template("units/detail.html", unit=unit)


@bp.route("/<int:unit_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(unit_id):
    unit = _get_unit(unit_id)
    if request.method == "POST":
        errors = _apply_form(unit, request.form)
        if not errors:
            db.session.commit()
            flash(f"Unit {unit.number} updated.", "success")
            return redirect(url_for("units.detail", unit_id=unit.id))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return render_template("units/form.html", unit=unit, form=request.form), 400
    form = {
        "number": unit.number, "floor": unit.floor or "", "bedrooms": unit.bedrooms or "",
        "area_sqft": unit.area_sqft or "", "parking_spot": unit.parking_spot or "",
        "owner_name": unit.owner_name or "",
        "monthly_fee": money(unit.monthly_fee_cents).lstrip("$"), "notes": unit.notes or "",
    }
    return render_template("units/form.html", unit=unit, form=form)


@bp.route("/<int:unit_id>/delete", methods=["POST"])
@admin_required
def delete(unit_id):
    unit = _get_unit(unit_id)
    if unit.residents:
        flash("Reassign or remove the unit's residents before deleting it.", "error")
        return redirect(url_for("units.detail", unit_id=unit.id))
    if unit.charges or unit.payments:
        flash("This unit has billing history and cannot be deleted.", "error")
        return redirect(url_for("units.detail", unit_id=unit.id))
    db.session.delete(unit)
    db.session.commit()
    flash(f"Unit {unit.number} deleted.", "success")
    return redirect(url_for("units.index"))
