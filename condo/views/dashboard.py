from datetime import datetime

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Announcement, Booking, MaintenanceRequest, Unit, User

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    now = datetime.now()
    announcements = db.session.scalars(
        db.select(Announcement).order_by(Announcement.pinned.desc(), Announcement.created_at.desc()).limit(5)
    ).all()

    if current_user.is_admin:
        units = db.session.scalars(db.select(Unit).order_by(Unit.number)).all()
        balances = [(u, u.balance) for u in units]
        outstanding = sum(b for _, b in balances if b > 0)
        delinquent = sorted([(u, b) for u, b in balances if b > 0], key=lambda x: -x[1])[:5]
        open_requests = db.session.scalars(
            db.select(MaintenanceRequest)
            .where(MaintenanceRequest.status.in_(("open", "in_progress")))
            .order_by(MaintenanceRequest.created_at.desc())
        ).all()
        stats = {
            "units": len(units),
            "occupied": sum(1 for u in units if u.residents),
            "residents": db.session.scalar(db.select(db.func.count(User.id)).where(User.role != "admin")),
            "outstanding": outstanding,
            "open_requests": len(open_requests),
        }
        upcoming = db.session.scalars(
            db.select(Booking).where(Booking.end >= now).order_by(Booking.start).limit(5)
        ).all()
        return render_template("dashboard_admin.html", stats=stats, delinquent=delinquent,
                               open_requests=open_requests[:5], announcements=announcements,
                               upcoming=upcoming)

    unit = current_user.unit
    my_requests = []
    if unit:
        my_requests = db.session.scalars(
            db.select(MaintenanceRequest).where(MaintenanceRequest.unit_id == unit.id)
            .order_by(MaintenanceRequest.created_at.desc()).limit(5)
        ).all()
    my_bookings = db.session.scalars(
        db.select(Booking).where(Booking.user_id == current_user.id, Booking.end >= now)
        .order_by(Booking.start)
    ).all()
    return render_template("dashboard_resident.html", unit=unit, my_requests=my_requests,
                           my_bookings=my_bookings, announcements=announcements)
