from datetime import datetime

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from ..extensions import db
from ..models import ACTIVE_STATUSES, Announcement, Booking, MaintenanceRequest, Owner, Unit
from .documents import current_building_insurance
from .insurance import can_manage
from .maintenance import can_decide

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
            .where(MaintenanceRequest.status.in_(ACTIVE_STATUSES))
            .order_by(MaintenanceRequest.created_at.desc())
        ).all()
        stats = {
            "units": len(units),
            "occupied": sum(1 for u in units if u.owners or u.residents),
            "people": db.session.scalar(db.select(db.func.count(Owner.id))),
            "outstanding": outstanding,
            "open_requests": len(open_requests),
        }
        upcoming = db.session.scalars(
            db.select(Booking).where(Booking.end >= now).order_by(Booking.start).limit(5)
        ).all()
        awaiting = [r for r in open_requests if r.awaiting_owner]
        insurance_alerts = []
        for u in units:
            for group, label in (("owners", "Owners"), ("tenants", "Tenants")):
                state = u.group_insurance(group)
                if state in ("missing", "expired", "expiring"):
                    insurance_alerts.append((u, label, state))
        return render_template("dashboard_admin.html", stats=stats, delinquent=delinquent,
                               open_requests=[r for r in open_requests if not r.awaiting_owner][:5],
                               awaiting=awaiting, announcements=announcements, upcoming=upcoming,
                               insurance_alerts=insurance_alerts,
                               building=current_building_insurance(current_user))

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
    to_approve = [r for r in my_requests if can_decide(r)] if unit else []
    if unit and current_user.role == "owner":  # don't miss older ones beyond the latest five
        to_approve = db.session.scalars(db.select(MaintenanceRequest).where(
            MaintenanceRequest.unit_id == unit.id, MaintenanceRequest.status == "awaiting_owner")).all()
    my_insurance = [o for o in unit.owners if o.needs_insurance and can_manage(o)] if unit else []
    return render_template("dashboard_resident.html", unit=unit, my_requests=my_requests,
                           my_bookings=my_bookings, announcements=announcements, to_approve=to_approve,
                           my_insurance=my_insurance, building=current_building_insurance(current_user))
