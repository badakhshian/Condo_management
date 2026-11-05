"""Home-insurance policies for owners and tenants (certificate file + policy period)."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import files
from ..extensions import db
from ..models import InsurancePolicy, Owner
from ..utils import parse_date

bp = Blueprint("insurance", __name__)


def can_manage(person, user=None):
    """Managers: anyone. A unit's owner account: anyone in that unit (e.g. their tenant).
    Any other account: only the contact with their own email in their own unit."""
    user = user or current_user
    if user.is_admin:
        return True
    if user.unit_id is None or user.unit_id != person.unit_id:
        return False
    if user.role == "owner":
        return True
    return bool(person.email) and person.email.lower() == user.email.lower()


def can_view(policy, user=None):
    user = user or current_user
    return user.is_admin or (user.unit_id is not None and user.unit_id == policy.owner.unit_id)


def _back(person):
    if request.args.get("next") == "dashboard" or not current_user.is_admin:
        return url_for("dashboard.index")
    return url_for("units.detail", unit_id=person.unit_id)


@bp.route("/people/<int:owner_id>/insurance/new", methods=["GET", "POST"])
@login_required
def new(owner_id):
    person = db.session.get(Owner, owner_id) or abort(404)
    if not can_manage(person):
        abort(403)
    if request.method == "POST":
        errors, dates = [], {}
        for field, label in (("valid_from", "Start date"), ("valid_until", "End date")):
            try:
                dates[field] = parse_date(request.form.get(field), label)
            except ValueError as exc:
                errors.append(str(exc))
        if len(dates) == 2 and dates["valid_until"] < dates["valid_from"]:
            errors.append("The end date is before the start date.")
        stored = files.take_upload(request.files.get("file"), errors, required=True)
        if not errors:
            db.session.add(InsurancePolicy(
                owner=person, file=stored, valid_from=dates["valid_from"], valid_until=dates["valid_until"],
                provider=request.form.get("provider", "").strip()[:200] or None,
                policy_number=request.form.get("policy_number", "").strip()[:100] or None))
            db.session.commit()
            flash(f"Insurance saved for {person.name}.", "success")
            return redirect(_back(person))
        for e in errors:
            flash(e, "error")
        return render_template("insurance/form.html", person=person, form=request.form, accept=files.ACCEPT), 400
    last = person.current_policy
    form = {"provider": last.provider if last else "", "policy_number": last.policy_number if last else ""}
    return render_template("insurance/form.html", person=person, form=form, accept=files.ACCEPT)


@bp.route("/insurance/<int:policy_id>/file")
@login_required
def download(policy_id):
    policy = db.session.get(InsurancePolicy, policy_id) or abort(404)
    if not can_view(policy) or policy.file is None:
        abort(403 if policy.file is not None else 404)
    return files.send(policy.file)


@bp.route("/insurance/<int:policy_id>/delete", methods=["POST"])
@login_required
def delete(policy_id):
    policy = db.session.get(InsurancePolicy, policy_id) or abort(404)
    if not current_user.is_admin:
        abort(403)
    person = policy.owner
    db.session.delete(policy)
    db.session.commit()
    flash("Insurance policy removed.", "success")
    return redirect(url_for("units.detail", unit_id=person.unit_id))
