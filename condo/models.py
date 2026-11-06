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
    owners = db.relationship("Owner", back_populates="unit", cascade="all, delete-orphan",
                             order_by="(Owner.is_primary.desc(), Owner.name)")

    @property
    def owner_contacts(self):
        return [o for o in self.owners if o.role != "tenant"]

    @property
    def tenants(self):
        return [o for o in self.owners if o.role == "tenant"]

    def members(self, group):
        """People who need cover in a group: 'owners' (owners and co-owners) or 'tenants'."""
        people = self.tenants if group == "tenants" else self.owner_contacts
        return [o for o in people if o.needs_insurance]

    def policies_for(self, group):
        return db.session.scalars(
            db.select(InsurancePolicy).where(InsurancePolicy.unit_id == self.id, InsurancePolicy.holder_group == group)
            .order_by(InsurancePolicy.valid_until.desc(), InsurancePolicy.id.desc())).all()

    def current_policy(self, group):
        """One policy covers the household: all the unit's owners, or all its tenants."""
        policies = self.policies_for(group)
        return policies[0] if policies else None

    def extra_policies(self, group, today=None):
        """Other policies still in force beside the main one, e.g. a co-owner who also keeps
        their own. Optional: the household is covered by any one valid policy. Renewals of the
        same policy (same policyholder and policy number) aren't listed twice."""
        from datetime import date
        today = today or date.today()
        policies = self.policies_for(group)
        if not policies:
            return []
        main, seen, extra = policies[0], set(), []
        seen.add((main.owner_id, (main.policy_number or "").strip().lower()))
        for p in policies[1:]:
            key = (p.owner_id, (p.policy_number or "").strip().lower())
            if p.valid_until >= today and key not in seen:
                seen.add(key)
                extra.append(p)
        return extra

    def group_insurance(self, group):
        """Insurance state of the owners' or tenants' shared policy; None when nobody is in the group."""
        if not self.members(group):
            return None
        p = self.current_policy(group)
        return insurance_state(p.valid_until if p else None)

    @property
    def owner_display(self):
        """Names of the unit's owner contacts, or the plain owner name when none are recorded."""
        names = [o.name for o in self.owners if o.role in ("owner", "co-owner")]
        return " / ".join(names) if names else (self.owner_name or "")

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
PAYMENT_METHOD_LABELS = {"bank transfer": "Interac e-Transfer / bank transfer", "check": "Cheque", "card": "Card",
                         "cash": "Cash", "purchase credit": "Credit for a purchase (owner paid a cost)",
                         "other": "Other"}


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


REQUEST_STATUSES = {"awaiting_owner": "Awaiting owner approval", "open": "Open",
                    "quote_requested": "Owner to get a quote", "quote_review": "Quote awaiting board approval",
                    "owner_repair": "Approved: owner to do the repair", "in_progress": "In progress",
                    "resolved": "Resolved", "closed": "Closed"}
ACTIVE_STATUSES = ("awaiting_owner", "open", "quote_requested", "quote_review", "owner_repair", "in_progress")
QUOTE_STATUSES = ("quote_requested", "quote_review", "owner_repair")
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
    # Tenant requests for their unit need the owner's go-ahead before work starts.
    owner_approval = db.Column(db.String(20))  # None (not needed), 'pending', 'approved', 'declined'
    owner_decision_by = db.Column(db.String(200))
    owner_decision_at = db.Column(db.DateTime)
    owner_decision_note = db.Column(db.Text)
    # Quote step: the board asks the owner to get a quote, then approves the cost before work.
    quote_note = db.Column(db.Text)               # board's instructions to the owner
    quote_vendor = db.Column(db.String(200))
    quote_amount_cents = db.Column(db.Integer)
    quote_file_id = db.Column(db.Integer, db.ForeignKey("stored_file.id"))
    quote_approved_cents = db.Column(db.Integer)
    quote_decided_by = db.Column(db.String(200))
    quote_decided_at = db.Column(db.DateTime)
    quote_decision_note = db.Column(db.Text)

    unit = db.relationship("Unit")
    submitted_by = db.relationship("User")
    quote_file = db.relationship("StoredFile", foreign_keys=[quote_file_id])
    comments = db.relationship("MaintenanceComment", back_populates="request",
                               cascade="all, delete-orphan", order_by="MaintenanceComment.created_at")

    @property
    def is_active(self):
        return self.status in ACTIVE_STATUSES

    @property
    def awaiting_owner(self):
        return self.status == "awaiting_owner"


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


COST_CATEGORIES = {
    "utilities": "Utilities",
    "insurance": "Insurance",
    "maintenance": "Maintenance",
    "repairs": "Repairs & projects",
    "administration": "Administration",
    "other": "Other",
}
_COST_KEYWORDS = [
    ("insurance", ("insurance", "assurance")),
    ("utilities", ("hydro", "electric bill", "électricité", "gas", "water bill")),
    ("maintenance", ("snow", "lawn", "landscap", "garden", "gardening", "cleaning", "déneigement", "leaf", "compost")),
    ("administration", ("bank", "fee", "frais", "interac", "cheque", "check order", "accounting", "tax")),
    ("repairs", ("repair", "heater", "plumb", "roof", "window", "door", "brick", "garage", "contractor",
                 "electric", "paint", "invoice")),
]


def guess_cost_category(text):
    t = (text or "").lower()
    for key, words in _COST_KEYWORDS:
        if any(w in t for w in words):
            return key
    return "other"


class Expense(db.Model):
    """A building-level cost (or refund) not tied to a unit, e.g. a contractor invoice.
    Negative amounts are money paid out of the condo account; positive ones are money back."""
    id = db.Column(db.Integer, primary_key=True)
    spent_on = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(20))
    description = db.Column(db.String(255), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    approved_by = db.Column(db.String(255))
    source = db.Column(db.String(255))
    notes = db.Column(db.Text)
    origin = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def sort_id(self):
        return self.id

    paid_by = None

    @property
    def cost_cents(self):
        """Money spent as a positive number (a refund shows as negative)."""
        return -self.amount_cents

    @property
    def category_key(self):
        return self.category if self.category in COST_CATEGORIES else guess_cost_category(
            f"{self.description} {self.notes or ''}")

    @property
    def category_label(self):
        return COST_CATEGORIES[self.category_key]


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


OWNER_ROLES = {"owner": "Owner", "co-owner": "Co-owner", "tenant": "Tenant", "contact": "Other contact"}

# Board of directors of the syndicate. Order = display order.
BOARD_POSITIONS = {
    "president": "President",
    "vice-president": "Vice-president",
    "treasurer": "Treasurer",
    "secretary": "Secretary",
    "accounting": "Accounting",
    "director": "Director",
}
SINGLE_HOLDER_POSITIONS = ("president", "vice-president", "treasurer", "secretary")
OPEN_TO_CONTACTS = ("accounting",)  # positions an outside contact (not a co-owner) may hold


def allowed_positions(role):
    if role in ("owner", "co-owner"):
        return tuple(BOARD_POSITIONS)
    if role == "contact":
        return OPEN_TO_CONTACTS
    return ()


class Owner(db.Model):
    """A person connected to a unit (usually one of several co-owners), with contact details.
    Separate from login accounts: an owner doesn't need to use the website."""
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="owner")
    email = db.Column(db.String(255))
    phone = db.Column(db.String(60))
    mailing_address = db.Column(db.Text)  # empty = lives in the unit
    is_primary = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.Text)
    board_roles = db.Column(db.String(200))  # comma-separated BOARD_POSITIONS keys
    origin = db.Column(db.String(20))  # 'sheet' = synced from the Google Sheet's Owners tab
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit", back_populates="owners")
    # Policies where this person is the named policyholder (each covers their whole group).
    policies = db.relationship("InsurancePolicy", back_populates="owner", cascade="all, delete-orphan",
                               passive_deletes=True,
                               order_by="(InsurancePolicy.valid_until.desc(), InsurancePolicy.id.desc())")

    @property
    def role_label(self):
        return OWNER_ROLES.get(self.role, self.role)

    @property
    def positions(self):
        keys = [k.strip() for k in (self.board_roles or "").split(",") if k.strip() in BOARD_POSITIONS]
        return sorted(set(keys), key=list(BOARD_POSITIONS).index)

    @positions.setter
    def positions(self, keys):
        keys = [k for k in BOARD_POSITIONS if k in set(keys or ())]
        self.board_roles = ",".join(keys) or None

    @property
    def position_labels(self):
        return [BOARD_POSITIONS[k] for k in self.positions]

    @property
    def insurance_group(self):
        return "tenants" if self.role == "tenant" else "owners"

    @property
    def current_policy(self):
        """The household policy that covers this person (shared with co-owners or co-tenants)."""
        return self.unit.current_policy(self.insurance_group) if self.needs_insurance and self.unit else None

    @property
    def insurance_state(self):
        p = self.current_policy
        return insurance_state(p.valid_until if p else None)

    @property
    def needs_insurance(self):
        return self.role in ("owner", "co-owner", "tenant")


# --- files, documents and insurance ---------------------------------------------

class StoredFile(db.Model):
    """An uploaded file kept in the database (serverless hosting has no lasting disk).
    Served only through views that check who may see it."""
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(100), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    uploaded_by = db.relationship("User")


DOCUMENT_CATEGORIES = {
    "syndicate": "Syndicate documents",
    "insurance": "Building insurance",
    "minutes": "Annual meeting minutes",
    "other": "Other",
}
DOCUMENT_VISIBILITY = {"everyone": "Everyone signed in", "owners": "Owners and managers", "managers": "Managers only"}


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(20), nullable=False, default="other")
    description = db.Column(db.Text)
    document_date = db.Column(db.Date)   # meeting date, adoption date, issue date
    valid_from = db.Column(db.Date)      # insurance policy period
    valid_until = db.Column(db.Date)
    provider = db.Column(db.String(200))  # insurer
    policy_number = db.Column(db.String(100))
    visibility = db.Column(db.String(20), nullable=False, default="everyone")
    file_id = db.Column(db.Integer, db.ForeignKey("stored_file.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    file = db.relationship("StoredFile", cascade="all, delete-orphan", single_parent=True)

    def visible_to(self, user):
        if user.is_admin or self.visibility == "everyone":
            return True
        return self.visibility == "owners" and user.role == "owner"


EXPIRING_SOON_DAYS = 30


def insurance_state(valid_until, today=None):
    """'valid', 'expiring', 'expired' or 'missing' for a policy end date."""
    from datetime import date, timedelta
    today = today or date.today()
    if valid_until is None:
        return "missing"
    if valid_until < today:
        return "expired"
    if valid_until <= today + timedelta(days=EXPIRING_SOON_DAYS):
        return "expiring"
    return "valid"


INSURANCE_STATE_LABELS = {"valid": "Insured", "expiring": "Expiring soon", "expired": "Expired", "missing": "No insurance"}


class InsurancePolicy(db.Model):
    """A home-insurance policy covering a unit's owners together, or its tenants together.
    `owner` is the named policyholder. Renewals add a new row; the latest end date counts."""
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("owner.id", ondelete="CASCADE"), nullable=False, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), index=True)
    holder_group = db.Column(db.String(10))  # 'owners' or 'tenants'
    provider = db.Column(db.String(200))
    policy_number = db.Column(db.String(100))
    valid_from = db.Column(db.Date, nullable=False)
    valid_until = db.Column(db.Date, nullable=False)
    file_id = db.Column(db.Integer, db.ForeignKey("stored_file.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    owner = db.relationship("Owner", back_populates="policies")
    unit = db.relationship("Unit")
    file = db.relationship("StoredFile", cascade="all, delete-orphan", single_parent=True)

    @property
    def state(self):
        return insurance_state(self.valid_until)


def hand_over_policies(person):
    """Before removing a person, pass the household policies they hold to someone else in
    the same group, so removing one co-owner doesn't drop the household's insurance."""
    others = [o for o in person.unit.members(person.insurance_group) if o.id != person.id] if person.unit else []
    if not others:
        return False
    heir = next((o for o in others if o.is_primary), others[0])
    for policy in list(person.policies):
        policy.owner = heir
    return True


class Notification(db.Model):
    """Something a user should know about, shown under the bell until they read it."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text)
    url = db.Column(db.String(500))
    kind = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    read_at = db.Column(db.DateTime)

    user = db.relationship("User")


REIMBURSEMENT_STATUSES = {"submitted": "Waiting for the president", "approved": "Approved: waiting for accounting",
                          "declined": "Declined", "settled": "Settled"}
SETTLE_METHODS = {"credit": "Credit on my next condo payment", "transfer": "Pay me back (e-transfer or cheque)"}


class Reimbursement(db.Model):
    """An owner paid a building cost and asks for it back, or for a credit on their dues."""
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("unit.id"), nullable=False, index=True)
    requested_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    request_id = db.Column(db.Integer, db.ForeignKey("maintenance_request.id"))  # repair it came from, if any
    description = db.Column(db.String(255), nullable=False)
    vendor = db.Column(db.String(200))
    amount_cents = db.Column(db.Integer, nullable=False)
    spent_on = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(20))
    settle_method = db.Column(db.String(20), nullable=False, default="credit")
    status = db.Column(db.String(20), nullable=False, default="submitted")
    decided_by = db.Column(db.String(200))
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.Text)
    settled_by = db.Column(db.String(200))
    settled_at = db.Column(db.DateTime)
    settle_reference = db.Column(db.String(120))
    payment_id = db.Column(db.Integer, db.ForeignKey("payment.id"))
    expense_id = db.Column(db.Integer, db.ForeignKey("expense.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    unit = db.relationship("Unit")
    requested_by = db.relationship("User")
    request = db.relationship("MaintenanceRequest")
    files = db.relationship("ReimbursementFile", back_populates="reimbursement", cascade="all, delete-orphan")

    @property
    def status_label(self):
        return REIMBURSEMENT_STATUSES.get(self.status, self.status)


class ReimbursementFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reimbursement_id = db.Column(db.Integer, db.ForeignKey("reimbursement.id", ondelete="CASCADE"), nullable=False)
    file_id = db.Column(db.Integer, db.ForeignKey("stored_file.id"), nullable=False)

    reimbursement = db.relationship("Reimbursement", back_populates="files")
    file = db.relationship("StoredFile", cascade="all, delete-orphan", single_parent=True)
