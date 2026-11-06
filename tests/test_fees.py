from datetime import date, timedelta

from condo.extensions import db
from condo.models import (Charge, FeeSchedule, Notification, Payment, SpecialContribution, Unit, User,
                          split_by_weight)
from condo.views.fees import unit_summary

from .conftest import login
from .test_staff import staff


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def set_shares(*shares):
    for number, share in zip(("101", "102", "201"), shares):
        unit(number).ownership_share = share
    db.session.commit()


def test_split_by_weight_adds_up_exactly():
    parts = split_by_weight(100001, {1: 1, 2: 1, 3: 1})
    assert sum(parts.values()) == 100001 and sorted(parts.values()) == [33333, 33334, 33334]
    assert split_by_weight(500, {1: 0, 2: 0}) == {1: 0, 2: 0}


def test_workbook_rate_for_unit_161(app):
    # The 2026 budget of $34,691 split by unit 161's share gives $4,027.37 a year in the workbook.
    s = FeeSchedule(year=2026, annual_total_cents=3469100)
    u = Unit(id=901, number="161", ownership_share=402737 / 3469100)
    other = Unit(id=902, number="999", ownership_share=1 - 402737 / 3469100)
    rows = {r["unit"].number: r for r in s.unit_fees([u, other])}
    assert rows["161"]["annual"] == 402737
    assert rows["161"]["annual"] + rows["999"]["annual"] == 3469100


def test_save_schedule_with_override_and_apply(admin_client):
    set_shares(0.5, 0.3, 0.2)
    resp = admin_client.post("/accounting/fees/2026", data={"annual_total": "12,000", "override_%d" % unit("201").id: "1,200"})
    assert resp.status_code == 302
    s = db.session.scalar(db.select(FeeSchedule).where(FeeSchedule.year == 2026))
    rows = {r["unit"].number: r for r in s.unit_fees([unit("101"), unit("102"), unit("201")])}
    assert rows["201"]["annual"] == 120000 and rows["201"]["override"]
    assert rows["101"]["annual"] == 675000 and rows["102"]["annual"] == 405000  # 10,800 split 5:3
    assert rows["101"]["monthly"] == 56250
    page = admin_client.get("/accounting/fees?year=2026").data.decode()
    assert "$12,000.00" in page and "Special contributions" in page

    admin_client.post("/accounting/fees/2026/apply")
    assert unit("101").monthly_fee_cents == 56250 and unit("201").monthly_fee_cents == 10000
    assert s.applied_at is not None


def test_fixed_amounts_cannot_exceed_budget(admin_client):
    set_shares(0.5, 0.3, 0.2)
    admin_client.post("/accounting/fees/2026", data={"annual_total": "1,000", "override_%d" % unit("101").id: "2,000"})
    assert db.session.scalar(db.select(FeeSchedule)) is None


def test_special_contribution_lifecycle(admin_client):
    set_shares(0.5, 0.3, 0.2)
    deadline = date.today() + timedelta(days=30)
    resp = admin_client.post("/accounting/specials/new", data={"title": "Window replacement", "method": "share",
                                                               "total": "10,000", "deadline": deadline.isoformat()})
    sc = db.session.scalar(db.select(SpecialContribution))
    assert resp.headers["Location"].endswith(f"/accounting/specials/{sc.id}")
    assert {sh.unit.number: sh.amount_cents for sh in sc.shares} == {"101": 500000, "102": 300000, "201": 200000}
    assert sc.billed_at is None and not db.session.scalars(db.select(Charge)).all()

    admin_client.post(f"/accounting/specials/{sc.id}/bill")
    charges = db.session.scalars(db.select(Charge).where(Charge.kind == "special")).all()
    assert len(charges) == 3 and all(c.due_date == deadline for c in charges)
    alice = db.session.scalar(db.select(User).where(User.email == "alice@test.com"))
    note = db.session.scalar(db.select(Notification).where(Notification.user_id == alice.id))
    assert "$5,000.00" in note.title

    admin_client.post("/accounting/payments/new", data={"unit_id": unit("101").id, "method": "check", "amount": "2,000",
                                                        "paid_on": date.today().isoformat(), "special_id": sc.id})
    p = db.session.scalar(db.select(Payment))
    assert p.special_id == sc.id and "Window replacement" in p.notes
    rows = {r["unit"].number: r for r in sc.status_rows()}
    assert (rows["101"]["status"], rows["101"]["left"]) == ("partial", 300000)
    assert rows["102"]["status"] == "unpaid"
    assert {r["unit"].number: r["status"] for r in sc.status_rows(deadline + timedelta(days=1))}["102"] == "overdue"

    new = deadline + timedelta(days=14)
    admin_client.post(f"/accounting/specials/{sc.id}/deadline", data={"deadline": new.isoformat()})
    assert sc.deadline == new and all(c.due_date == new for c in charges)

    admin_client.post(f"/accounting/specials/{sc.id}/delete")
    assert db.session.get(SpecialContribution, sc.id) is not None  # billed: kept

    page = admin_client.get(f"/accounting/specials/{sc.id}").data.decode()
    assert "Window replacement" in page and "Partly paid" in page


def test_equal_and_custom_splits(admin_client):
    deadline = (date.today() + timedelta(days=10)).isoformat()
    admin_client.post("/accounting/specials/new", data={"title": "Roof", "method": "equal", "total": "100", "deadline": deadline})
    admin_client.post("/accounting/specials/new", data={"title": "Balcony", "method": "custom", "deadline": deadline,
                                                        "amount_%d" % unit("102").id: "750"})
    roof, balcony = db.session.scalars(db.select(SpecialContribution).order_by(SpecialContribution.id)).all()
    assert sorted(sh.amount_cents for sh in roof.shares) == [3333, 3333, 3334]
    assert [(sh.unit.number, sh.amount_cents) for sh in balcony.shares] == [("102", 75000)]
    # share split without shares set is refused
    resp = admin_client.post("/accounting/specials/new", data={"title": "X", "method": "share", "total": "100",
                                                               "deadline": deadline})
    assert resp.status_code == 400


def test_owner_sees_own_fee_and_specials(app, admin_client):
    set_shares(0.5, 0.3, 0.2)
    admin_client.post("/accounting/fees/%d" % date.today().year, data={"annual_total": "12,000"})
    admin_client.post("/accounting/specials/new", data={"title": "Elevator", "method": "share", "total": "1,000",
                                                        "deadline": (date.today() + timedelta(days=5)).isoformat()})
    sc = db.session.scalar(db.select(SpecialContribution))
    admin_client.post(f"/accounting/specials/{sc.id}/bill")
    summary = unit_summary(unit("101"))
    assert summary["fee"]["annual"] == 600000 and summary["specials"][0][1]["amount"] == 50000
    admin_client.post("/logout")

    login(admin_client, "alice@test.com")
    page = admin_client.get("/accounting/").data.decode()
    assert "Elevator" in page and "$6,000.00" in page
    assert admin_client.post("/accounting/fees/2026", data={"annual_total": "1"}).status_code in (302, 403)
    assert db.session.scalar(db.select(FeeSchedule)).annual_total_cents == 1200000


def test_permissions(app, client):
    login(client, "bob@test.com")  # tenant
    assert client.get("/accounting/fees").status_code in (302, 403)
    client.post("/logout")
    staff(["accounting"])
    login(client, "staff@x.com")
    page = client.get("/accounting/fees")
    assert page.status_code == 200 and "+ Special contribution" not in page.data.decode()
    assert client.post("/accounting/fees/2026", data={"annual_total": "5"}).status_code in (302, 403)
    assert db.session.scalar(db.select(FeeSchedule)) is None
    assert client.get("/accounting/specials/new").status_code in (302, 403)
