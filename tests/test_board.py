from condo import sheet_sync
from condo.extensions import db
from condo.models import Owner, Unit

from .conftest import login
from .test_sheet_sync import add_owners_tab, make_sheet


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def add(client, unit_obj, name, role="owner", positions=()):
    return client.post(f"/units/{unit_obj.id}/owners/new", data={
        "name": name, "role": role, "email": name.split()[0].lower() + "@x.com", "positions": list(positions)})


def person(name):
    return db.session.scalar(db.select(Owner).where(Owner.name == name))


def test_owner_can_hold_several_positions(admin_client):
    add(admin_client, unit("101"), "Raquel Silva", positions=["treasurer", "accounting"])
    assert person("Raquel Silva").position_labels == ["Treasurer", "Accounting"]
    page = admin_client.get(f"/units/{unit('101').id}").data
    assert b'class="badge board">Treasurer' in page and b'class="badge board">Accounting' in page


def test_single_holder_positions_move(admin_client):
    add(admin_client, unit("101"), "Ann First", positions=["president"])
    resp = add(admin_client, unit("102"), "Ben Second", positions=["president", "director"])
    assert person("Ann First").positions == []
    assert person("Ben Second").positions == ["president", "director"]
    page = admin_client.get(resp.headers["Location"]).data
    assert b"President moved from Ann First to Ben Second." in page
    # Directors and accounting can have several people.
    add(admin_client, unit("101"), "Cat Third", positions=["director"])
    assert person("Ben Second").positions == ["president", "director"]


def test_tenants_and_contacts_restricted(admin_client):
    resp = add(admin_client, unit("101"), "Tom Tenant", role="tenant", positions=["president"])
    assert resp.status_code == 400 and person("Tom Tenant") is None
    resp = add(admin_client, unit("101"), "Outside Cpa", role="contact", positions=["treasurer"])
    assert resp.status_code == 400
    assert add(admin_client, unit("101"), "Outside Cpa", role="contact", positions=["accounting"]).status_code == 302
    assert person("Outside Cpa").positions == ["accounting"]


def test_edit_keeps_and_changes_positions(admin_client):
    add(admin_client, unit("101"), "Ann First", positions=["secretary"])
    o = person("Ann First")
    assert b'value="secretary" checked' in admin_client.get(f"/owners/{o.id}/edit").data
    admin_client.post(f"/owners/{o.id}/edit", data={"name": "Ann First", "role": "owner", "positions": ["treasurer"]})
    assert person("Ann First").positions == ["treasurer"]


def test_board_tab_and_dashboards(admin_client, client):
    add(admin_client, unit("101"), "Ann First", positions=["president"])
    add(admin_client, unit("102"), "Ben Second", positions=["treasurer"])
    page = admin_client.get("/directory/board").data.decode()
    assert "Ann First" in page and "Ben Second" in page and "Vacant" in page  # secretary, VP empty
    assert "mailto:?bcc=ann@x.com,ben@x.com" in page
    assert "Board role" in admin_client.get("/directory/owners/export.csv").data.decode()
    assert "Board of directors" in admin_client.get("/").data.decode()
    admin_client.post("/logout")
    login(client, "bob@test.com")  # tenant sees whom to contact
    dash = client.get("/").data.decode()
    assert "Board of directors" in dash and "President" in dash and "Ann First" in dash
    assert client.get("/directory/board").status_code == 403


def test_sheet_board_role_column(app):
    data = add_owners_tab(make_sheet(), [
        ["#101", "Alice Chen", "", "", "", "Owner", "yes", ""],
        ["#102", "Bob Okafor", "", "", "", "Owner", "", ""],
    ])
    from openpyxl import load_workbook
    import io
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Owners"]
    ws.cell(1, 9, "Board role")
    ws.cell(2, 9, "Président, Treasury")
    ws.cell(3, 9, "Accountant / chef")
    buf = io.BytesIO()
    wb.save(buf)
    plan = sheet_sync.parse(buf.getvalue())
    assert any('unknown board role "chef"' in w for w in plan["warnings"])
    sheet_sync.apply(plan)
    assert person("Alice Chen").positions == ["president", "treasurer"]
    assert person("Bob Okafor").positions == ["accounting"]
