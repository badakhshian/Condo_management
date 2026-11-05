from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


ROLES = ("admin", "owner", "tenant")
ROLE_LABELS = {"admin": "Manager", "owner": "Owner", "tenant": "Tenant"}


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(40))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="owner")
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"))
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit", back_populates="residents")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_active(self):  # Flask-Login hook: deactivated users cannot log in
        return self.active

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class Unit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(20), unique=True, nullable=False)
    floor = db.Column(db.Integer)
    bedrooms = db.Column(db.Integer)
    area_sqft = db.Column(db.Integer)
    parking_spot = db.Column(db.String(20))
    monthly_fee_cents = db.Column(db.Integer, nullable=False, default=0)
    owner_name = db.Column(db.String(200))
    ownership_share = db.Column(db.Float)  # fraction of the building, e.g. 0.1161
    notes = db.Column(db.Text)

    residents = db.relationship("User", back_populates="unit", order_by="User.name")
    charges = db.relationship("Charge", back_populates="unit", cascade="all, delete-orphan",
                              order_by="Charge.due_date.desc()")
    payments = db.relationship("Payment", back_populates="unit", cascade="all, delete-orphan",
                               order_by="Payment.paid_on.desc()")

    @property
    def total_charged(self):
        return db.session.scalar(
            db.select(func.coalesce(func.sum(Charge.amount_cents), 0)).where(Charge.unit_id == self.id)
        )

    @property
    def total_paid(self):
        return db.session.scalar(
            db.select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(Payment.unit_id == self.id)
        )

    @property
    def balance(self):
        """Positive = amount owed by the unit; negative = credit."""
        return self.total_charged - self.total_paid

    def ledger(self):
        """Chronological ledger rows with a running balance."""
        entries = [(c.due_date, 0, c.id, c.description, c.amount_cents, 0, c.origin) for c in self.charges]
        entries += [(p.paid_on, 1, p.id, p.label, 0, p.amount_cents, p.origin) for p in self.payments]
        entries.sort(key=lambda e: (e[0], e[1], e[2]))
        running, rows = 0, []
        for when, kind, entry_id, desc, charge, payment, origin in entries:
            running += charge - payment
            rows.append({"date": when, "type": "payment" if kind else "charge", "id": entry_id, "origin": origin,
                         "description": desc, "charge": charge, "payment": payment, "balance": running})
        return rows


CHARGE_KINDS = {"dues": "Monthly dues", "special": "Special assessment", "fine": "Fine", "other": "Other",
                "opening": "Opening balance"}
# Kinds created by the system (dues generation, spreadsheet import), never by the manual charge form.
SYSTEM_CHARGE_KINDS = ("dues", "opening")


class Charge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, default="dues")
    period = db.Column(db.String(7))  # 'YYYY-MM' for monthly dues
    description = db.Column(db.String(255), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    origin = db.Column(db.String(20))  # 'sheet' = synced from the Google Sheet; None = entered in the app
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit", back_populates="charges")

    __table_args__ = (db.UniqueConstraint("unit_id", "kind", "period", name="uq_charge_unit_kind_period"),)


PAYMENT_METHODS = ("check", "bank transfer", "card", "cash", "purchase credit", "other")


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False, index=True)
    amount_cents = db.Column(db.Integer, nullable=False)
    paid_on = db.Column(db.Date, nullable=False)
    method = db.Column(db.String(30), nullable=False, default="check")
    reference = db.Column(db.String(80))
    notes = db.Column(db.String(255))
    origin = db.Column(db.String(20))  # 'sheet' = synced from the Google Sheet; None = entered in the app
    recorded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit", back_populates="payments")
    recorded_by = db.relationship("User")

    @property
    def label(self):
        text = self.notes or f"Payment ({self.method})"
        return text + (f" #{self.reference}" if self.reference else "")


REQUEST_STATUSES = {"open": "Open", "in_progress": "In progress", "resolved": "Resolved", "closed": "Closed"}
REQUEST_PRIORITIES = {"low": "Low", "normal": "Normal", "high": "High", "urgent": "Urgent"}
REQUEST_CATEGORIES = ("Plumbing", "Electrical", "HVAC", "Appliance", "Common area", "Security", "Other")


class MaintenanceRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), index=True)  # null = common area
    submitted_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(40), nullable=False, default="Other")
    priority = db.Column(db.String(20), nullable=False, default="normal")
    status = db.Column(db.String(20), nullable=False, default="open")
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    unit = db.relationship("Unit")
    submitted_by = db.relationship("User")
    comments = db.relationship("MaintenanceComment", back_populates="request",
                               cascade="all, delete-orphan", order_by="MaintenanceComment.created_at")

    @property
    def is_active(self):
        return self.status in ("open", "in_progress")


class MaintenanceComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("maintenance_request.id"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    request = db.relationship("MaintenanceRequest", back_populates="comments")
    author = db.relationship("User")


class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    pinned = db.Column(db.Boolean, nullable=False, default=False)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    author = db.relationship("User")


class Amenity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text)
    open_hour = db.Column(db.Integer, nullable=False, default=8)    # 0-23
    close_hour = db.Column(db.Integer, nullable=False, default=22)  # 1-24
    max_hours = db.Column(db.Integer, nullable=False, default=4)
    active = db.Column(db.Boolean, nullable=False, default=True)

    bookings = db.relationship("Booking", back_populates="amenity", cascade="all, delete-orphan")


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    amenity_id = db.Column(db.Integer, db.ForeignKey("amenity.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    start = db.Column(db.DateTime, nullable=False)
    end = db.Column(db.DateTime, nullable=False)
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow)

    amenity = db.relationship("Amenity", back_populates="bookings")
    user = db.relationship("User")

    @staticmethod
    def conflicts(amenity_id, start, end):
        return db.session.scalars(
            db.select(Booking).where(Booking.amenity_id == amenity_id,
                                     Booking.start < end, Booking.end > start)
        ).all()


class ImportBatch(db.Model):
    """An uploaded spreadsheet, parsed and waiting for the manager to load each dataset."""
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255))
    payload = db.Column(db.JSON, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    created_by = db.relationship("User")


class Expense(db.Model):
    """A building-level transaction not tied to a unit (e.g. a contractor invoice).
    Negative amounts are money paid out of the condo account."""
    id = db.Column(db.Integer, primary_key=True)
    spent_on = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    approved_by = db.Column(db.String(255))
    source = db.Column(db.String(255))
    notes = db.Column(db.Text)
    origin = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=utcnow)


class Setting(db.Model):
    """Small key/value store for app-wide settings (e.g. the linked Google Sheet)."""
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.JSON)

    @staticmethod
    def get(key, default=None):
        row = db.session.get(Setting, key)
        return row.value if row is not None else default

    @staticmethod
    def put(key, value):
        row = db.session.get(Setting, key)
        if row is None:
            db.session.add(Setting(key=key, value=value))
        else:
            row.value = value
