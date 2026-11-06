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

def add_policy(client, unit_obj, group, until, start=None, holder=None):
    start = start or (until - timedelta(days=365))
    data = {"provider": "Desjardins", "policy_number": "H-1", "valid_from": start.isoformat(),
            "valid_until": until.isoformat(), "file": (io.BytesIO(PDF), "cert.pdf")}
    if holder is not None:
        data["policyholder_id"] = str(holder.id)
    return client.post(f"/units/{unit_obj.id}/insurance/{group}/new", content_type="multipart/form-data", data=data)


def test_one_policy_covers_all_owners(admin_client):
    u = unit("101")
    mostafa = Owner(unit=u, name="Mostafa B", role="owner", is_primary=True)
    delara = Owner(unit=u, name="Delara A", role="co-owner")
    tom = Owner(unit=u, name="Tom Renter", role="tenant")
    db.session.add_all([mostafa, delara, tom])
    db.session.commit()
    page = admin_client.get(f"/units/{u.id}").data.decode()
    assert "Owners’ insurance" in page and "Tenant insurance" in page and "None on file yet" in page
    form = admin_client.get(f"/units/{u.id}/insurance/owners/new").data.decode()
    assert "Mostafa B, Delara A" in form and 'name="policyholder_id"' in form

    assert add_policy(admin_client, u, "owners", date.today() + timedelta(days=200), holder=delara).status_code == 302
    for person in (mostafa, delara):  # one policy, both owners covered
        assert db.session.get(Owner, person.id).insurance_state == "valid"
    assert db.session.get(Owner, tom.id).insurance_state == "missing"  # tenants need their own
    u = db.session.get(Unit, u.id)
    assert u.group_insurance("owners") == "valid" and u.group_insurance("tenants") == "missing"
    page = admin_client.get(f"/units/{u.id}").data.decode()
    assert page.count("View certificate") == 1 and "policyholder Delara A" in page


def test_household_renewal_and_expiry(admin_client):
    u = unit("101")
    o = Owner(unit=u, name="Alice", role="owner")
    db.session.add(o)
    db.session.commit()
    add_policy(admin_client, u, "owners", date.today() + timedelta(days=10))
    assert db.session.get(Unit, u.id).group_insurance("owners") == "expiring"
    add_policy(admin_client, u, "owners", date.today() + timedelta(days=375), start=date.today() + timedelta(days=10))
    u = db.session.get(Unit, u.id)
    assert len(u.policies_for("owners")) == 2 and u.group_insurance("owners") == "valid"


def test_insurance_needs_members_dates_and_file(admin_client):
    u = unit("101")
    resp = admin_client.get(f"/units/{u.id}/insurance/tenants/new")
    assert resp.status_code == 302  # no tenants yet
    db.session.add(Owner(unit=u, name="Alice", role="owner"))
    db.session.commit()
    resp = admin_client.post(f"/units/{u.id}/insurance/owners/new", content_type="multipart/form-data",
                             data={"valid_from": "2026-12-01", "valid_until": "2026-01-01"})
    assert resp.status_code == 400
    assert db.session.scalar(db.select(db.func.count(InsurancePolicy.id))) == 0


def test_removing_policyholder_keeps_household_cover(admin_client):
    u = unit("101")
    a, b = Owner(unit=u, name="Ann", role="owner"), Owner(unit=u, name="Ben", role="co-owner")
    db.session.add_all([a, b])
    db.session.commit()
    add_policy(admin_client, u, "owners", date.today() + timedelta(days=100), holder=a)
    admin_client.post(f"/owners/{a.id}/delete")
    policy = db.session.scalar(db.select(InsurancePolicy))
    assert policy is not None and policy.owner.name == "Ben"
    assert db.session.get(Owner, b.id).insurance_state == "valid"


def test_old_person_link_redirects(admin_client):
    o = Owner(unit=unit("101"), name="Tom", role="tenant")
    db.session.add(o)
    db.session.commit()
    resp = admin_client.get(f"/people/{o.id}/insurance/new")
    assert resp.headers["Location"].endswith(f"/units/{o.unit_id}/insurance/tenants/new?holder={o.id}")


def test_who_can_upload_and_see_household_insurance(app, client):
    u101 = unit("101")
    u102 = user("bob@test.com").unit  # Bob is a tenant of 102
    db.session.add_all([Owner(unit=u101, name="Alice Chen", role="owner"), Owner(unit=u101, name="Tom", role="tenant"),
                        Owner(unit=u102, name="Bob", role="tenant"), Owner(unit=u102, name="Landlord", role="owner")])
    db.session.commit()

    login(client, "alice@test.com")  # owner of 101: both of 101's households, nothing in 102
    assert add_policy(client, u101, "tenants", date.today() + timedelta(days=100)).status_code == 302
    assert add_policy(client, u101, "owners", date.today() + timedelta(days=100)).status_code == 302
    assert client.get(f"/units/{u102.id}/insurance/tenants/new").status_code == 403
    pol = db.session.get(Unit, u101.id).current_policy("tenants")
    assert client.get(f"/insurance/{pol.id}/file").data == PDF
    dash = client.get("/").data.decode()
    assert "Owners’ insurance" in dash and "Tenant insurance" in dash
    client.post("/logout")

    login(client, "bob@test.com")  # tenant of 102: only 102's tenant policy
    assert client.get(f"/units/{u102.id}/insurance/tenants/new").status_code == 200
    assert client.get(f"/units/{u102.id}/insurance/owners/new").status_code == 403
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
