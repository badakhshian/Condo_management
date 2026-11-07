"""The Directory: one place for owners, tenants and website accounts (staff and others).

Owners and tenants are unit contacts (the Owner model, split by role); accounts are
website logins (the User model). Adding and editing still happen in the owners and
residents views; this module lists, searches and exports.
"""
import csv
import io

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import (ACCOUNT_ROLES, BOARD_POSITIONS, STAFF_PERMISSIONS, INSURANCE_STATE_LABELS, OWNER_ROLES, ROLE_LABELS, SINGLE_HOLDER_POSITIONS,
                      Owner, Unit, User, allowed_positions)
from ..utils import admin_required, board_has_accounts, board_positions_of, board_required, can_manage_accounts
from ..i18n import gettext as _, lazy_gettext as _l

bp = Blueprint("directory", __name__, url_prefix="/directory")

# Which contact roles appear on which tab.
GROUPS = {
    "owners": {"title": _l("Owners"), "roles": ("owner", "co-owner", "contact"), "default_role": "owner"},
    "tenants": {"title": _l("Tenants"), "roles": ("tenant",), "default_role": "tenant"},
}
TABS = [("owners", _l("Owners")), ("tenants", _l("Tenants")), ("board", _l("Board")), ("accounts", _l("Accounts"))]


def board_members():
    """[(position key, label, [people])] in board order, including vacant positions."""
    people = db.session.scalars(db.select(Owner).join(Unit).where(Owner.board_roles.is_not(None))
                                .order_by(Unit.number, Owner.name)).all()
    return [(key, label, [p for p in people if key in p.positions]) for key, label in BOARD_POSITIONS.items()]


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
    """Hired staff accounts (people who aren't owners or tenants), plus a temporary admin."""
    stmt = db.select(User).outerjoin(Unit).where(User.role.in_(("staff", "admin"))).order_by(User.name)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(db.or_(User.name.ilike(like), User.email.ilike(like), User.phone.ilike(like),
                                 User.job_title.ilike(like)))
    users = db.session.scalars(stmt).all()
    if role in STAFF_PERMISSIONS:
        users = [u for u in users if role in u.permission_keys]
    return users


def can_manage_board(user=None):
    user = user or current_user
    return user.is_authenticated and (user.is_admin or bool(board_positions_of(user)))


def visible_tabs():
    if current_user.is_admin:
        tabs = TABS
    elif current_user.can("directory"):
        tabs = [t for t in TABS if t[0] in ("owners", "tenants")]
    else:
        tabs = [t for t in TABS if t[0] == "board"]
    if not can_manage_accounts():
        tabs = [t for t in tabs if t[0] != "accounts"]
    elif not current_user.is_admin:
        tabs = tabs + [("accounts", _l("Accounts"))]
    return tabs


def _counts():
    counts = dict(db.session.execute(db.select(Owner.role, db.func.count(Owner.id)).group_by(Owner.role)).all())
    out = {g: sum(counts.get(r, 0) for r in spec["roles"]) for g, spec in GROUPS.items()}
    out["accounts"] = db.session.scalar(db.select(db.func.count(User.id)).where(User.role.in_(("staff", "admin"))))
    out["board"] = db.session.scalar(db.select(db.func.count(Owner.id)).where(Owner.board_roles.is_not(None)))
    return out


@bp.route("/")
@login_required
def index():
    if current_user.can("directory"):
        return redirect(url_for("directory.contacts", group="owners"))
    if can_manage_board():
        return redirect(url_for("directory.board"))
    abort(403)


@bp.route("/accounts")
@board_required
def accounts():
    q, role = request.args.get("q", "").strip(), request.args.get("role", "")
    users = search_accounts(q, role)
    emails = sorted({u.email for u in users if u.email and u.active})
    return render_template("directory/accounts.html", users=users, q=q, role=role, perms=STAFF_PERMISSIONS,
                           bootstrap=not board_has_accounts(),
                           emails=emails, tab="accounts", tabs=visible_tabs(), counts=_counts())


@bp.route("/board")
@login_required
def board():
    if not can_manage_board():
        abort(403)
    members = board_members()
    emails = sorted({p.email for _, _, people in members for p in people if p.email})
    candidates = db.session.scalars(db.select(Owner).join(Unit).where(Owner.role.in_(("owner", "co-owner", "contact")))
                                    .order_by(Unit.number, Owner.name)).all()
    eligible = {key: [c for c in candidates if key in allowed_positions(c.role)] for key in BOARD_POSITIONS}
    return render_template("directory/board.html", members=members, emails=emails, eligible=eligible,
                           single=SINGLE_HOLDER_POSITIONS, tab="board", tabs=visible_tabs(), counts=_counts())


@bp.route("/board/assign", methods=["POST"])
@login_required
def assign():
    if not can_manage_board():
        abort(403)
    key = request.form.get("position", "")
    raw = request.form.get("owner_id", "")
    person = db.session.get(Owner, int(raw)) if raw.isdigit() else None
    if key not in BOARD_POSITIONS or person is None:
        flash(_("Choose a person for the position."), "error")
        return redirect(url_for("directory.board"))
    if key not in allowed_positions(person.role):
        flash(_("%(name)s can't be %(position)s: only co-owners sit on the board (an outside contact can do Accounting).",
                name=person.name, position=BOARD_POSITIONS[key]) if person.role == "contact" else
              _("%(name)s can't be %(position)s: only co-owners sit on the board.", name=person.name,
                position=BOARD_POSITIONS[key]), "error")
        return redirect(url_for("directory.board"))
    if key in person.positions:
        flash(_("%(name)s is already %(key)s.", name=person.name, key=BOARD_POSITIONS[key]), "info")
        return redirect(url_for("directory.board"))
    if key in SINGLE_HOLDER_POSITIONS:
        for other in db.session.scalars(db.select(Owner).where(Owner.board_roles.like(f"%{key}%"))):
            if key in other.positions:
                other.positions = [p for p in other.positions if p != key]
                flash(_("%(key)s moved from %(name)s to %(name2)s.", key=BOARD_POSITIONS[key], name=other.name, name2=person.name), "info")
    person.positions = person.positions + [key]
    from ..models import link_admin_accounts
    converted = link_admin_accounts()
    db.session.commit()
    flash(_("%(name)s is now %(key)s.", name=person.name, key=BOARD_POSITIONS[key]), "success")
    for u in converted:
        flash(_("The temporary administrator account (%(email)s) is now %(name)s's own account, as %(key)s.", email=u.email, name=u.name, key=BOARD_POSITIONS[key]), "info")
    return redirect(url_for("directory.board"))


@bp.route("/board/unassign", methods=["POST"])
@login_required
def unassign():
    if not can_manage_board():
        abort(403)
    key = request.form.get("position", "")
    raw = request.form.get("owner_id", "")
    person = db.session.get(Owner, int(raw)) if raw.isdigit() else None
    if person is None or key not in person.positions:
        abort(400)
    person.positions = [p for p in person.positions if p != key]
    db.session.commit()
    flash(_("%(name)s is no longer %(key)s.", name=person.name, key=BOARD_POSITIONS[key]), "success")
    return redirect(url_for("directory.board"))


@bp.route("/<group>")
@login_required
def contacts(group):
    if not current_user.can("directory"):
        abort(403)
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
    logins = ({u.email.lower(): u for u in db.session.scalars(db.select(User).where(User.role.in_(("owner", "tenant", "admin"))))}
              if can_manage_accounts() else None)
    return render_template("directory/contacts.html", people=people, q=q, role=role, roles=role_choices,
                           logins=logins,
                           emails=emails, group=group, spec=GROUPS[group], units_without=without,
                           tab=group, tabs=visible_tabs(), counts=_counts())


@bp.route("/<group>/export.csv")
@login_required
def export(group):
    if not (can_manage_accounts() if group == "accounts" else current_user.can("directory")):
        abort(403)
    buf = io.StringIO()
    writer = csv.writer(buf)
    q, role = request.args.get("q", "").strip(), request.args.get("role", "")
    if group == "accounts":
        writer.writerow(["Name", "Job / company", "Email", "Phone", "Access", "Active"])
        for u in search_accounts(q, role):
            writer.writerow([u.name, u.job_title or "", u.email, u.phone or "",
                             "; ".join(STAFF_PERMISSIONS[k].split(":")[0] for k in u.permission_keys),
                             "yes" if u.active else "no"])
    elif group in GROUPS:
        writer.writerow(["Unit", "Name", "Role", "Board role", "Primary contact", "Email", "Phone",
                         "Mailing address", "Insurance", "Insurer", "Insured until", "Notes"])
        for o in search_contacts(group, q, role):
            p = o.current_policy
            writer.writerow([o.unit.number, o.name, o.role_label, ", ".join(o.position_labels),
                             "yes" if o.is_primary else "", o.email or "",
                             o.phone or "", (o.mailing_address or "").replace("\n", ", "),
                             INSURANCE_STATE_LABELS[o.insurance_state] if o.needs_insurance else "",
                             (p.provider or "") if p else "", p.valid_until.isoformat() if p else "", o.notes or ""])
    else:
        abort(404)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={group}.csv"})
