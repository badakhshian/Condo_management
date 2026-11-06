from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import files, notify
from ..extensions import db
from ..utils import board_positions_of, parse_date, parse_money
from ..models import (ACTIVE_STATUSES, QUOTE_STATUSES, REQUEST_CATEGORIES, REQUEST_PRIORITIES, REQUEST_STATUSES,
                      MaintenanceComment, MaintenanceRequest, Unit, User, utcnow)

bp = Blueprint("maintenance", __name__, url_prefix="/maintenance")


def is_board(user=None):
    user = user or current_user
    return user.is_admin or bool(board_positions_of(user))


TABS = {"projects": "Projects", "maintenance": "Maintenance"}
KIND_OF_TAB = {"projects": "project", "maintenance": "maintenance"}


def sees_all(user=None):
    user = user or current_user
    return is_board(user) or user.can("maintenance")


def can_add_projects(user=None):
    """Projects are building work the board plans; residents follow them but don't create them."""
    return sees_all(user)


def _visible(req):
    """The board and repair staff see everything; residents see their unit's requests, their own,
    and the building's projects."""
    return (sees_all() or req.submitted_by_id == current_user.id
            or (req.unit_id is not None and req.unit_id == current_user.unit_id)
            or (req.kind == "project" and req.unit_id is None))


def _url(req):
    return url_for("maintenance.detail", request_id=req.id)


def _tell_unit_side(req, title, body=None):
    """The person who asked, plus the unit's owner accounts."""
    notify.send([req.submitted_by] + notify.unit_owner_accounts(req.unit_id), title, url=_url(req), body=body,
                kind="maintenance")


@bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "projects")
    if tab not in TABS:
        tab = "projects"
    status = request.args.get("status", "active")
    base = db.select(MaintenanceRequest)
    if not sees_all():
        conds = [MaintenanceRequest.submitted_by_id == current_user.id,
                 db.and_(MaintenanceRequest.kind == "project", MaintenanceRequest.unit_id.is_(None))]
        if current_user.unit_id:
            conds.append(MaintenanceRequest.unit_id == current_user.unit_id)
        base = base.where(db.or_(*conds))
    counts = {key: db.session.scalar(db.select(db.func.count()).select_from(
        base.where(MaintenanceRequest.kind == kind, MaintenanceRequest.status.in_(ACTIVE_STATUSES)).subquery()))
        for key, kind in KIND_OF_TAB.items()}
    stmt = base.where(MaintenanceRequest.kind == KIND_OF_TAB[tab]).order_by(MaintenanceRequest.created_at.desc())
    if status == "active":
        stmt = stmt.where(MaintenanceRequest.status.in_(ACTIVE_STATUSES))
    elif status in REQUEST_STATUSES:
        stmt = stmt.where(MaintenanceRequest.status == status)
    requests_ = db.session.scalars(stmt).all()
    return render_template("maintenance/index.html", requests=requests_, status=status, tab=tab, tabs=TABS,
                           counts=counts, can_add_projects=can_add_projects(),
                           statuses=REQUEST_STATUSES, priorities=REQUEST_PRIORITIES)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    picks_unit = current_user.is_admin or current_user.is_staff
    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all() if picks_unit else []
    values = request.form if request.method == "POST" else request.args
    kind = "project" if values.get("kind") == "project" and can_add_projects() else "maintenance"
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
        budget = target = None
        if kind == "project":
            if request.form.get("budget", "").strip():
                try:
                    budget = parse_money(request.form.get("budget"))
                except ValueError as e:
                    errors.append(f"Budget: {e}")
            if request.form.get("target_date", "").strip():
                try:
                    target = parse_date(request.form.get("target_date"), "Target date")
                except ValueError as e:
                    errors.append(str(e))

        unit_id = None
        if location == "unit":
            if picks_unit:
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
            req = MaintenanceRequest(title=title, description=description, category=category, kind=kind,
                                     priority=priority, unit_id=unit_id, submitted_by_id=current_user.id,
                                     budget_cents=budget, target_date=target)
            needs_owner = kind == "maintenance" and needs_owner_approval(current_user, unit_id)
            if needs_owner:
                req.status, req.owner_approval = "awaiting_owner", "pending"
            db.session.add(req)
            db.session.flush()
            where = f"unit {req.unit.number}" if req.unit_id else "common area"
            if needs_owner:
                notify.send(notify.unit_owner_accounts(unit_id), f"Your tenant asked for a repair: {title}",
                            url=_url(req), body=f"{current_user.name}: {description}", kind="maintenance")
            elif kind == "project":
                notify.send([u for u in notify.admins() if u.id != current_user.id],
                            f"New project ({where}): {title}", url=_url(req),
                            body=f"{current_user.name}: {description}", kind="maintenance")
            else:
                notify.send(notify.admins(), f"New repair request ({where}): {title}", url=_url(req),
                            body=f"{current_user.name}: {description}", kind="maintenance")
            db.session.commit()
            flash("Request sent to the unit's owner for approval." if needs_owner
                  else "Project added." if kind == "project" else "Maintenance request submitted.", "success")
            return redirect(url_for("maintenance.detail", request_id=req.id))
        for e in errors:
            flash(e, "error")
        return render_template("maintenance/form.html", form=request.form, units=units, kind=kind,
                               categories=REQUEST_CATEGORIES, priorities=REQUEST_PRIORITIES), 400
    return render_template("maintenance/form.html", units=units, kind=kind,
                           form={"priority": "normal", "location": "common" if kind == "project" else "unit"},
                           categories=REQUEST_CATEGORIES, priorities=REQUEST_PRIORITIES)


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
                           priorities=REQUEST_PRIORITIES, can_decide=can_decide(req), owner_accounts=owners,
                           board=is_board(), unit_owner=is_unit_owner(req), accept=files.ACCEPT,
                           can_projects=can_add_projects())


def is_unit_owner(req, user=None):
    user = user or current_user
    return user.is_admin or (user.role == "owner" and req.unit_id is not None and user.unit_id == req.unit_id)


@bp.route("/<int:request_id>/ask-quote", methods=["POST"])
@login_required
def ask_quote(request_id):
    """Board: the owner should get a quote and have the cost approved before doing the repair."""
    req = _get_visible(request_id)
    if not is_board() or req.unit_id is None or req.status not in ("open", "in_progress"):
        abort(403)
    note = request.form.get("note", "").strip()
    req.status, req.quote_note = "quote_requested", note or None
    req.quote_amount_cents = req.quote_file_id = req.quote_approved_cents = None
    db.session.add(MaintenanceComment(request=req, author_id=current_user.id,
                                      body="The board asked the owner to get a quote for board approval."
                                      + (f" {note}" if note else "")))
    _tell_unit_side(req, f"Please get a quote for: {req.title}",
                    "Upload the quote on the request; once the board approves the cost, go ahead with the repair "
                    "and request a reimbursement." + (f" Board's note: {note}" if note else ""))
    db.session.commit()
    flash("The owner has been asked for a quote.", "success")
    return redirect(_url(req))


@bp.route("/<int:request_id>/quote", methods=["POST"])
@login_required
def submit_quote(request_id):
    req = _get_visible(request_id)
    if req.status != "quote_requested" or not is_unit_owner(req):
        abort(403)
    errors = []
    try:
        amount = parse_money(request.form.get("amount"))
    except ValueError as e:
        errors.append(str(e))
    stored = files.take_upload(request.files.get("file"), errors, required=True)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(_url(req))
    req.quote_amount_cents, req.quote_file = amount, stored
    req.quote_vendor = request.form.get("vendor", "").strip()[:200] or None
    req.status = "quote_review"
    db.session.add(MaintenanceComment(request=req, author_id=current_user.id,
                                      body=f"Quote submitted: ${amount / 100:,.2f}"
                                      + (f" from {req.quote_vendor}" if req.quote_vendor else "") + "."))
    notify.send(notify.board(), f"Quote to approve: unit {req.unit.number}, ${amount / 100:,.2f} — {req.title}",
                url=_url(req), kind="maintenance")
    db.session.commit()
    flash("Quote sent to the board for approval.", "success")
    return redirect(_url(req))


@bp.route("/<int:request_id>/quote-decision", methods=["POST"])
@login_required
def quote_decision(request_id):
    req = _get_visible(request_id)
    if req.status != "quote_review" or not is_board():
        abort(403)
    decision = request.form.get("decision")
    note = request.form.get("note", "").strip()
    if decision not in ("approve", "decline"):
        abort(400)
    if decision == "decline" and not note:
        flash("Tell the owner why, e.g. ask for another quote.", "error")
        return redirect(_url(req))
    req.quote_decided_by, req.quote_decided_at, req.quote_decision_note = current_user.name, utcnow(), note or None
    if decision == "approve":
        raw = request.form.get("approved_amount", "").strip()
        try:
            approved = parse_money(raw) if raw else req.quote_amount_cents
        except ValueError as e:
            flash(str(e), "error")
            return redirect(_url(req))
        req.quote_approved_cents, req.status = approved, "owner_repair"
        msg = f"Quote approved for ${approved / 100:,.2f}. The owner can do the repair and request a reimbursement."
        _tell_unit_side(req, f"Quote approved (${approved / 100:,.2f}): go ahead with {req.title}",
                        "When it's done and paid, open the request and click Request reimbursement."
                        + (f" Board's note: {note}" if note else ""))
    else:
        req.status = "quote_requested"
        msg = f"Quote declined: {note}"
        _tell_unit_side(req, f"Quote not approved: {req.title}", note)
    db.session.add(MaintenanceComment(request=req, author_id=current_user.id, body=f"{msg} ({current_user.name})"))
    db.session.commit()
    flash("Decision recorded and the owner notified.", "success")
    return redirect(_url(req))


@bp.route("/<int:request_id>/quote-file")
@login_required
def quote_file(request_id):
    req = _get_visible(request_id)
    if req.quote_file is None:
        abort(404)
    return files.send(req.quote_file)


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
    notify.send([req.submitted_by], f"The owner {verb} your repair request: {req.title}", url=_url(req),
                body=note or None, kind="maintenance")
    if decision == "approve":
        notify.send(notify.admins(), f"Repair approved by the owner, ready to schedule: {req.title}", url=_url(req),
                    kind="maintenance")
    db.session.commit()
    flash(f"Repair {verb}." + (" The manager can now schedule it." if decision == "approve" else ""), "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))


@bp.route("/<int:request_id>/project", methods=["POST"])
@login_required
def project_details(request_id):
    """The board files an item under Projects or Maintenance and sets a project's budget and target date."""
    req = _get_visible(request_id)
    if not can_add_projects():
        abort(403)
    kind = request.form.get("kind", req.kind)
    if kind not in ("project", "maintenance"):
        abort(400)
    errors, budget, target = [], None, None
    if kind == "project":
        if request.form.get("budget", "").strip():
            try:
                budget = parse_money(request.form.get("budget"))
            except ValueError as e:
                errors.append(f"Budget: {e}")
        if request.form.get("target_date", "").strip():
            try:
                target = parse_date(request.form.get("target_date"), "Target date")
            except ValueError as e:
                errors.append(str(e))
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(_url(req))
    moved = kind != req.kind
    req.kind = kind
    if kind == "project":
        req.budget_cents, req.target_date = budget, target
    if moved:
        db.session.add(MaintenanceComment(request=req, author_id=current_user.id,
                                          body=f"Moved to {'Projects' if kind == 'project' else 'Maintenance'}."))
    db.session.commit()
    flash("Moved to Projects." if moved and kind == "project" else "Moved to Maintenance." if moved
          else "Project details saved.", "success")
    return redirect(_url(req))


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
        others = [req.submitted_by] + ([] if current_user.is_admin else notify.admins())
        notify.send(others, f"New comment on “{req.title}” from {current_user.name}", url=_url(req), body=body,
                    kind="maintenance")
        db.session.commit()
        flash("Comment added.", "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))


@bp.route("/<int:request_id>/status", methods=["POST"])
@login_required
def update_status(request_id):
    req = _get_visible(request_id)
    new_status = request.form.get("status")
    new_priority = request.form.get("priority", req.priority)
    if current_user.can("maintenance"):
        if new_status not in REQUEST_STATUSES or new_priority not in REQUEST_PRIORITIES:
            abort(400)
        if req.awaiting_owner and new_status not in ("awaiting_owner", "closed"):
            flash("This repair is waiting for the owner. Record the owner's approval first.", "error")
            return redirect(url_for("maintenance.detail", request_id=req.id))
        if new_status == "awaiting_owner" and not req.awaiting_owner:
            abort(400)
        if new_status in QUOTE_STATUSES and new_status != req.status:
            flash("Use the quote buttons to move a request through the quote step.", "error")
            return redirect(url_for("maintenance.detail", request_id=req.id))
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
        notify.send([req.submitted_by], f"Your repair request is now {REQUEST_STATUSES[new_status].lower()}: {req.title}",
                    url=_url(req), kind="maintenance")
    db.session.commit()
    flash("Request updated.", "success")
    return redirect(url_for("maintenance.detail", request_id=req.id))
