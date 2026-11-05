import io
from datetime import date

import pytest
from openpyxl import Workbook

from condo.extensions import db
from condo.importer import ImportError_, parse_workbook
from condo.models import Charge, ImportBatch, Unit

UNITS = [("KIAN", "#161", 0.6), ("GILLES", "#165", 0.4)]


def make_workbook(balances=((100.0, -50.0), (200.5, -75.25)), sheet="Cotisations proprietaires"):
    """A two-unit workbook with the same layout as the real one, for 2026 Jan-Dec.

    balances[i] = (balance in Sep 2026, balance in Oct 2026) for unit i.
    Payments are recorded up to September only.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.cell(1, 4, 2026.0)
    for m, name in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]):
        ws.cell(2, 4 + m, name)
    ws.cell(2, 16, "Sum")
    sep, oct_ = 4 + 8, 4 + 9

    def block(start, label, values):
        ws.cell(start, 1, label)
        for i, (name, unit, share) in enumerate(UNITS):
            r = start + 2 + i
            ws.cell(r, 1, name)
            ws.cell(r, 2, unit)
            ws.cell(r, 3, share)
            for col, v in values(i).items():
                ws.cell(r, col, v)
        ws.cell(start + 2 + len(UNITS), 2, "Total")

    ws.cell(1, 1, None)
    block(1, "Includs Interest", lambda i: {sep: balances[i][0], oct_: balances[i][1]})
    block(10, "Interest Accumulated", lambda i: {sep: [4.5, 0][i]})
    block(20, "Monthly Condo fee received", lambda i: {c: 300.0 for c in range(4, sep + 1)})
    block(30, "Monthly Condo fee", lambda i: {c: [341.03, 327.0][i] for c in range(4, 16)})
    ws.cell(31, 1, None)
    ws.cell(32, 1, "NICOLAS/MARISE")
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_parse_picks_last_month_with_payments():
    data = parse_workbook(make_workbook(), today=date(2026, 11, 5))
    assert data["month"] == "2026-09"
    u161, u165 = data["units"]
    assert u161 == {"number": "161", "share": 0.6, "fee_cents": 34103, "owner": "Nicolas/Marise",
                    "balance_cents": 10000, "interest_cents": 450}
    assert u165["balance_cents"] == 20050 and u165["owner"] == "Gilles"


def test_parse_explicit_month():
    data = parse_workbook(make_workbook(), month=(2026, 10), today=date(2026, 11, 5))
    assert [u["balance_cents"] for u in data["units"]] == [-5000, -7525]


def test_parse_rejects_wrong_files():
    with pytest.raises(ImportError_, match="no \"Cotisations"):
        parse_workbook(make_workbook(sheet="Other"))
    with pytest.raises(ImportError_, match="couldn't be read"):
        parse_workbook(io.BytesIO(b"not a spreadsheet"))


def upload(client, **form):
    data = {"workbook": (make_workbook(), "sim.xlsx"), **form}
    return client.post("/import/", data=data, content_type="multipart/form-data")


def test_import_requires_admin(alice_client):
    assert alice_client.get("/import/").status_code == 403


def test_upload_only_previews(admin_client):
    before = db.session.scalar(db.select(db.func.count(Unit.id)))
    resp = upload(admin_client, month="2026-09")
    assert resp.status_code == 302
    assert db.session.scalar(db.select(db.func.count(Unit.id))) == before  # nothing written yet
    page = admin_client.get(resp.headers["Location"])
    assert page.status_code == 200 and b"Load units" in page.data and b"$341.03" in page.data


def test_each_dataset_loads_separately(admin_client):
    location = upload(admin_client, month="2026-09").headers["Location"]
    batch_id = int(location.rstrip("/").split("/")[-1])

    # Owners and balances refuse to load before the units exist.
    admin_client.post(f"/import/{batch_id}/load/balances")
    assert db.session.scalar(db.select(db.func.count(Charge.id)).where(Charge.kind == "opening")) == 0

    admin_client.post(f"/import/{batch_id}/load/units")
    u161 = db.session.scalar(db.select(Unit).where(Unit.number == "161"))
    assert (u161.monthly_fee_cents, u161.ownership_share, u161.owner_name) == (34103, 0.6, None)

    admin_client.post(f"/import/{batch_id}/load/owners",
                      data={"owner_161": "Nicolas & Marise", "owner_165": "Gilles"})
    assert db.session.get(Unit, u161.id).owner_name == "Nicolas & Marise"

    admin_client.post(f"/import/{batch_id}/load/balances")
    assert db.session.get(Unit, u161.id).balance == 10000
    opening = db.session.scalar(db.select(Charge).where(Charge.unit_id == u161.id))
    assert opening.due_date == date(2026, 9, 30) and "$4.50 late interest" in opening.description

    # Loading again (same upload or a new one) never duplicates.
    admin_client.post(f"/import/{batch_id}/load/balances")
    location = upload(admin_client, month="2026-09").headers["Location"]
    admin_client.post(location.rstrip("/") + "/load/balances")
    assert db.session.get(Unit, u161.id).balance == 10000
    assert db.session.get(ImportBatch, batch_id).payload["loaded"]["balances"]["by"] == "Admin"


def test_credit_balance_and_manual_form_cannot_create_opening(admin_client):
    location = upload(admin_client, month="2026-10").headers["Location"]
    admin_client.post(location.rstrip("/") + "/load/units")
    admin_client.post(location.rstrip("/") + "/load/balances")
    u165 = db.session.scalar(db.select(Unit).where(Unit.number == "165"))
    assert u165.balance == -7525
    resp = admin_client.post("/billing/charges/new", data={"unit_id": str(u165.id), "kind": "opening",
                                                           "description": "x", "amount": "5",
                                                           "due_date": "2026-10-01"})
    assert resp.status_code == 400


def test_upgrade_schema_adds_missing_columns(app):
    from sqlalchemy import inspect, text

    from condo.migrate import upgrade_schema
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE legacy_probe (id INTEGER)"))
    # Simulate an old database: rebuild `unit` without the new columns.
    db.drop_all()
    with db.engine.begin() as conn:
        conn.execute(text('CREATE TABLE unit (id INTEGER PRIMARY KEY, number VARCHAR(20) NOT NULL, '
                          'floor INTEGER, bedrooms INTEGER, area_sqft INTEGER, parking_spot VARCHAR(20), '
                          'monthly_fee_cents INTEGER NOT NULL DEFAULT 0, notes TEXT)'))
        conn.execute(text("DROP TABLE legacy_probe"))
    upgrade_schema()
    cols = {c["name"] for c in inspect(db.engine).get_columns("unit")}
    assert {"owner_name", "ownership_share"} <= cols
    db.create_all()  # leave a full schema for teardown
