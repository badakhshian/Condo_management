import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import ROLE_LABELS, ROLES, Unit, User
from ..utils import admin_required

bp = Blueprint("residents", __name__, url_prefix="/residents")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _apply_form(user, form, creating):
    errors = []
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    role = form.get("role", "owner")
    unit_id = form.get("unit_id") or None
    password = form.get("password", "")

    if not name:
        errors.append("Name is required.")
    if not EMAIL_RE.match(email):
        errors.append("A valid email is required.")
    else:
        existing = db.session.scalar(db.select(User).where(User.email == email))
        if existing and existing.id != user.id:
            errors.append("Another account already uses that email.")
    if role not in ROLES:
        errors.append("Invalid role.")
    unit = None
    if unit_id:
        unit = db.session.get(Unit, int(unit_id)) if str(unit_id).isdigit() else None
        if unit is None:
            errors.append("Selected unit does not exist.")
    elif role != "admin":
        errors.append("Owners and tenants must be assigned to a unit.")
    if creating and len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    elif password and len(password) < 8:
        errors.append("New password must be at least 8 characters.")
    if not creating and user.id == current_user.id:
        if role != "admin":
            errors.append("You cannot remove your own manager role.")
        if not form.get("active"):
            errors.append("You cannot deactivate your own account.")
    if errors:
        return errors

    user.name = name
    user.email = email
    user.phone = form.get("phone", "").strip() or None
    user.role = role
    user.unit = unit
    user.active = bool(form.get("active")) if not creating else True
    if password:
        user.set_password(password)
    return []


def _units():
    return db.session.scalars(db.select(Unit).order_by(Unit.number)).all()


@bp.route("/")
@admin_required
def index():
    """Old address of the accounts list; it now lives in the Directory."""
    return redirect(url_for("directory.accounts"), code=301)


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new():
    user = User()
    if request.method == "POST":
        errors = _apply_form(user, request.form, creating=True)
        if not errors:
            db.session.add(user)
            db.session.commit()
            flash(f"Account for {user.name} created.", "success")
            return redirect(url_for("directory.accounts"))
        for e in errors:
            flash(e, "error")
        return render_template("residents/form.html", user=user, form=request.form, units=_units(),
                               roles=ROLE_LABELS, creating=True), 400
    form = {"unit_id": request.args.get("unit_id", ""), "role": "owner"}
    return render_template("residents/form.html", user=user, form=form, units=_units(),
                           roles=ROLE_LABELS, creating=True)


@bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if request.method == "POST":
        errors = _apply_form(user, request.form, creating=False)
        if not errors:
            db.session.commit()
            flash(f"{user.name} updated.", "success")
            return redirect(url_for("directory.accounts"))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return render_template("residents/form.html", user=user, form=request.form, units=_units(),
                               roles=ROLE_LABELS, creating=False), 400
    form = {"name": user.name, "email": user.email, "phone": user.phone or "", "role": user.role,
            "unit_id": str(user.unit_id or ""), "active": user.active}
    return render_template("residents/form.html", user=user, form=form, units=_units(),
                           roles=ROLE_LABELS, creating=False)
