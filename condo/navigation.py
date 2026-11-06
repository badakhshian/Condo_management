"""The "← Back" link shown on every page below the main menu pages.

Each page has a logical parent (used when the page was opened directly, e.g. from an
email link). In the browser, the link goes back to the previous portal page instead when
there is one, so filters, periods and tabs are kept.
"""
from flask import request, url_for
from flask_login import current_user

# Menu pages: no back link.
TOP_LEVEL = {
    "dashboard.index", "units.index", "directory.contacts", "directory.board", "directory.accounts",
    "directory.index", "billing.index", "maintenance.index", "announcements.index", "documents.index",
    "amenities.index", "auth.login", "billing.my_account", "notifications.index", "reimbursements.index",
}


def _unit_parent(unit_id):
    return ("Unit", url_for("units.detail", unit_id=unit_id)) if current_user.is_admin else \
        ("Dashboard", url_for("dashboard.index"))


def parent_for(endpoint, args):
    """(label, url) of the page a page belongs under, or None for menu pages."""
    from .extensions import db
    from .models import Owner

    if not endpoint or endpoint in TOP_LEVEL or endpoint == "static":
        return None
    admin = current_user.is_authenticated and current_user.is_admin
    if endpoint in ("units.detail", "units.new"):
        return "Units", url_for("units.index")
    if endpoint == "units.edit":
        return "Unit", url_for("units.detail", unit_id=args["unit_id"])
    if endpoint in ("owners.new", "insurance.new"):
        return _unit_parent(args["unit_id"])
    if endpoint == "owners.edit":
        owner = db.session.get(Owner, args["owner_id"])
        if request.args.get("next") == "directory" or owner is None:
            return "Directory", url_for("directory.index")
        return "Unit", url_for("units.detail", unit_id=owner.unit_id)
    if endpoint == "owners.new_from_directory":
        return "Directory", url_for("directory.contacts", group=args["group"])
    if endpoint.startswith("residents."):
        return "Accounts", url_for("directory.accounts")
    if endpoint.startswith("billing."):
        if admin or current_user.role == "owner":
            return "Accounting", url_for("billing.index")
        return "Dashboard", url_for("dashboard.index")
    if endpoint == "fees.index":
        return "Accounting", url_for("billing.index")
    if endpoint.startswith("fees."):
        return "Fees & contributions", url_for("fees.index")
    if endpoint.startswith("reimbursements."):
        return "Reimbursements", url_for("reimbursements.index")
    if endpoint.startswith("maintenance."):
        return "Maintenance", url_for("maintenance.index")
    if endpoint.startswith("announcements."):
        return "Notices", url_for("announcements.index")
    if endpoint.startswith("documents."):
        return "Documents", url_for("documents.index")
    if endpoint.startswith("amenities."):
        return "Amenities", url_for("amenities.index")
    if endpoint == "sheet.index":
        return "Accounting", url_for("billing.index")
    if endpoint.startswith("sheet."):
        return "Google Sheet", url_for("sheet.index")
    if endpoint == "importer.upload":
        return "Units", url_for("units.index")
    if endpoint.startswith("importer."):
        return "Import", url_for("importer.upload")
    return "Dashboard", url_for("dashboard.index")


def here():
    """The current page's path and query, to pass as ?back= on links into edit pages."""
    return request.full_path.rstrip("?")


def safe_back():
    """The ?back= page a link came from, if it is a page of this portal (never another site)."""
    target = request.args.get("back", "") or request.form.get("back", "")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def label_for(path):
    from .extensions import db
    from .models import Unit
    import re

    bare = path.split("?", 1)[0]
    m = re.match(r"^/units/(\d+)$", bare)
    if m:
        unit = db.session.get(Unit, int(m.group(1)))
        return f"unit {unit.number}" if unit else "the unit"
    for prefix, label in (("/directory", "Directory"), ("/accounting", "Accounting"), ("/maintenance", "Maintenance"),
                          ("/documents", "Documents"), ("/reimbursements", "Reimbursements"), ("/units", "Units"),
                          ("/notifications", "Notifications"), ("/sheet", "Google Sheet")):
        if bare.startswith(prefix):
            return label
    return "the previous page"


def back_link():
    try:
        if not current_user.is_authenticated:
            return None
        came_from = safe_back()
        if came_from and request.endpoint not in TOP_LEVEL:
            return {"label": label_for(came_from), "url": came_from}
        parent = parent_for(request.endpoint, request.view_args or {})
    except Exception:  # never break a page over its back link
        return None
    if parent is None:
        return None
    return {"label": parent[0], "url": parent[1]}
