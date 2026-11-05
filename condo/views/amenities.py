from datetime import datetime, time, timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Amenity, Booking
from ..utils import admin_required, parse_datetime

bp = Blueprint("amenities", __name__, url_prefix="/amenities")


def validate_booking(amenity, start, end, now=None):
    """Return a list of error strings for a proposed booking (empty if valid)."""
    now = now or datetime.now()
    errors = []
    if not amenity.active:
        errors.append(f"{amenity.name} is not currently available for booking.")
    if end <= start:
        errors.append("End time must be after start time.")
        return errors
    if start < now:
        errors.append("Bookings must be in the future.")
    if start.date() != end.date():
        errors.append("Bookings must start and end on the same day.")
    if (end - start) > timedelta(hours=amenity.max_hours):
        errors.append(f"{amenity.name} can be booked for at most {amenity.max_hours} hour(s).")
    day_start = datetime.combine(start.date(), time())
    if start < day_start + timedelta(hours=amenity.open_hour) or end > day_start + timedelta(hours=amenity.close_hour):
        errors.append(f"{amenity.name} is open {amenity.open_hour:02d}:00–{amenity.close_hour:02d}:00.")
    if Booking.conflicts(amenity.id, start, end):
        errors.append("That time overlaps an existing booking.")
    return errors


@bp.route("/")
@login_required
def index():
    stmt = db.select(Amenity).order_by(Amenity.name)
    if not current_user.is_admin:
        stmt = stmt.where(Amenity.active.is_(True))
    amenities = db.session.scalars(stmt).all()
    now = datetime.now()
    mine = db.session.scalars(
        db.select(Booking).where(Booking.user_id == current_user.id, Booking.end >= now).order_by(Booking.start)
    ).all()
    return render_template("amenities/index.html", amenities=amenities, mine=mine)


@bp.route("/<int:amenity_id>", methods=["GET", "POST"])
@login_required
def detail(amenity_id):
    amenity = db.session.get(Amenity, amenity_id) or abort(404)
    if not amenity.active and not current_user.is_admin:
        abort(404)
    form = {}
    status = 200
    if request.method == "POST":
        form = request.form
        try:
            day = request.form.get("date", "")
            start = parse_datetime(f"{day}T{request.form.get('start', '')}", "Start time")
            end = parse_datetime(f"{day}T{request.form.get('end', '')}", "End time")
            errors = validate_booking(amenity, start, end)
        except ValueError as e:
            errors = [str(e)]
        if not errors:
            db.session.add(Booking(amenity=amenity, user_id=current_user.id, start=start, end=end,
                                   notes=request.form.get("notes", "").strip() or None))
            db.session.commit()
            flash(f"{amenity.name} booked for {start:%a %b %d, %H:%M}–{end:%H:%M}.", "success")
            return redirect(url_for("amenities.detail", amenity_id=amenity.id))
        for e in errors:
            flash(e, "error")
        status = 400
    upcoming = db.session.scalars(
        db.select(Booking).where(Booking.amenity_id == amenity.id, Booking.end >= datetime.now())
        .order_by(Booking.start)
    ).all()
    return render_template("amenities/detail.html", amenity=amenity, upcoming=upcoming, form=form), status


@bp.route("/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel(booking_id):
    booking = db.session.get(Booking, booking_id) or abort(404)
    if booking.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    amenity_id = booking.amenity_id
    db.session.delete(booking)
    db.session.commit()
    flash("Booking cancelled.", "success")
    if request.form.get("back") == "list":
        return redirect(url_for("amenities.index"))
    return redirect(url_for("amenities.detail", amenity_id=amenity_id))


def _apply_amenity_form(amenity):
    errors = []
    name = request.form.get("name", "").strip()
    if not name:
        errors.append("Name is required.")
    else:
        existing = db.session.scalar(db.select(Amenity).where(Amenity.name == name))
        if existing and existing.id != amenity.id:
            errors.append("An amenity with that name already exists.")
    try:
        open_hour = int(request.form.get("open_hour", 8))
        close_hour = int(request.form.get("close_hour", 22))
        max_hours = int(request.form.get("max_hours", 4))
        if not (0 <= open_hour < close_hour <= 24):
            errors.append("Opening hours must satisfy 0 ≤ open < close ≤ 24.")
        if max_hours < 1:
            errors.append("Maximum booking length must be at least 1 hour.")
    except ValueError:
        errors.append("Hours must be whole numbers.")
    if errors:
        return errors
    amenity.name = name
    amenity.description = request.form.get("description", "").strip() or None
    amenity.open_hour, amenity.close_hour, amenity.max_hours = open_hour, close_hour, max_hours
    amenity.active = bool(request.form.get("active"))
    return []


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new():
    amenity = Amenity(active=True)
    if request.method == "POST":
        errors = _apply_amenity_form(amenity)
        if not errors:
            db.session.add(amenity)
            db.session.commit()
            flash(f"{amenity.name} added.", "success")
            return redirect(url_for("amenities.index"))
        for e in errors:
            flash(e, "error")
        return render_template("amenities/form.html", amenity=amenity, form=request.form), 400
    form = {"open_hour": 8, "close_hour": 22, "max_hours": 4, "active": True}
    return render_template("amenities/form.html", amenity=amenity, form=form)


@bp.route("/<int:amenity_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(amenity_id):
    amenity = db.session.get(Amenity, amenity_id) or abort(404)
    if request.method == "POST":
        errors = _apply_amenity_form(amenity)
        if not errors:
            db.session.commit()
            flash(f"{amenity.name} updated.", "success")
            return redirect(url_for("amenities.detail", amenity_id=amenity.id))
        db.session.rollback()
        for e in errors:
            flash(e, "error")
        return render_template("amenities/form.html", amenity=amenity, form=request.form), 400
    form = {"name": amenity.name, "description": amenity.description or "", "open_hour": amenity.open_hour,
            "close_hour": amenity.close_hour, "max_hours": amenity.max_hours, "active": amenity.active}
    return render_template("amenities/form.html", amenity=amenity, form=form)
