"""The Directory: one place for owners, tenants and website accounts (staff and others).

Owners and tenants are unit contacts (the Owner model, split by role); accounts are
website logins (the User model). Adding and editing still happen in the owners and
residents views; this module lists, searches and exports.
"""
import csv
import io

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for

from ..extensions import db
from ..models import OWNER_ROLES, ROLE_LABELS, Owner, Unit, User
from ..utils import admin_required

bp = Blueprint("directory", __name__, url_prefix="/directory")

# Which contact roles appear on which tab.
GROUPS = {
    "owners": {"title": "Owners", "roles": ("owner", "co-owner", "contact"), "default_role": "owner"},
    "tenants": {"title": "Tenants", "roles": ("tenant",), "default_role": "tenant"},
}
TABS = [("owners", "Owners"), ("tenants", "Tenants"), ("accounts", "Accounts")]


def group_for_role(role):
    return "tenants" if role == "tenant" else "owners"


def search_contacts(group, q="", role=""):
    roles = GROUPS[group]["roles"]
    if role in roles:
        roles = (role,)
    stmt = (db.select(Owner).join(Unit).where(Owner.role.in_(roles))
            .order_by(Unit.number, Owner.is_primary.desc(), Owner.name))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(db.or_(Owner.name.ilike(like), Owner.email.ilike(like), Owner.phone.ilike(like),
                                 Owner.mailing_address.ilike(like), Owner.notes.ilike(like), Unit.number.ilike(like)))
    return db.session.scalars(stmt).all()


def search_accounts(q="", role=""):
    stmt = db.select(User).outerjoin(Unit).order_by(User.role != "admin", User.name)
    if role in ROLE_LABELS:
        stmt = stmt.where(User.role == role)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(db.or_(User.name.ilike(like), User.email.ilike(like), User.phone.ilike(like),
                                 Unit.number.ilike(like)))
    return db.session.scalars(stmt).all()


def _counts():
    counts = dict(db.session.execute(db.select(Owner.role, db.func.count(Owner.id)).group_by(Owner.role)).all())
    out = {g: sum(counts.get(r, 0) for r in spec["roles"]) for g, spec in GROUPS.items()}
    out["accounts"] = db.session.scalar(db.select(db.func.count(User.id)))
    return out


@bp.route("/")
@admin_required
def index():
    return redirect(url_for("directory.contacts", group="owners"))


@bp.route("/accounts")
@admin_required
def accounts():
    q, role = request.args.get("q", "").strip(), request.args.get("role", "")
    users = search_accounts(q, role)
    emails = sorted({u.email for u in users if u.email and u.active})
    return render_template("directory/accounts.html", users=users, q=q, role=role, roles=ROLE_LABELS,
                           emails=emails, tab="accounts", tabs=TABS, counts=_counts())


@bp.route("/<group>")
@admin_required
def contacts(group):
    if group not in GROUPS:
        abort(404)
    q, role = request.args.get("q", "").strip(), request.args.get("role", "")
    people = search_contacts(group, q, role)
    emails = sorted({p.email for p in people if p.email})
    role_choices = {r: OWNER_ROLES[r] for r in GROUPS[group]["roles"]}
    without = []
    if group == "owners" and not (q or role):
        without = db.session.scalars(db.select(Unit).where(
            ~Unit.owners.any(Owner.role.in_(GROUPS["owners"]["roles"]))).order_by(Unit.number)).all()
    return render_template("directory/contacts.html", people=people, q=q, role=role, roles=role_choices,
                           emails=emails, group=group, spec=GROUPS[group], units_without=without,
                           tab=group, tabs=TABS, counts=_counts())


@bp.route("/<group>/export.csv")
@admin_required
def export(group):
    buf = io.StringIO()
    writer = csv.writer(buf)
    q, role = request.args.get("q", "").strip(), request.args.get("role", "")
    if group == "accounts":
        writer.writerow(["Name", "Role", "Unit", "Email", "Phone", "Active"])
        for u in search_accounts(q, role):
            writer.writerow([u.name, u.role_label, u.unit.number if u.unit else "", u.email, u.phone or "",
                             "yes" if u.active else "no"])
    elif group in GROUPS:
        writer.writerow(["Unit", "Name", "Role", "Primary contact", "Email", "Phone", "Mailing address", "Notes"])
        for o in search_contacts(group, q, role):
            writer.writerow([o.unit.number, o.name, o.role_label, "yes" if o.is_primary else "", o.email or "",
                             o.phone or "", (o.mailing_address or "").replace("\n", ", "), o.notes or ""])
    else:
        abort(404)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={group}.csv"})
