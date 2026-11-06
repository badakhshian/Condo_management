from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import User

bp = Blueprint("auth", __name__)


def _safe_next(target):
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return None
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = db.session.scalar(db.select(User).where(User.email == email))
        if user and user.check_password(password):
            if not user.active:
                flash("This account has been deactivated. Contact the management office.", "error")
            else:
                login_user(user, remember=bool(request.form.get("remember")))
                if user.must_change_password:
                    flash("Welcome! Please choose your own password to continue.", "info")
                    return redirect(url_for("auth.account"))
                return redirect(_safe_next(request.args.get("next")) or url_for("dashboard.index"))
        else:
            flash("Invalid email or password.", "error")
    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.before_app_request
def require_new_password():
    """Someone whose password was set by the board must choose their own before going on."""
    if (current_user.is_authenticated and getattr(current_user, "must_change_password", False)
            and request.endpoint not in ("auth.account", "auth.logout", "static")):
        flash("Please choose your own password to continue.", "info")
        return redirect(url_for("auth.account"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        phone = request.form.get("phone", "").strip()
        if new or current:
            if not current_user.check_password(current):
                flash("Current password is incorrect.", "error")
                return render_template("auth/account.html"), 400
            if len(new) < 8:
                flash("New password must be at least 8 characters.", "error")
                return render_template("auth/account.html"), 400
            if new != confirm:
                flash("New passwords do not match.", "error")
                return render_template("auth/account.html"), 400
            if current_user.must_change_password and new == current:
                flash("Choose a new password, different from the one you were given.", "error")
                return render_template("auth/account.html"), 400
            current_user.set_password(new)
            current_user.must_change_password = False
        current_user.phone = phone or None
        db.session.commit()
        flash("Account updated.", "success")
        return redirect(url_for("auth.account"))
    return render_template("auth/account.html")
