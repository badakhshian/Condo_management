from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Announcement
from ..utils import admin_required

bp = Blueprint("announcements", __name__, url_prefix="/announcements")


@bp.route("/")
@login_required
def index():
    items = db.session.scalars(
        db.select(Announcement).order_by(Announcement.pinned.desc(), Announcement.created_at.desc())
    ).all()
    return render_template("announcements/index.html", items=items)


def _save(item):
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    if not title or not body:
        flash("Title and message are both required.", "error")
        return False
    item.title, item.body = title, body
    item.pinned = bool(request.form.get("pinned"))
    return True


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new():
    item = Announcement()
    if request.method == "POST":
        if _save(item):
            item.author_id = current_user.id
            db.session.add(item)
            db.session.commit()
            flash("Announcement posted.", "success")
            return redirect(url_for("announcements.index"))
        return render_template("announcements/form.html", item=item, form=request.form), 400
    return render_template("announcements/form.html", item=item, form={})


@bp.route("/<int:item_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(item_id):
    item = db.session.get(Announcement, item_id) or abort(404)
    if request.method == "POST":
        if _save(item):
            db.session.commit()
            flash("Announcement updated.", "success")
            return redirect(url_for("announcements.index"))
        return render_template("announcements/form.html", item=item, form=request.form), 400
    form = {"title": item.title, "body": item.body, "pinned": item.pinned}
    return render_template("announcements/form.html", item=item, form=form)


@bp.route("/<int:item_id>/delete", methods=["POST"])
@admin_required
def delete(item_id):
    item = db.session.get(Announcement, item_id) or abort(404)
    db.session.delete(item)
    db.session.commit()
    flash("Announcement deleted.", "success")
    return redirect(url_for("announcements.index"))
