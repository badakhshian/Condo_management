from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (ACTIVE_STATUSES, REQUEST_CATEGORIES, REQUEST_PRIORITIES, REQUEST_STATUSES,
                      MaintenanceComment, MaintenanceRequest, Unit, User, utcnow)

bp = Blueprint("maintenance", __name__, url_prefix="/maintenance")


def _visible(req):
    """Residents see requests for their own unit, plus any request they submitted."""
    return (current_user.is_admin or req.submitted_by_id == current_user.id
            or (req.unit_id is not None and req.unit_id == current_user.unit_id))


@bp.route("/")
@login_required
def index():
    status = request.args.get("status", "active")
    stmt = db.select(MaintenanceRequest).order_by(MaintenanceRequest.created_at.desc())
    if not current_user.is_admin:
        conds = [MaintenanceRequest.submitted_by_id == current_user.id]
        if current_user.unit_id:
            conds.append(MaintenanceRequest.unit_id == current_user.unit_id)
        stmt = stmt.where(db.or_(*conds))
    if status == "active":
        stmt = stmt.where(MaintenanceRequest.status.in_(ACTIVE_STATUSES))
    elif status in REQUEST_STATUSES:
        stmt = stmt.where(MaintenanceRequest.status == status)
    requests_ = db.session.scalars(stmt).all()
    return render_template("maintenance/index.html", requests=requests_, status=status,
                           statuses=REQUEST_STATUSES, priorities=REQUEST_PRIORITIES)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all() if current_user.is_admin else []
    if request.method == "POST":
        errors = []
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "Other")
        priority = request.form.get("priority", "normal")
        location = request.form.get("location", "unit")
        if not title:
            errors.append("A short title is required.")
        if not description:
            errors.append("Please describe the problem.")
        if category not in REQUEST_CATEGORIES:
            errors.append("Choose a category.")
        if priority not in REQUEST_PRIORITIES:
            errors.append("Choose a priority.")

        unit_id = None
        if location == "unit":
            if current_user.is_admin:
                raw = request.form.get("unit_id", "")
                unit = db.session.get(Unit, int(raw)) if raw.isdigit() else None
                if unit is None:
                    errors.append("Select a unit, or mark this as a common-area issue.")
                else:
                    unit_id = unit.id
            elif current_user.unit_id is None:
                errors.append("Your account is not linked to a unit; report it as a common-area issue.")
            else:
                unit_id = current_user.unit_id

        if not errors:
            req = MaintenanceRequest(title=title, description=description, category=category,
                                     priority=priority, unit_id=unit_id, submitted_by_id=current_user.id)
            needs_owner = needs_owner_approval(current_user, unit_id)
            if needs_owner:
                req.status, req.owner_approval = "awaiting_owner", "pending"
            db.session.add(req)
            db.session.commit()
            flash("Request sent to the unit's owner for approval." if needs_owner
                  else "Maintenance request submitted.", "success")
            return redirect(url_for("maintenance.detail", request_id=req.id))
        for e in errors:
            flash(e, "error")
        return render_template("maintenance/form.html", form=request.form, units=units,
                               categories=REQUEST_CATEGORIES, priorities=REQUEST_PRIORITIES), 400
    return render_template("maintenance/form.html", form={"priority": "normal", "location": "unit"},
                           units=units, categories=REQUEST_CATEGORIES, priorities=REQUEST_PRIORITIES)


def needs_owner_approval(user, unit_id):
    """Repairs a tenant asks for in their unit need the owner's go-ahead first."""
    return unit_id is not None and not user.is_admin and user.role == "tenant"


def can_decide(req, user=None):
    """Owner accounts of the unit decide; managers can record a decision made by phone or email."""
    user = user or current_user
    if not req.awaiting_owner:
        return False
    return user.is_admin or (user.role == "owner" and user.unit_id is not None and user.unit_id == req.unit_id)


def owner_accounts(unit_id):
    return db.session.scalars(db.select(User).where(User.unit_id == unit_id, User.role == "owner",
                                                    User.active.is_(True))).all()


def _get_visible(request_id):
    req = db.session.get(MaintenanceRequest, request_id) or abort(404)
    if not _visible(req):
        abort(403)
    return req


@bp.route("/<int:request_id>")
@login_required
def detail(request_id):
    req = _get_visible(request_id)
    owners = owner_accounts(req.unit_id) if req.awaiting_owner and req.unit_id else []
    return render_template("maintenance/detail.html", req=req, statuses=REQUEST_STATUSES,
                           priorities=REQUEST_PRIORITIES, can_decide=can_decide(req), owner_accounts=owners)


@bp.route("/<int:request_id>/owner-decision", methods=["POST"])
@login_required
def owner_decision(request_id):
    req = _get_visible(request_id)
    if not can_decide(req):
        abort(403)
    decision = request.form.get("decision")
    if decision not in ("approve", "decline"):
        abort(400)
    note = request.form.get("note", "").strip()
    if decision == "decline" and not note:
        flash("Please say why the repair is declined, so the tenant knows.", "error")
        return redirect(url_for("maintenance.detail", request_id=req.id))
    who = current_user.name
    if current_user.is_admin:
        on_behalf = request.form.get("on_behalf", "").strip()
        who = f"{current_user.name} (manager), on behalf of {on_behalf or 'the owner'}"
    req.owner_approval = "approved" if decision == "approve" else "declined"
    req.owner_decision_by, req.owner_decision_at, req.owner_decision_note = who[:200], utcnow(), note or None
    req.status = "open" if decision == "approve" else "closed"
    verb = "approved" if decision == "approve" else "declined"
    db.session.add(MaintenanceComment(request=req, author_id=current_user.id,
                                      body=f"Owner {verb} this repair ({who})." + (f" Note: {note}" if note else "")))
    db.session.commit()
    flash(f"Repair {verb}." + (" The manager can now schedule it." if decision == "approve" else ""), "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))


@bp.route("/<int:request_id>/comment", methods=["POST"])
@login_required
def comment(request_id):
    req = _get_visible(request_id)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Comment cannot be empty.", "error")
    else:
        db.session.add(MaintenanceComment(request=req, author_id=current_user.id, body=body))
        req.updated_at = utcnow()
        db.session.commit()
        flash("Comment added.", "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))


@bp.route("/<int:request_id>/status", methods=["POST"])
@login_required
def update_status(request_id):
    req = _get_visible(request_id)
    new_status = request.form.get("status")
    new_priority = request.form.get("priority", req.priority)
    if current_user.is_admin:
        if new_status not in REQUEST_STATUSES or new_priority not in REQUEST_PRIORITIES:
            abort(400)
        if req.awaiting_owner and new_status not in ("awaiting_owner", "closed"):
            flash("This repair is waiting for the owner. Record the owner's approval first.", "error")
            return redirect(url_for("maintenance.detail", request_id=req.id))
        if new_status == "awaiting_owner" and not req.awaiting_owner:
            abort(400)
    else:
        # Residents may only close their own request (e.g. "fixed itself") or reopen a resolved one.
        allowed = {"closed"} if req.is_active else {"open"} if req.status == "resolved" else set()
        if req.submitted_by_id != current_user.id or new_status not in allowed:
            abort(403)
        new_priority = req.priority
    old = req.status
    req.status = new_status
    req.priority = new_priority
    if old != new_status:
        db.session.add(MaintenanceComment(
            request=req, author_id=current_user.id,
            body=f"Status changed from {REQUEST_STATUSES[old]} to {REQUEST_STATUSES[new_status]}."))
    db.session.commit()
    flash("Request updated.", "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))
