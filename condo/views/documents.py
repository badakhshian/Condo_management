"""Shared documents: syndicate documents, building insurance, annual meeting minutes."""
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .. import files
from ..extensions import db
from ..models import DOCUMENT_CATEGORIES, DOCUMENT_VISIBILITY, Document, insurance_state
from ..utils import admin_required, parse_date
from ..i18n import gettext as _

bp = Blueprint("documents", __name__, url_prefix="/documents")


def visible_documents(user, category=None):
    stmt = db.select(Document).order_by(Document.category, db.func.coalesce(
        Document.valid_until, Document.document_date, db.func.date(Document.created_at)).desc(), Document.id.desc())
    if category:
        stmt = stmt.where(Document.category == category)
    if not user.can("documents"):
        allowed = ["everyone"] + (["owners"] if user.role == "owner" else [])
        stmt = stmt.where(Document.visibility.in_(allowed))
    return db.session.scalars(stmt).all()


def current_building_insurance(user, today=None):
    """The building policy covering today (or the latest one), if the user may see it."""
    today = today or date.today()
    policies = [d for d in visible_documents(user, "insurance")]
    covering = [d for d in policies if (d.valid_from or date.min) <= today <= (d.valid_until or date.max)]
    pick = max(covering or policies, key=lambda d: (d.valid_until or date.min, d.id), default=None)
    if pick is None:
        return None
    return {"doc": pick, "state": insurance_state(pick.valid_until, today)}


@bp.route("/")
@login_required
def index():
    docs = visible_documents(current_user)
    grouped = {key: [d for d in docs if d.category == key] for key in DOCUMENT_CATEGORIES}
    return render_template("documents/index.html", grouped=grouped, categories=DOCUMENT_CATEGORIES,
                           visibility=DOCUMENT_VISIBILITY, building=current_building_insurance(current_user),
                           human_size=files.human_size)


def _apply_form(doc, form, file, creating):
    errors = []
    title = form.get("title", "").strip()
    category = form.get("category", "other")
    visibility = form.get("visibility", "everyone")
    if not title:
        errors.append(_("Give the document a title."))
    if category not in DOCUMENT_CATEGORIES:
        errors.append(_("Choose a category."))
    if visibility not in DOCUMENT_VISIBILITY:
        errors.append(_("Choose who can see it."))
    dates = {}
    for field, label in (("document_date", "Date"), ("valid_from", "Start date"), ("valid_until", "End date")):
        raw = form.get(field, "").strip()
        if raw:
            try:
                dates[field] = parse_date(raw, label)
            except ValueError as exc:
                errors.append(str(exc))
        else:
            dates[field] = None
    if category == "insurance" and not (dates.get("valid_from") and dates.get("valid_until")):
        errors.append(_("Building insurance needs the policy's start and end dates."))
    if dates.get("valid_from") and dates.get("valid_until") and dates["valid_until"] < dates["valid_from"]:
        errors.append(_("The end date is before the start date."))
    stored = files.take_upload(file, errors, required=creating)
    if errors:
        return errors
    doc.title = title[:200]
    doc.category = category
    doc.visibility = visibility
    doc.description = form.get("description", "").strip() or None
    doc.provider = form.get("provider", "").strip()[:200] or None
    doc.policy_number = form.get("policy_number", "").strip()[:100] or None
    doc.document_date, doc.valid_from, doc.valid_until = dates["document_date"], dates["valid_from"], dates["valid_until"]
    if stored is not None:
        doc.file = stored
    return []


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new():
    doc = Document()
    if request.method == "POST":
        errors = _apply_form(doc, request.form, request.files.get("file"), creating=True)
        if not errors:
            db.session.add(doc)
            db.session.commit()
            flash(_("“%(doc)s” uploaded.", doc=doc.title), "success")
            return redirect(url_for("documents.index"))
        for e in errors:
            flash(e, "error")
        return render_template("documents/form.html", doc=None, form=request.form, categories=DOCUMENT_CATEGORIES,
                               visibility=DOCUMENT_VISIBILITY, accept=files.ACCEPT), 400
    category = request.args.get("category", "syndicate")
    return render_template("documents/form.html", doc=None, categories=DOCUMENT_CATEGORIES,
                           visibility=DOCUMENT_VISIBILITY, accept=files.ACCEPT,
                           form={"category": category if category in DOCUMENT_CATEGORIES else "other",
                                 "visibility": "owners" if category == "minutes" else "everyone"})


@bp.route("/<int:doc_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(doc_id):
    doc = db.session.get(Document, doc_id) or abort(404)
    if request.method == "POST":
        errors = _apply_form(doc, request.form, request.files.get("file"), creating=False)
        if not errors:
            db.session.commit()
            flash(_("“%(doc)s” updated.", doc=doc.title), "success")
            return redirect(url_for("documents.index"))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return render_template("documents/form.html", doc=doc, form=request.form, categories=DOCUMENT_CATEGORIES,
                               visibility=DOCUMENT_VISIBILITY, accept=files.ACCEPT), 400
    form = {k: getattr(doc, k) or "" for k in ("title", "category", "description", "provider", "policy_number",
                                               "visibility")}
    for k in ("document_date", "valid_from", "valid_until"):
        form[k] = getattr(doc, k).isoformat() if getattr(doc, k) else ""
    return render_template("documents/form.html", doc=doc, form=form, categories=DOCUMENT_CATEGORIES,
                           visibility=DOCUMENT_VISIBILITY, accept=files.ACCEPT)


@bp.route("/<int:doc_id>/delete", methods=["POST"])
@admin_required
def delete(doc_id):
    doc = db.session.get(Document, doc_id) or abort(404)
    db.session.delete(doc)
    db.session.commit()
    flash(_("“%(doc)s” deleted.", doc=doc.title), "success")
    return redirect(url_for("documents.index"))


@bp.route("/<int:doc_id>/file")
@login_required
def download(doc_id):
    doc = db.session.get(Document, doc_id) or abort(404)
    if not doc.visible_to(current_user):
        abort(403)
    return files.send(doc.file)
