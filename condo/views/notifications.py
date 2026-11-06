"""The notification bell: each user sees only notifications addressed to them."""
from urllib.parse import urlparse

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Notification, utcnow

bp = Blueprint("notifications", __name__, url_prefix="/notifications")


@bp.route("/")
@login_required
def index():
    show = request.args.get("show", "all")
    stmt = db.select(Notification).where(Notification.user_id == current_user.id)
    if show == "unread":
        stmt = stmt.where(Notification.read_at.is_(None))
    items = db.session.scalars(stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(200)).all()
    return render_template("notifications/index.html", items=items, show=show)


@bp.route("/<int:note_id>")
@login_required
def open(note_id):
    note = db.session.get(Notification, note_id)
    if note is None or note.user_id != current_user.id:
        abort(404)
    if note.read_at is None:
        note.read_at = utcnow()
        db.session.commit()
    target = note.url or url_for("notifications.index")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:  # only ever redirect inside the portal
        target = url_for("notifications.index")
    return redirect(target)


@bp.route("/read-all", methods=["POST"])
@login_required
def read_all():
    db.session.execute(db.update(Notification).where(Notification.user_id == current_user.id,
                                                    Notification.read_at.is_(None)).values(read_at=utcnow()))
    db.session.commit()
    return redirect(url_for("notifications.index"))
