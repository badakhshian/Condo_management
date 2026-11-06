from datetime import date

from condo.extensions import db
from condo.models import Expense, Payment, Unit, guess_cost_category
from condo.views.billing import resolve_period


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def seed():
    u1, u2 = unit("101"), unit("102")
    db.session.add_all([
        Payment(unit=u1, amount_cents=40000, paid_on=date(2026, 10, 5), method="bank transfer", notes="Fee payment"),
        Payment(unit=u2, amount_cents=35000, paid_on=date(2026, 11, 2), method="bank transfer"),
        Payment(unit=u1, amount_cents=10000, paid_on=date(2025, 12, 30), method="cash"),
        Expense(spent_on=date(2026, 11, 5), description="Hydra Electric invoice — parking heaters", amount_cents=-195458,
                origin="sheet"),
        Expense(spent_on=date(2026, 10, 20), description="Snow removal contract", category="maintenance", amount_cents=-60000),
    ])
    db.session.commit()


def test_resolve_period():
    today = date(2026, 11, 6)
    y = resolve_period({}, today)
    assert (y["view"], y["start"], y["end"], y["label"]) == ("year", date(2026, 1, 1), date(2026, 12, 31), "2026")
    m = resolve_period({"view": "month", "year": "2026", "month": "1"}, today)
    assert (m["start"], m["end"], m["label"]) == (date(2026, 1, 1), date(2026, 1, 31), "January 2026")
    assert m["prev"] == {"view": "month", "year": 2025, "month": 12}
    assert resolve_period({"view": "month", "year": "x"}, today)["label"] == "November 2026"


def test_year_view_has_separate_panels_and_monthly_table(admin_client):
    seed()
    page = admin_client.get("/accounting/?view=year&year=2026").data.decode()
    assert "<h1>Accounting</h1>" in page and "<title>Accounting · 2026" in page
    payments_panel = page.split("<h2>Payments")[1].split("</section>")[0]
    costs_panel = page.split("<h2>Costs")[1].split("</section>")[0]
    assert "$750.00" in payments_panel and "$100.00" not in payments_panel  # 2025 payment excluded
    assert "Hydra Electric" in costs_panel and "Snow removal" in costs_panel and "$2,554.58" in costs_panel
    assert "Repairs &amp; projects" in costs_panel and "Maintenance" in costs_panel
    assert "2026 by month" in page and "November" in page
    assert "−" in page  # net label


def test_month_view(admin_client):
    seed()
    page = admin_client.get("/accounting/?view=month&year=2026&month=10").data.decode()
    assert "October 2026" in page and "2026 by month" not in page
    payments_panel = page.split("<h2>Payments")[1].split("</section>")[0]
    assert "$400.00" in payments_panel and "$350.00" not in payments_panel
    assert "Hydra" not in page.split("<h2>Costs")[1].split("</section>")[0]


def test_add_and_remove_manual_cost(admin_client):
    resp = admin_client.post("/accounting/costs/new", data={"kind": "cost", "category": "insurance",
                                                            "description": "Building insurance premium",
                                                            "amount": "7,154.00", "spent_on": "2026-09-01"})
    assert resp.status_code == 302 and "view=month" in resp.headers["Location"]
    cost = db.session.scalar(db.select(Expense))
    assert cost.amount_cents == -715400 and cost.cost_cents == 715400 and cost.category_label == "Insurance"
    admin_client.post("/accounting/costs/new", data={"kind": "refund", "category": "repairs",
                                                     "description": "Contractor refund", "amount": "100",
                                                     "spent_on": "2026-09-15"})
    refund = db.session.scalar(db.select(Expense).where(Expense.description == "Contractor refund"))
    assert refund.cost_cents == -10000
    admin_client.post(f"/accounting/costs/{cost.id}/delete")
    assert db.session.get(Expense, cost.id) is None


def test_sheet_costs_cannot_be_removed(admin_client):
    seed()
    sheet_cost = db.session.scalar(db.select(Expense).where(Expense.origin == "sheet"))
    admin_client.post(f"/accounting/costs/{sheet_cost.id}/delete")
    assert db.session.get(Expense, sheet_cost.id) is not None


def test_cost_validation(admin_client):
    resp = admin_client.post("/accounting/costs/new", data={"kind": "cost", "category": "nope", "description": "",
                                                            "amount": "-5", "spent_on": "bad"})
    assert resp.status_code == 400 and db.session.scalar(db.select(db.func.count(Expense.id))) == 0


def test_old_billing_links_redirect(admin_client):
    resp = admin_client.get("/billing/?view=month&year=2026&month=3")
    assert resp.status_code == 301 and resp.headers["Location"] == "/accounting/?view=month&year=2026&month=3"
    assert admin_client.get("/billing/unit/1").headers["Location"] == "/accounting/unit/1"


def test_nav_says_accounting(admin_client):
    page = admin_client.get("/").data.decode()
    assert ">Accounting</a>" in page and ">Billing</a>" not in page


def test_accounting_is_admin_only(alice_client):
    assert alice_client.get("/accounting/").status_code == 403
    assert alice_client.get("/accounting/costs/new").status_code == 403


def test_guess_cost_category():
    assert guess_cost_category("Electricity_Hydro-Quebec") == "utilities"
    assert guess_cost_category("Snow Removal") == "maintenance"
    assert guess_cost_category("Insurance_Unique Cie Ass.") == "insurance"
    assert guess_cost_category("Bank charges") == "administration"
    assert guess_cost_category("parking heaters, labour & materials") == "repairs"
    assert guess_cost_category("Mystery") == "other"


def test_purchase_credit_is_also_a_cost_and_opening_not_billed(admin_client):
    from condo.models import Charge
    u = unit("102")
    db.session.add_all([
        Charge(unit=u, kind="opening", period="2026-09", due_date=date(2026, 9, 23), amount_cents=-72216,
               description="Opening balance"),
        Charge(unit=u, kind="dues", period="2026-10", due_date=date(2026, 10, 1), amount_cents=32700,
               description="Monthly fee"),
        Payment(unit=u, amount_cents=11431, paid_on=date(2026, 10, 9), method="purchase credit",
                notes="Purchase (reimbursement) — Leaf blower for condo yard"),
        Payment(unit=u, amount_cents=21126, paid_on=date(2026, 10, 10), method="bank transfer"),
    ])
    db.session.commit()
    page = admin_client.get("/accounting/?view=month&year=2026&month=10").data.decode()
    costs_panel = page.split("<h2>Costs")[1].split("</section>")[0]
    assert "Leaf blower for condo yard" in costs_panel and "Paid by owner of unit 102" in costs_panel
    assert "$114.31" in costs_panel and "Maintenance $114.31" in costs_panel
    # Net = cash actually received (211.26): payments 325.57 − owner-paid cost 114.31.
    assert "$211.26</span>Net" in page
    units = page.split("<h2>Units</h2>")[1]
    row = units.split(">102<")[1].split("</tr>")[0]
    assert "$327.00" in row  # billed excludes the opening balance
