import io
from datetime import date, timedelta

from condo.extensions import db
from condo.models import Document, InsurancePolicy, MaintenanceRequest, Owner, Unit, User, insurance_state

from .conftest import login

PDF = b"%PDF-1.4 test certificate"


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def user(email):
    return db.session.scalar(db.select(User).where(User.email == email))


def upload_doc(client, **fields):
    data = {"title": "Doc", "category": "syndicate", "visibility": "everyone",
            "file": (io.BytesIO(PDF), "doc.pdf"), **fields}
    return client.post("/documents/new", data=data, content_type="multipart/form-data")


# --- insurance status ------------------------------------------------------------

def test_insurance_state():
    today = date(2026, 11, 5)
    assert insurance_state(None, today) == "missing"
    assert insurance_state(date(2026, 11, 4), today) == "expired"
    assert insurance_state(date(2026, 11, 30), today) == "expiring"
    assert insurance_state(date(2027, 6, 1), today) == "valid"


# --- documents --------------------------------------------------------------------

def test_upload_and_view_documents(admin_client):
    assert upload_doc(admin_client, title="Declaration of co-ownership").status_code == 302
    doc = db.session.scalar(db.select(Document))
    resp = admin_client.get(f"/documents/{doc.id}/file")
    assert resp.data == PDF and resp.mimetype == "application/pdf" and "inline" in resp.headers["Content-Disposition"]
    assert b"Declaration of co-ownership" in admin_client.get("/documents/").data


def test_building_insurance_needs_dates_and_shows_everywhere(admin_client, client):
    resp = upload_doc(admin_client, title="Building policy", category="insurance")
    assert resp.status_code == 400  # dates missing
    start, end = date.today() - timedelta(days=10), date.today() + timedelta(days=355)
    upload_doc(admin_client, title="Building policy", category="insurance", provider="Promutuel",
               policy_number="B-77", valid_from=start.isoformat(), valid_until=end.isoformat())
    assert b"Promutuel" in admin_client.get("/").data
    admin_client.post("/logout")
    login(client, "bob@test.com")  # a tenant
    page = client.get("/").data
    assert b"Building insurance" in page and b"Promutuel" in page
    assert b"Promutuel" in client.get("/documents/").data


def test_document_visibility(admin_client, client):
    upload_doc(admin_client, title="AGM minutes 2026", category="minutes", visibility="owners")
    upload_doc(admin_client, title="Board notes", category="other", visibility="managers")
    minutes, notes = db.session.scalars(db.select(Document).order_by(Document.id)).all()
    admin_client.get("/documents/")  # show the pending "uploaded" messages before switching user
    admin_client.post("/logout")
    login(client, "bob@test.com")  # tenant
    page = client.get("/documents/").data
    assert b"AGM minutes" not in page and b"Board notes" not in page
    assert client.get(f"/documents/{minutes.id}/file").status_code == 403
    client.post("/logout")
    login(client, "alice@test.com")  # owner
    assert b"AGM minutes" in client.get("/documents/").data
    assert client.get(f"/documents/{minutes.id}/file").status_code == 200
    assert client.get(f"/documents/{notes.id}/file").status_code == 403


def test_documents_admin_only_upload(alice_client):
    assert alice_client.get("/documents/new").status_code == 403


def test_bad_uploads_rejected(admin_client):
    resp = upload_doc(admin_client, file=(io.BytesIO(b"MZ..."), "virus.exe"))
    assert resp.status_code == 400 and b"PDF" in resp.data
    big = io.BytesIO(b"0" * (4 * 1024 * 1024 + 10))
    assert upload_doc(admin_client, file=(big, "big.pdf")).status_code in (400, 413)
    assert db.session.scalar(db.select(db.func.count(Document.id))) == 0


# --- personal insurance -------------------------------------------------------------

def add_policy(client, owner, until, start=None):
    start = start or (until - timedelta(days=365))
    return client.post(f"/people/{owner.id}/insurance/new", content_type="multipart/form-data", data={
        "provider": "Desjardins", "policy_number": "H-1", "valid_from": start.isoformat(),
        "valid_until": until.isoformat(), "file": (io.BytesIO(PDF), "cert.pdf")})


def test_unit_page_shows_tenants_and_insurance(admin_client):
    u = unit("101")
    alice = Owner(unit=u, name="Alice Chen", role="owner", email="alice@test.com")
    tom = Owner(unit=u, name="Tom Renter", role="tenant")
    db.session.add_all([alice, tom])
    db.session.commit()
    page = admin_client.get(f"/units/{u.id}").data.decode()
    assert "Tenants" in page and "Tom Renter" in page and "No insurance" in page

    assert add_policy(admin_client, tom, date.today() + timedelta(days=200)).status_code == 302
    assert add_policy(admin_client, alice, date.today() + timedelta(days=10)).status_code == 302
    u = db.session.get(Unit, u.id)
    assert u.group_insurance("tenants") == "valid" and u.group_insurance("owners") == "expiring"
    page = admin_client.get(f"/units/{u.id}").data.decode()
    assert "Expiring soon" in page and "certificate" in page

    # Renewal keeps history; the latest end date counts.
    add_policy(admin_client, alice, date.today() + timedelta(days=375), start=date.today() + timedelta(days=10))
    alice = db.session.get(Owner, alice.id)
    assert len(alice.policies) == 2 and alice.insurance_state == "valid"


def test_insurance_dates_and_file_required(admin_client):
    o = Owner(unit=unit("101"), name="Alice", role="owner")
    db.session.add(o)
    db.session.commit()
    resp = admin_client.post(f"/people/{o.id}/insurance/new", content_type="multipart/form-data",
                             data={"valid_from": "2026-12-01", "valid_until": "2026-01-01"})
    assert resp.status_code == 400
    assert db.session.scalar(db.select(db.func.count(InsurancePolicy.id))) == 0


def test_who_can_upload_and_see_personal_insurance(app, client):
    u101 = unit("101")
    bob_unit = user("bob@test.com").unit  # unit 102, Bob is a tenant there
    alice_c = Owner(unit=u101, name="Alice Chen", role="owner", email="alice@test.com")
    tenant_c = Owner(unit=u101, name="Tom Renter", role="tenant", email="tom@x.com")
    bob_c = Owner(unit=bob_unit, name="Bob", role="tenant", email="bob@test.com")
    other_c = Owner(unit=bob_unit, name="Landlord", role="owner", email="landlord@x.com")
    db.session.add_all([alice_c, tenant_c, bob_c, other_c])
    db.session.commit()

    login(client, "alice@test.com")  # owner of 101: can manage anyone in 101, nobody in 102
    assert add_policy(client, tenant_c, date.today() + timedelta(days=100)).status_code == 302
    assert client.get(f"/people/{bob_c.id}/insurance/new").status_code == 403
    pol = db.session.get(Owner, tenant_c.id).current_policy
    assert client.get(f"/insurance/{pol.id}/file").data == PDF
    assert b"Home insurance" in client.get("/").data
    client.post("/logout")

    login(client, "bob@test.com")  # tenant of 102: only their own record
    assert client.get(f"/people/{bob_c.id}/insurance/new").status_code == 200
    assert client.get(f"/people/{other_c.id}/insurance/new").status_code == 403
    assert client.get(f"/insurance/{pol.id}/file").status_code == 403  # another unit's certificate


def test_directory_shows_insurance(admin_client):
    o = Owner(unit=unit("101"), name="Alice", role="owner")
    db.session.add(o)
    db.session.commit()
    assert b"No insurance" in admin_client.get("/directory/owners").data
    assert b"Insured until" in admin_client.get("/directory/owners/export.csv").data


# --- owner approval of tenant repairs ---------------------------------------------------

def submit(client, title="Leaky tap", location="unit"):
    return client.post("/maintenance/new", data={"title": title, "description": "Drips", "category": "Plumbing",
                                                 "priority": "normal", "location": location})


def make_owner_account(unit_obj, email="owner102@test.com"):
    owner = User(email=email, name="Olivia Owner", role="owner", unit=unit_obj)
    owner.set_password("password123")
    db.session.add(owner)
    db.session.commit()
    return owner


def test_tenant_request_waits_for_owner_then_owner_approves(app, client):
    make_owner_account(user("bob@test.com").unit)
    login(client, "bob@test.com")  # tenant
    submit(client)
    req = db.session.scalar(db.select(MaintenanceRequest))
    assert (req.status, req.owner_approval) == ("awaiting_owner", "pending")
    # The tenant can't approve their own request.
    assert client.post(f"/maintenance/{req.id}/owner-decision", data={"decision": "approve"}).status_code == 403
    client.post("/logout")

    login(client, "owner102@test.com")
    assert b"Repairs waiting for your approval" in client.get("/").data
    client.post(f"/maintenance/{req.id}/owner-decision", data={"decision": "approve", "note": "Go ahead"})
    req = db.session.get(MaintenanceRequest, req.id)
    assert (req.status, req.owner_approval, req.owner_decision_by) == ("open", "approved", "Olivia Owner")
    assert "Owner approved" in req.comments[-1].body


def test_owner_decline_needs_reason(app, client):
    make_owner_account(user("bob@test.com").unit)
    login(client, "bob@test.com")
    submit(client)
    client.post("/logout")
    login(client, "owner102@test.com")
    req = db.session.scalar(db.select(MaintenanceRequest))
    client.post(f"/maintenance/{req.id}/owner-decision", data={"decision": "decline"})
    assert db.session.get(MaintenanceRequest, req.id).awaiting_owner
    client.post(f"/maintenance/{req.id}/owner-decision", data={"decision": "decline", "note": "I'll fix it myself"})
    req = db.session.get(MaintenanceRequest, req.id)
    assert (req.status, req.owner_approval) == ("closed", "declined")


def test_manager_records_approval_and_cannot_skip_it(app, client):
    login(client, "bob@test.com")
    submit(client)
    client.post("/logout")
    login(client, "admin@test.com")
    req = db.session.scalar(db.select(MaintenanceRequest))
    assert b"No owner of this unit has a website account" in client.get(f"/maintenance/{req.id}").data
    client.post(f"/maintenance/{req.id}/status", data={"status": "in_progress", "priority": "normal"})
    assert db.session.get(MaintenanceRequest, req.id).awaiting_owner  # blocked
    assert b"Waiting for owner approval" in client.get("/").data
    client.post(f"/maintenance/{req.id}/owner-decision",
                data={"decision": "approve", "on_behalf": "Olivia, by phone"})
    req = db.session.get(MaintenanceRequest, req.id)
    assert req.status == "open" and "on behalf of Olivia, by phone" in req.owner_decision_by


def test_owner_and_common_area_requests_skip_approval(app, client):
    login(client, "alice@test.com")  # owner
    submit(client, "Owner's own repair")
    client.post("/logout")
    login(client, "bob@test.com")  # tenant, common area
    submit(client, "Lobby light", location="common")
    statuses = {r.title: r.status for r in db.session.scalars(db.select(MaintenanceRequest))}
    assert statuses == {"Owner's own repair": "open", "Lobby light": "open"}


def test_other_units_owner_cannot_decide(app, client):
    login(client, "bob@test.com")
    submit(client)
    client.post("/logout")
    login(client, "alice@test.com")  # owner of a different unit
    req = db.session.scalar(db.select(MaintenanceRequest))
    assert client.post(f"/maintenance/{req.id}/owner-decision", data={"decision": "approve"}).status_code == 403
