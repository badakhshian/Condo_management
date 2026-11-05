from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (REQUEST_CATEGORIES, REQUEST_PRIORITIES, REQUEST_STATUSES, MaintenanceComment,
                      MaintenanceRequest, Unit, utcnow)

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
        stmt = stmt.where(MaintenanceRequest.status.in_(("open", "in_progress")))
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
            db.session.add(req)
            db.session.commit()
            flash("Maintenance request submitted.", "success")
            return redirect(url_for("maintenance.detail", request_id=req.id))
        for e in errors:
            flash(e, "error")
        return render_template("maintenance/form.html", form=request.form, units=units,
                               categories=REQUEST_CATEGORIES, priorities=REQUEST_PRIORITIES), 400
    return render_template("maintenance/form.html", form={"priority": "normal", "location": "unit"},
                           units=units, categories=REQUEST_CATEGORIES, priorities=REQUEST_PRIORITIES)


def _get_visible(request_id):
    req = db.session.get(MaintenanceRequest, request_id) or abort(404)
    if not _visible(req):
        abort(403)
    return req


@bp.route("/<int:request_id>")
@login_required
def detail(request_id):
    req = _get_visible(request_id)
    return render_template("maintenance/detail.html", req=req, statuses=REQUEST_STATUSES,
                           priorities=REQUEST_PRIORITIES)


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
