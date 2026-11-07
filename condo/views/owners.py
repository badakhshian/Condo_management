"""Owner contacts: several people per unit, with email, phone and mailing address."""
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import hand_over_policies, BOARD_POSITIONS, OWNER_ROLES, SINGLE_HOLDER_POSITIONS, Owner, Unit, allowed_positions
from ..utils import admin_required
from ..i18n import gettext as _

bp = Blueprint("owners", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _apply_form(owner, form, unit=None):
    errors = []
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    role = form.get("role", "owner")
    if not name:
        errors.append(_("Name is required."))
    if email and not EMAIL_RE.match(email):
        errors.append(_("That email address doesn't look right."))
    if role not in OWNER_ROLES:
        errors.append(_("Choose a role."))
    if errors:
        return errors
    owner.name = name[:200]
    owner.email = email or None
    owner.phone = form.get("phone", "").strip()[:60] or None
    address = form.get("mailing_address", "").strip()
    source_id = form.get("copy_address_from", "")
    target_unit_id = unit.id if unit is not None else owner.unit_id
    if not address and source_id.isdigit():
        source = db.session.get(Owner, int(source_id))
        # Privacy: an address can only be copied from someone in the same unit.
        if source is not None and source.mailing_address and target_unit_id is not None \
                and source.unit_id == target_unit_id:
            address = source.mailing_address
    owner.mailing_address = address or None
    owner.role = role
    owner.is_primary = bool(form.get("is_primary"))
    owner.notes = form.get("notes", "").strip() or None
    dropped = [str(BOARD_POSITIONS[p]) for p in owner.positions if p not in allowed_positions(role)]
    if dropped:  # e.g. an owner who became a tenant leaves the board
        owner.positions = [p for p in owner.positions if p in allowed_positions(role)]
        flash(_("%(name)s is no longer %(dropped)s: that role can't hold it.", name=owner.name, dropped=', '.join(dropped)), "info")
    return []


def claim_single_positions(owner):
    """President, vice-president, treasurer and secretary have one holder: giving one to
    `owner` takes it from whoever had it. Returns messages describing each move."""
    moved = []
    for key in set(owner.positions) & set(SINGLE_HOLDER_POSITIONS):
        others = db.session.scalars(db.select(Owner).where(Owner.board_roles.like(f"%{key}%"),
                                                           Owner.id != owner.id)).all()
        for other in others:
            if key in other.positions:
                other.positions = [p for p in other.positions if p != key]
                moved.append(f"{BOARD_POSITIONS[key]} moved from {other.name} to {owner.name}.")
    return moved


def address_sources(unit, owner=None):
    """Addresses that can be copied into this form: only from people in the same unit, so
    nobody sees another unit's owners' addresses. Each distinct address appears once.
    Returns (same_unit, []) — the second list is kept for the template's shape."""
    if unit is None:
        return [], []
    rows = db.session.scalars(
        db.select(Owner).where(Owner.unit_id == unit.id, Owner.mailing_address.is_not(None),
                               Owner.mailing_address != "").order_by(Owner.name)).all()
    same, seen = [], set()
    for o in rows:
        if owner is not None and o.id == owner.id:
            continue
        key = o.mailing_address.strip().lower()
        if key not in seen:
            seen.add(key)
            same.append(o)
    return same, []


def _only_one_primary(owner):
    if owner.is_primary:
        for other in owner.unit.owners:
            if other is not owner:
                other.is_primary = False


def _units():
    return db.session.scalars(db.select(Unit).order_by(Unit.number)).all()


def _back(owner, unit, from_directory=False):
    """After saving, return to the page the person was added or edited from (the Directory tab
    or the unit page), falling back to the unit."""
    from ..navigation import safe_back
    if safe_back():
        return safe_back()
    if from_directory or request.args.get("next") == "directory":
        from .directory import group_for_role
        return url_for("directory.contacts", group=group_for_role(owner.role))
    return url_for("units.detail", unit_id=unit.id)


@bp.route("/directory/<group>/new", methods=["GET", "POST"])
@admin_required
def new_from_directory(group):
    """Add a person from a Directory tab: the unit is picked in the form."""
    from .directory import GROUPS
    if group not in GROUPS:
        abort(404)
    default_role = GROUPS[group]["default_role"]
    raw = request.form.get("unit_id", "") if request.method == "POST" else request.args.get("unit_id", "")
    unit = db.session.get(Unit, int(raw)) if raw.isdigit() else None
    if request.method == "POST" and unit is None:
        flash(_("Choose a unit."), "error")
        return render_template("owners/form.html", unit=None, owner=None, units=_units(), sources=address_sources(None),
                               form=request.form, roles=OWNER_ROLES), 400
    if unit is None:
        return render_template("owners/form.html", unit=None, owner=None, units=_units(), sources=address_sources(None),
                               roles=OWNER_ROLES, form={"role": default_role})
    return new(unit.id, default_role=default_role, from_directory=True)


@bp.route("/units/<int:unit_id>/owners/new", methods=["GET", "POST"])
@admin_required
def new(unit_id, default_role=None, from_directory=False):
    unit = db.session.get(Unit, unit_id) or abort(404)
    default_role = default_role or (request.args.get("role") if request.args.get("role") in OWNER_ROLES else "owner")
    owner = Owner(role=default_role, is_primary=not unit.owners)  # attached to the unit only once valid
    units = _units() if from_directory else None
    if request.method == "POST":
        errors = _apply_form(owner, request.form, unit=unit)
        if not errors:
            owner.unit = unit
            db.session.add(owner)
            _only_one_primary(owner)
            db.session.flush()
            db.session.commit()
            flash(_("%(name)s added to unit %(number)s.", name=owner.name, number=unit.number), "success")
            return redirect(_back(owner, unit, from_directory))
        for e in errors:
            flash(e, "error")
        return render_template("owners/form.html", unit=unit, owner=None, units=units,
                               sources=address_sources(unit, None), form=request.form,
                               roles=OWNER_ROLES), 400
    return render_template("owners/form.html", unit=unit, owner=None, units=units,
                           sources=address_sources(unit, None), roles=OWNER_ROLES,
                           form={"role": default_role, "is_primary": owner.is_primary, "unit_id": unit.id})


def _get(owner_id):
    owner = db.session.get(Owner, owner_id) or abort(404)
    return owner


def _login_for(owner):
    """The sign-in account that belongs to this person (same email), if any."""
    from ..models import User
    if not owner.email:
        return None
    return db.session.scalar(db.select(User).where(db.func.lower(User.email) == owner.email.lower()))


@bp.route("/owners/<int:owner_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(owner_id):
    owner = _get(owner_id)
    if owner.origin == "sheet":
        flash(_("This owner comes from the Google Sheet's Owners tab. Change it there; the next sync updates the app."),
              "error")
        return redirect(url_for("units.detail", unit_id=owner.unit_id))
    if request.method == "POST":
        errors = _apply_form(owner, request.form)
        if not errors:
            _only_one_primary(owner)
            db.session.commit()
            flash(_("%(name)s updated.", name=owner.name), "success")
            return redirect(_back(owner, owner.unit))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return render_template("owners/form.html", unit=owner.unit, owner=owner, sources=address_sources(owner.unit, owner), login=_login_for(owner), form=request.form,
                               roles=OWNER_ROLES), 400
    form = {k: getattr(owner, k) or "" for k in ("name", "email", "phone", "mailing_address", "role", "notes")}
    form["is_primary"] = owner.is_primary
    return render_template("owners/form.html", unit=owner.unit, owner=owner, sources=address_sources(owner.unit, owner), login=_login_for(owner), form=form, roles=OWNER_ROLES)


@bp.route("/owners/<int:owner_id>/delete", methods=["POST"])
@admin_required
def delete(owner_id):
    owner = _get(owner_id)
    unit_id = owner.unit_id
    if owner.origin == "sheet":
        flash(_("This owner comes from the Google Sheet's Owners tab. Remove them there."), "error")
        return redirect(url_for("units.detail", unit_id=unit_id))
    kept = hand_over_policies(owner) if owner.policies else False
    db.session.delete(owner)
    db.session.commit()
    flash(_("%(name)s removed. Their household insurance now lists another member as policyholder.", name=owner.name)
          if kept else _("%(name)s removed.", name=owner.name), "success")
    from ..navigation import safe_back
    return redirect(safe_back() or url_for("units.detail", unit_id=unit_id))


@bp.route("/owners/")
@admin_required
def old_directory():
    return redirect(url_for("directory.contacts", group="owners"), code=301)
