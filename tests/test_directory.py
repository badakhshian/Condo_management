from condo.extensions import db
from condo.models import Owner, Unit


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def test_directory_tabs_and_counts(admin_client):
    u = unit("101")
    db.session.add_all([Owner(unit=u, name="Alice Chen", role="owner"),
                        Owner(unit=u, name="Wei Chen", role="co-owner"),
                        Owner(unit=u, name="Tom Renter", role="tenant")])
    db.session.commit()
    resp = admin_client.get("/directory/")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/directory/owners")
    page = admin_client.get("/directory/owners").data
    assert b'Owners <span class="count">2</span>' in page
    assert b'Tenants <span class="count">1</span>' in page
    assert b'Accounts <span class="count">3</span>' in page  # fixture logins: admin, alice, bob
    accounts = admin_client.get("/directory/accounts").data
    assert b"Admin" in accounts and b"Administrator (temporary)" in accounts
    assert b"Directory" in admin_client.get("/").data


def test_old_addresses_redirect(admin_client):
    assert admin_client.get("/owners/").headers["Location"].endswith("/directory/owners")
    assert admin_client.get("/residents/").headers["Location"].endswith("/directory/accounts")


def test_add_tenant_from_directory_picks_unit(admin_client):
    page = admin_client.get("/directory/tenants/new").data
    assert b'name="unit_id"' in page and b"Add a tenant" in page
    resp = admin_client.post("/directory/tenants/new", data={"name": "Tom", "role": "tenant"})
    assert resp.status_code == 400  # no unit chosen
    resp = admin_client.post("/directory/tenants/new", data={"unit_id": str(unit("102").id), "name": "Tom Renter",
                                                             "role": "tenant", "email": "tom@x.com"})
    assert resp.headers["Location"].endswith("/directory/tenants")
    tom = db.session.scalar(db.select(Owner).where(Owner.name == "Tom Renter"))
    assert tom.unit.number == "102" and tom.role == "tenant"


def test_add_tenant_from_unit_page(admin_client):
    u = unit("102")
    assert b'value="tenant" selected' in admin_client.get(f"/units/{u.id}/owners/new?role=tenant").data


def test_edit_from_directory_returns_to_tab(admin_client):
    o = Owner(unit=unit("101"), name="Tom", role="tenant")
    db.session.add(o)
    db.session.commit()
    resp = admin_client.post(f"/owners/{o.id}/edit?next=directory", data={"name": "Tom R", "role": "tenant"})
    assert resp.headers["Location"].endswith("/directory/tenants")


def test_accounts_search_and_export(admin_client):
    tenants = admin_client.get("/directory/accounts?role=tenant").data.split(b"<tbody>")[1]
    assert b"Bob" in tenants and b"Admin" not in tenants
    csv = admin_client.get("/directory/accounts/export.csv").data
    assert b"admin@test.com" in csv and b"bob@test.com" in csv
