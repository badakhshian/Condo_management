import io
from datetime import date

from condo.extensions import db
from condo.models import (Expense, MaintenanceRequest, Notification, Owner, Payment, Reimbursement, Unit, User)

from .conftest import login

PDF = b"%PDF-1.4 receipt"


def user(email):
    return db.session.scalar(db.select(User).where(User.email == email))


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def account(email, name, role, unit_obj=None):
    u = User(email=email, name=name, role=role, unit=unit_obj)
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


def give_position(email, unit_number, key):
    o = Owner(unit=unit(unit_number), name=email.split("@")[0].title(), role="owner", email=email)
    o.positions = [key]
    db.session.add(o)
    db.session.commit()


def notes_for(email):
    return [n.title for n in db.session.scalars(db.select(Notification).where(
        Notification.user_id == user(email).id).order_by(Notification.id))]


def switch(client, email):
    client.post("/logout")
    login(client, email)


# --- notifications -------------------------------------------------------------------

def test_bell_and_notification_page(app, client):
    alice = user("alice@test.com")
    db.session.add_all([Notification(user_id=alice.id, title="Hello Alice", url="/maintenance/"),
                        Notification(user_id=user("bob@test.com").id, title="For Bob only")])
    db.session.commit()
    login(client, "alice@test.com")
    page = client.get("/").data.decode()
    assert 'class="bell-count">1<' in page
    listing = client.get("/notifications/").data.decode()
    assert "Hello Alice" in listing and "For Bob only" not in listing
    note = db.session.scalar(db.select(Notification).where(Notification.title == "Hello Alice"))
    resp = client.get(f"/notifications/{note.id}")
    assert resp.headers["Location"] == "/maintenance/" and db.session.get(Notification, note.id).read_at
    bob_note = db.session.scalar(db.select(Notification).where(Notification.title == "For Bob only"))
    assert client.get(f"/notifications/{bob_note.id}").status_code == 404
    assert 'class="bell-count"' not in client.get("/").data.decode()


def test_mark_all_read_and_external_urls_blocked(app, client):
    alice = user("alice@test.com")
    db.session.add_all([Notification(user_id=alice.id, title="A", url="https://evil.example/"),
                        Notification(user_id=alice.id, title="B")])
    db.session.commit()
    login(client, "alice@test.com")
    evil = db.session.scalar(db.select(Notification).where(Notification.title == "A"))
    assert client.get(f"/notifications/{evil.id}").headers["Location"] == "/notifications/"
    client.post("/notifications/read-all")
    assert all(n.read_at for n in db.session.scalars(db.select(Notification).where(Notification.user_id == alice.id)))


def test_events_notify_the_right_people(app, client):
    owner102 = account("olivia@test.com", "Olivia", "owner", user("bob@test.com").unit)
    login(client, "bob@test.com")  # tenant of 102
    client.post("/maintenance/new", data={"title": "Leaky tap", "description": "Drips", "category": "Plumbing",
                                          "priority": "normal", "location": "unit"})
    assert notes_for("olivia@test.com") == ["Your tenant asked for a repair: Leaky tap"]
    req = db.session.scalar(db.select(MaintenanceRequest))
    switch(client, "olivia@test.com")
    client.post(f"/maintenance/{req.id}/owner-decision", data={"decision": "approve"})
    assert "The owner approved your repair request: Leaky tap" in notes_for("bob@test.com")
    assert "Repair approved by the owner, ready to schedule: Leaky tap" in notes_for("admin@test.com")
    switch(client, "admin@test.com")
    client.post(f"/maintenance/{req.id}/status", data={"status": "in_progress", "priority": "normal"})
    client.post(f"/maintenance/{req.id}/comment", data={"body": "Plumber Tuesday"})
    bob = notes_for("bob@test.com")
    assert "Your repair request is now in progress: Leaky tap" in bob
    assert "New comment on “Leaky tap” from Admin" in bob
    client.post("/announcements/new", data={"title": "Water shutoff", "body": "Tuesday"})
    assert "New notice: Water shutoff" in notes_for("alice@test.com")
    assert "New notice: Water shutoff" not in notes_for("admin@test.com")  # not the author


# --- quote step -----------------------------------------------------------------------

def test_board_asks_owner_for_quote_then_approves(app, client):
    req = MaintenanceRequest(unit=unit("101"), submitted_by=user("alice@test.com"), title="Garage drain",
                             description="Clogged", status="open")
    db.session.add(req)
    db.session.commit()
    login(client, "admin@test.com")
    assert b"Ask the owner for a quote" in client.get(f"/maintenance/{req.id}").data
    client.post(f"/maintenance/{req.id}/ask-quote", data={"note": "Two quotes please"})
    assert db.session.get(MaintenanceRequest, req.id).status == "quote_requested"
    assert "Please get a quote for: Garage drain" in notes_for("alice@test.com")

    switch(client, "alice@test.com")  # the unit's owner uploads the quote
    resp = client.post(f"/maintenance/{req.id}/quote", content_type="multipart/form-data", data={
        "vendor": "Plombier ABC", "amount": "850", "file": (io.BytesIO(PDF), "quote.pdf")})
    assert resp.status_code == 302
    req = db.session.get(MaintenanceRequest, req.id)
    assert (req.status, req.quote_amount_cents) == ("quote_review", 85000)
    assert any("Quote to approve" in t for t in notes_for("admin@test.com"))
    assert client.post(f"/maintenance/{req.id}/quote-decision", data={"decision": "approve"}).status_code == 403

    switch(client, "admin@test.com")
    client.post(f"/maintenance/{req.id}/quote-decision", data={"decision": "approve", "approved_amount": "800"})
    req = db.session.get(MaintenanceRequest, req.id)
    assert (req.status, req.quote_approved_cents) == ("owner_repair", 80000)
    assert any("Quote approved ($800.00)" in t for t in notes_for("alice@test.com"))
    switch(client, "alice@test.com")
    page = client.get(f"/maintenance/{req.id}").data.decode()
    assert "Request reimbursement" in page
    form = client.get(f"/reimbursements/new?request_id={req.id}").data.decode()
    assert 'value="800.00"' in form and "Plombier ABC" in form


def test_quote_decline_needs_reason_and_goes_back(app, client):
    req = MaintenanceRequest(unit=unit("101"), submitted_by=user("alice@test.com"), title="Fence",
                             description="Broken", status="quote_review", quote_amount_cents=5000)
    db.session.add(req)
    db.session.commit()
    login(client, "admin@test.com")
    client.post(f"/maintenance/{req.id}/quote-decision", data={"decision": "decline"})
    assert db.session.get(MaintenanceRequest, req.id).status == "quote_review"
    client.post(f"/maintenance/{req.id}/quote-decision", data={"decision": "decline", "note": "Too high"})
    assert db.session.get(MaintenanceRequest, req.id).status == "quote_requested"


def test_tenant_cannot_submit_quote(app, client):
    req = MaintenanceRequest(unit=user("bob@test.com").unit, submitted_by=user("bob@test.com"), title="X",
                             description="Y", status="quote_requested")
    db.session.add(req)
    db.session.commit()
    login(client, "bob@test.com")
    resp = client.post(f"/maintenance/{req.id}/quote", content_type="multipart/form-data",
                       data={"amount": "5", "file": (io.BytesIO(PDF), "q.pdf")})
    assert resp.status_code == 403


# --- reimbursements -------------------------------------------------------------------

def submit_reimbursement(client, method="credit", amount="114.31"):
    return client.post("/reimbursements/new", content_type="multipart/form-data", data={
        "description": "Leaf blower for the yard", "vendor": "Amazon", "amount": amount, "spent_on": "2026-10-09",
        "category": "maintenance", "settle_method": method,
        "files": [(io.BytesIO(PDF), "receipt.pdf"), (io.BytesIO(PDF), "photo.pdf")]})


def test_reimbursement_credit_flow_with_board_roles(app, client):
    account("pres@test.com", "Pat President", "owner", unit("102"))
    account("acct@test.com", "Ann Accounting", "owner", unit("102"))
    give_position("pres@test.com", "102", "president")
    give_position("acct@test.com", "102", "accounting")

    login(client, "alice@test.com")  # owner of 101
    assert submit_reimbursement(client).status_code == 302
    r = db.session.scalar(db.select(Reimbursement))
    assert (r.status, len(r.files), r.unit.number) == ("submitted", 2, "101")
    assert notes_for("pres@test.com") == ["Reimbursement to approve: unit 101, $114.31"]
    assert notes_for("admin@test.com") == []  # the President has an account, so managers aren't bothered
    assert client.post(f"/reimbursements/{r.id}/decide", data={"decision": "approve"}).status_code == 403

    switch(client, "acct@test.com")  # accounting can't approve, only record
    assert client.post(f"/reimbursements/{r.id}/decide", data={"decision": "approve"}).status_code == 403

    switch(client, "pres@test.com")
    assert b"Reimbursements waiting for you" in client.get("/").data
    client.post(f"/reimbursements/{r.id}/decide", data={"decision": "approve", "note": "Thanks"})
    assert db.session.get(Reimbursement, r.id).status == "approved"
    assert "Reimbursement approved: $114.31" in notes_for("alice@test.com")
    assert notes_for("acct@test.com") == ["Approved reimbursement to record: unit 101, $114.31"]
    assert client.post(f"/reimbursements/{r.id}/settle").status_code == 403  # president can't record

    switch(client, "acct@test.com")
    client.post(f"/reimbursements/{r.id}/settle", data={"settled_on": "2026-10-12"})
    r = db.session.get(Reimbursement, r.id)
    assert r.status == "settled" and r.payment_id
    credit = db.session.get(Payment, r.payment_id)
    assert (credit.method, credit.amount_cents, credit.unit.number) == ("purchase credit", 11431, "101")
    assert "Reimbursement settled: $114.31 credited to unit 101's account" in notes_for("alice@test.com")


def test_reimbursement_payback_and_decline(app, client):
    login(client, "alice@test.com")
    submit_reimbursement(client, method="transfer", amount="50")
    submit_reimbursement(client, method="credit", amount="20")
    first, second = db.session.scalars(db.select(Reimbursement).order_by(Reimbursement.id)).all()
    # No President or Accounting accounts yet: managers receive and handle both steps.
    assert len([t for t in notes_for("admin@test.com") if t.startswith("Reimbursement to approve")]) == 2
    switch(client, "admin@test.com")
    client.post(f"/reimbursements/{first.id}/decide", data={"decision": "approve"})
    client.post(f"/reimbursements/{first.id}/settle", data={"reference": "e-transfer 123"})
    expense = db.session.get(Expense, db.session.get(Reimbursement, first.id).expense_id)
    assert expense.cost_cents == 5000 and "Reimbursed to unit 101" in expense.description
    client.post(f"/reimbursements/{second.id}/decide", data={"decision": "decline"})
    assert db.session.get(Reimbursement, second.id).status == "submitted"  # reason required
    client.post(f"/reimbursements/{second.id}/decide", data={"decision": "decline", "note": "Not a building cost"})
    assert db.session.get(Reimbursement, second.id).status == "declined"
    assert "Reimbursement declined: $20.00" in notes_for("alice@test.com")


def test_reimbursement_needs_receipt_and_privacy(app, client):
    login(client, "alice@test.com")
    resp = client.post("/reimbursements/new", data={"description": "X", "amount": "5", "spent_on": "2026-10-01",
                                                    "settle_method": "credit"})
    assert resp.status_code == 400 and db.session.scalar(db.select(db.func.count(Reimbursement.id))) == 0
    submit_reimbursement(client)
    r = db.session.scalar(db.select(Reimbursement))
    switch(client, "bob@test.com")  # tenant of another unit
    assert client.get(f"/reimbursements/{r.id}").status_code == 403
    assert client.get(f"/reimbursements/files/{r.files[0].id}").status_code == 403
    assert client.get("/reimbursements/new").status_code == 403  # tenants don't request


def test_accounting_units_panel_comes_first(admin_client):
    page = admin_client.get("/accounting/").data.decode()
    assert page.index("<h2>Units</h2>") < page.index("<h2>Payments") < page.index("<h2>Costs")
