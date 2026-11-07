"""Reimbursement requests: an owner paid a building cost and asks for it back.

Flow: the owner submits it with receipts and chooses credit-on-dues or pay-me-back →
the President approves or declines → Accounting (or the Treasurer) records it. A credit
becomes a purchase credit on the unit (an owner-paid building cost); a payback becomes a
building cost. Everyone involved is notified at each step.
"""
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import files, notify
from ..extensions import db
from ..models import (COST_CATEGORIES, REIMBURSEMENT_STATUSES, SETTLE_METHODS, Expense, MaintenanceComment,
                      MaintenanceRequest, Payment, Reimbursement, ReimbursementFile, Unit, utcnow)
from ..utils import board_positions_of, money, parse_date, parse_money
from ..i18n import gettext as _, lazy_gettext as _l, lazy_money

bp = Blueprint("reimbursements", __name__, url_prefix="/reimbursements")


def _holds(user, keys):
    return bool(set(board_positions_of(user)) & set(keys))


def is_president(user=None):
    """The President decides; managers stand in while no President has an account."""
    user = user or current_user
    return _holds(user, ["president"]) or (user.is_admin and not notify.with_positions(["president"]))


def is_accounting(user=None):
    user = user or current_user
    return _holds(user, ["accounting", "treasurer"]) or (
        user.is_admin and not notify.with_positions(["accounting", "treasurer"]))


def can_request(user=None):
    user = user or current_user
    return user.is_admin or (user.role == "owner" and user.unit_id is not None)


def can_see_all(user=None):
    user = user or current_user
    return user.is_admin or is_president(user) or is_accounting(user) or bool(board_positions_of(user))


def can_view(r, user=None):
    user = user or current_user
    return can_see_all(user) or r.requested_by_id == user.id or (
        user.role == "owner" and user.unit_id is not None and user.unit_id == r.unit_id)


def _get(reimb_id):
    r = db.session.get(Reimbursement, reimb_id) or abort(404)
    if not can_view(r):
        abort(403)
    return r


def _url(r):
    return url_for("reimbursements.detail", reimb_id=r.id)


def _note_on_request(r, text):
    if r.request_id:
        db.session.add(MaintenanceComment(request_id=r.request_id, author_id=current_user.id, body=text))


@bp.route("/")
@login_required
def index():
    if not (can_request() or can_see_all()):
        abort(403)
    show = request.args.get("show", "")
    stmt = db.select(Reimbursement).order_by(Reimbursement.created_at.desc(), Reimbursement.id.desc())
    if not can_see_all():
        stmt = stmt.where(Reimbursement.unit_id == current_user.unit_id)
    waiting_for_me = []
    if is_president():
        waiting_for_me.append("submitted")
    if is_accounting():
        waiting_for_me.append("approved")
    if not show:
        show = "mine" if waiting_for_me else "all"
    if show == "mine" and waiting_for_me:
        stmt = stmt.where(Reimbursement.status.in_(waiting_for_me))
    elif show in REIMBURSEMENT_STATUSES:
        stmt = stmt.where(Reimbursement.status == show)
    items = db.session.scalars(stmt).all()
    return render_template("reimbursements/index.html", items=items, show=show, waiting_for_me=waiting_for_me,
                           statuses=REIMBURSEMENT_STATUSES, can_request=can_request())


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if not can_request():
        abort(403)
    units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all() if current_user.is_admin else None
    repair = None
    raw_req = request.values.get("request_id", "")
    if raw_req.isdigit():
        repair = db.session.get(MaintenanceRequest, int(raw_req))
        if repair is None or not (current_user.is_admin or repair.unit_id == current_user.unit_id):
            repair = None
    if request.method == "POST":
        errors = []
        if current_user.is_admin:
            raw = request.form.get("unit_id", "")
            unit = db.session.get(Unit, int(raw)) if raw.isdigit() else None
        else:
            unit = current_user.unit
        if unit is None:
            errors.append(_("Choose the unit that paid."))
        description = request.form.get("description", "").strip()
        if not description:
            errors.append(_("Say what was paid for."))
        try:
            amount = parse_money(request.form.get("amount"))
        except ValueError as e:
            errors.append(str(e))
        try:
            spent_on = parse_date(request.form.get("spent_on"), "Date paid")
        except ValueError as e:
            errors.append(str(e))
        method = request.form.get("settle_method", "credit")
        if method not in SETTLE_METHODS:
            errors.append(_("Choose how you'd like to be reimbursed."))
        category = request.form.get("category", "repairs")
        if category not in COST_CATEGORIES:
            category = "other"
        uploads = [f for f in request.files.getlist("files") if f and f.filename]
        if not uploads:
            errors.append(_("Attach the receipt or invoice."))
        stored = [files.take_upload(f, errors) for f in uploads]
        if not errors:
            r = Reimbursement(unit=unit, requested_by_id=current_user.id, request=repair,
                              description=description[:255], vendor=request.form.get("vendor", "").strip()[:200] or None,
                              amount_cents=amount, spent_on=spent_on, category=category, settle_method=method)
            for s in stored:
                r.files.append(ReimbursementFile(file=s))
            db.session.add(r)
            db.session.flush()
            _note_on_request(r, f"Reimbursement of ${amount / 100:,.2f} requested.")
            notify.send(notify.president(), _l("Reimbursement to approve: unit %(number)s, %(amount)s", number=unit.number, amount=lazy_money(amount)),
                        url=_url(r), body=description, kind="reimbursement")
            db.session.commit()
            flash(_("Sent to the president for approval. You'll be notified of the decision."), "success")
            return redirect(_url(r))
        for e in errors:
            flash(e, "error")
        return render_template("reimbursements/form.html", form=request.form, units=units, repair=repair,
                               methods=SETTLE_METHODS, categories=COST_CATEGORIES, accept=files.ACCEPT), 400
    form = {"spent_on": date.today().isoformat(), "settle_method": "credit", "category": "repairs"}
    if repair is not None:
        form.update(description=repair.title, vendor=repair.quote_vendor or "",
                    amount=f"{(repair.quote_approved_cents or repair.quote_amount_cents or 0) / 100:.2f}"
                    if (repair.quote_approved_cents or repair.quote_amount_cents) else "")
    return render_template("reimbursements/form.html", form=form, units=units, repair=repair, methods=SETTLE_METHODS,
                           categories=COST_CATEGORIES, accept=files.ACCEPT)


@bp.route("/<int:reimb_id>")
@login_required
def detail(reimb_id):
    r = _get(reimb_id)
    return render_template("reimbursements/detail.html", r=r, methods=SETTLE_METHODS, categories=COST_CATEGORIES,
                           can_decide=r.status == "submitted" and is_president(),
                           can_settle=r.status == "approved" and is_accounting(),
                           human_size=files.human_size)


@bp.route("/<int:reimb_id>/decide", methods=["POST"])
@login_required
def decide(reimb_id):
    r = _get(reimb_id)
    if r.status != "submitted" or not is_president():
        abort(403)
    decision = request.form.get("decision")
    note = request.form.get("note", "").strip()
    if decision not in ("approve", "decline"):
        abort(400)
    if decision == "decline" and not note:
        flash(_("Please give the owner a reason for declining."), "error")
        return redirect(_url(r))
    r.status = "approved" if decision == "approve" else "declined"
    r.decided_by, r.decided_at, r.decision_note = current_user.name, utcnow(), note or None
    amount = f"${r.amount_cents / 100:,.2f}"  # for the stored note
    if decision == "approve":
        notify.send([r.requested_by], _l("Reimbursement approved: %(amount)s", amount=lazy_money(r.amount_cents)), url=_url(r),
                    body=_l("Accounting will now record it."), kind="reimbursement", include_actor=True)
        notify.send(notify.accounting(), _l("Approved reimbursement to record: unit %(number)s, %(amount)s", number=r.unit.number, amount=lazy_money(r.amount_cents)),
                    url=_url(r), body=_l("%(settle_method)s: %(description)s", settle_method=SETTLE_METHODS[r.settle_method], description=r.description), kind="reimbursement")
        _note_on_request(r, f"Reimbursement of {amount} approved by {current_user.name}.")
    else:
        notify.send([r.requested_by], _l("Reimbursement declined: %(amount)s", amount=lazy_money(r.amount_cents)), url=_url(r), body=note,
                    kind="reimbursement", include_actor=True)
        _note_on_request(r, f"Reimbursement of {amount} declined by {current_user.name}: {note}")
    db.session.commit()
    flash(_("Approved. Accounting has been notified.") if decision == "approve" else _("Declined. The owner has been told why."),
          "success")
    return redirect(_url(r))


@bp.route("/<int:reimb_id>/settle", methods=["POST"])
@login_required
def settle(reimb_id):
    r = _get(reimb_id)
    if r.status != "approved" or not is_accounting():
        abort(403)
    try:
        when = parse_date(request.form.get("settled_on") or date.today().isoformat(), "Date")
    except ValueError as e:
        flash(str(e), "error")
        return redirect(_url(r))
    reference = request.form.get("reference", "").strip()[:120] or None
    label = f"Reimbursement #{r.id} — {r.description}"
    if r.settle_method == "credit":
        payment = Payment(unit=r.unit, amount_cents=r.amount_cents, paid_on=when, method="purchase credit",
                          notes=label[:255], reference=reference, recorded_by_id=current_user.id)
        db.session.add(payment)
        db.session.flush()
        r.payment_id = payment.id
        done = f"credited to unit {r.unit.number}'s account"
    else:
        expense = Expense(spent_on=when, category=r.category, amount_cents=-r.amount_cents,
                          description=f"Reimbursed to unit {r.unit.number}: {r.description}"[:255],
                          approved_by=r.decided_by, source=reference or r.vendor, recorded_by_id=current_user.id)
        db.session.add(expense)
        db.session.flush()
        r.expense_id = expense.id
        done = "paid back"
    r.status, r.settled_by, r.settled_at, r.settle_reference = "settled", current_user.name, utcnow(), reference
    amount = f"${r.amount_cents / 100:,.2f}"
    if r.settle_method == "credit":
        title = _l("Reimbursement settled: %(amount)s credited to unit %(number)s's account",
                   amount=lazy_money(r.amount_cents), number=r.unit.number)
        msg = _("Recorded: %(amount)s credited to unit %(number)s's account.", amount=money(r.amount_cents), number=r.unit.number)
    else:
        title = _l("Reimbursement settled: %(amount)s paid back", amount=lazy_money(r.amount_cents))
        msg = _("Recorded: %(amount)s paid back.", amount=money(r.amount_cents))
    notify.send([r.requested_by], title, url=_url(r), kind="reimbursement", include_actor=True)
    _note_on_request(r, f"Reimbursement of {amount} {done}.")
    db.session.commit()
    flash(msg, "success")
    return redirect(_url(r))


@bp.route("/files/<int:file_row_id>")
@login_required
def download(file_row_id):
    row = db.session.get(ReimbursementFile, file_row_id) or abort(404)
    if not can_view(row.reimbursement):
        abort(403)
    return files.send(row.file)
