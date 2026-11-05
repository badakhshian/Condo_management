"""Owner contacts: several people per unit, with email, phone and mailing address."""
import csv
import io
import re

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import OWNER_ROLES, Owner, Unit
from ..utils import admin_required

bp = Blueprint("owners", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _apply_form(owner, form):
    errors = []
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    role = form.get("role", "owner")
    if not name:
        errors.append("Name is required.")
    if email and not EMAIL_RE.match(email):
        errors.append("That email address doesn't look right.")
    if role not in OWNER_ROLES:
        errors.append("Choose a role.")
    if errors:
        return errors
    owner.name = name[:200]
    owner.email = email or None
    owner.phone = form.get("phone", "").strip()[:60] or None
    owner.mailing_address = form.get("mailing_address", "").strip() or None
    owner.role = role
    owner.is_primary = bool(form.get("is_primary"))
    owner.notes = form.get("notes", "").strip() or None
    return []


def _only_one_primary(owner):
    if owner.is_primary:
        for other in owner.unit.owners:
            if other is not owner:
                other.is_primary = False


@bp.route("/units/<int:unit_id>/owners/new", methods=["GET", "POST"])
@admin_required
def new(unit_id):
    unit = db.session.get(Unit, unit_id) or abort(404)
    owner = Owner(role="owner", is_primary=not unit.owners)  # attached to the unit only once valid
    if request.method == "POST":
        errors = _apply_form(owner, request.form)
        if not errors:
            owner.unit = unit
            db.session.add(owner)
            _only_one_primary(owner)
            db.session.commit()
            flash(f"{owner.name} added to unit {unit.number}.", "success")
            return redirect(url_for("units.detail", unit_id=unit.id))
        for e in errors:
            flash(e, "error")
        return render_template("owners/form.html", unit=unit, owner=None, form=request.form,
                               roles=OWNER_ROLES), 400
    return render_template("owners/form.html", unit=unit, owner=None, roles=OWNER_ROLES,
                           form={"role": "owner", "is_primary": owner.is_primary})


def _get(owner_id):
    owner = db.session.get(Owner, owner_id) or abort(404)
    return owner


@bp.route("/owners/<int:owner_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(owner_id):
    owner = _get(owner_id)
    if owner.origin == "sheet":
        flash("This owner comes from the Google Sheet's Owners tab. Change it there; the next sync updates the app.",
              "error")
        return redirect(url_for("units.detail", unit_id=owner.unit_id))
    if request.method == "POST":
        errors = _apply_form(owner, request.form)
        if not errors:
            _only_one_primary(owner)
            db.session.commit()
            flash(f"{owner.name} updated.", "success")
            return redirect(url_for("units.detail", unit_id=owner.unit_id))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return render_template("owners/form.html", unit=owner.unit, owner=owner, form=request.form,
                               roles=OWNER_ROLES), 400
    form = {k: getattr(owner, k) or "" for k in ("name", "email", "phone", "mailing_address", "role", "notes")}
    form["is_primary"] = owner.is_primary
    return render_template("owners/form.html", unit=owner.unit, owner=owner, form=form, roles=OWNER_ROLES)


@bp.route("/owners/<int:owner_id>/delete", methods=["POST"])
@admin_required
def delete(owner_id):
    owner = _get(owner_id)
    unit_id = owner.unit_id
    if owner.origin == "sheet":
        flash("This owner comes from the Google Sheet's Owners tab. Remove them there.", "error")
        return redirect(url_for("units.detail", unit_id=unit_id))
    db.session.delete(owner)
    db.session.commit()
    flash(f"{owner.name} removed.", "success")
    return redirect(url_for("units.detail", unit_id=unit_id))


def _search(q, role):
    stmt = db.select(Owner).join(Unit).order_by(Unit.number, Owner.is_primary.desc(), Owner.name)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(db.or_(Owner.name.ilike(like), Owner.email.ilike(like), Owner.phone.ilike(like),
                                 Owner.mailing_address.ilike(like), Unit.number.ilike(like)))
    if role in OWNER_ROLES:
        stmt = stmt.where(Owner.role == role)
    return db.session.scalars(stmt).all()


@bp.route("/owners/")
@admin_required
def directory():
    q, role = request.args.get("q", "").strip(), request.args.get("role", "")
    owners = _search(q, role)
    emails = sorted({o.email for o in owners if o.email})
    units_without = db.session.scalars(
        db.select(Unit).where(~Unit.owners.any()).order_by(Unit.number)).all()
    return render_template("owners/directory.html", owners=owners, q=q, role=role, roles=OWNER_ROLES,
                           emails=emails, units_without=units_without)


@bp.route("/owners/export.csv")
@admin_required
def export():
    owners = _search(request.args.get("q", "").strip(), request.args.get("role", ""))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Unit", "Name", "Role", "Primary contact", "Email", "Phone", "Mailing address", "Notes"])
    for o in owners:
        writer.writerow([o.unit.number, o.name, o.role_label, "yes" if o.is_primary else "", o.email or "",
                         o.phone or "", (o.mailing_address or "").replace("\n", ", "), o.notes or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=owners.csv"})
