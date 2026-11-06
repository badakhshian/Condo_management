from condo.extensions import db
from condo.models import Owner, Unit, User, link_admin_accounts

from .conftest import login


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def user(email):
    return db.session.scalar(db.select(User).where(User.email == email))


def test_temporary_admin_becomes_own_account_when_given_a_position(admin_client):
    admin = user("admin@test.com")
    admin.name = "Building Manager"
    db.session.commit()
    banner = admin_client.get("/").data.decode()
    assert "temporary administrator account" in banner and "admin@test.com" in banner
    # Add yourself as an owner with the same email, then take your board position.
    me = Owner(unit=unit("101"), name="Mostafa Badakhshian", role="owner", email="ADMIN@test.com")
    db.session.add(me)
    db.session.commit()
    resp = admin_client.post("/directory/board/assign", data={"position": "president", "owner_id": str(me.id)})
    page = admin_client.get(resp.headers["Location"]).data.decode()
    assert "is now Mostafa Badakhshian&#39;s own account" in page
    admin = user("admin@test.com")
    assert (admin.role, admin.unit.number, admin.name) == ("owner", "101", "Mostafa Badakhshian")
    assert admin.is_admin and admin.role_label == "President"
    dash = admin_client.get("/").data.decode()
    assert "temporary administrator account" not in dash and ">President</span>" in dash
    assert "Management dashboard" in dash  # still manages the building, as President


def test_conversion_waits_for_a_board_position(app):
    db.session.add(Owner(unit=unit("101"), name="Someone", role="owner", email="admin@test.com"))
    db.session.commit()
    assert link_admin_accounts() == []  # no position yet: stays a temporary admin, nobody is locked out
    assert user("admin@test.com").role == "admin"


def test_board_position_grants_management(app, client):
    o = Owner(unit=unit("101"), name="Alice Chen", role="owner", email="alice@test.com")
    o.positions = ["secretary"]
    db.session.add(o)
    db.session.commit()
    login(client, "alice@test.com")
    for path in ("/units/", "/directory/owners", "/accounting/", "/documents/new", "/announcements/new"):
        assert client.get(path).status_code == 200, path
    # Losing the position removes the access.
    o.positions = []
    db.session.commit()
    assert client.get("/units/").status_code == 403


def test_account_roles_are_owner_or_tenant(admin_client):
    form = admin_client.get("/residents/new?role=owner").data.decode()
    assert 'value="owner"' in form and 'value="tenant"' in form and 'value="admin"' not in form
    resp = admin_client.post("/residents/new", data={"name": "X", "email": "x@x.com", "role": "admin",
                                                     "unit_id": str(unit("101").id), "password": "password123"})
    assert resp.status_code == 400 and user("x@x.com") is None


def test_temporary_admin_cannot_drop_own_access_by_mistake(admin_client):
    admin = user("admin@test.com")
    resp = admin_client.post(f"/residents/{admin.id}/edit", data={"name": "Admin", "email": "admin@test.com",
                                                                  "role": "owner", "unit_id": str(unit("101").id),
                                                                  "active": "on"})
    assert resp.status_code == 400 and user("admin@test.com").role == "admin"
