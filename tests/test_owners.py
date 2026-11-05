from condo.extensions import db
from condo.models import Owner, Unit


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def test_add_several_owners_with_contacts(admin_client):
    u = unit("101")
    for name, email, primary in [("Alice Chen", "alice@x.com", "on"), ("Wei Chen", "wei@x.com", "on")]:
        resp = admin_client.post(f"/units/{u.id}/owners/new", data={
            "name": name, "email": email, "phone": "514-555-0100", "role": "co-owner",
            "mailing_address": "12 Elm St\nLaval", "is_primary": primary})
        assert resp.status_code == 302
    owners = db.session.get(Unit, u.id).owners
    assert [o.name for o in owners if o.is_primary] == ["Wei Chen"]  # only one primary contact
    page = admin_client.get(f"/units/{u.id}").data
    assert b"alice@x.com" in page and b"12 Elm St" in page


def test_owner_validation(admin_client):
    u = unit("101")
    resp = admin_client.post(f"/units/{u.id}/owners/new", data={"name": "", "email": "bad", "role": "owner"})
    assert resp.status_code == 400
    assert db.session.scalar(db.select(db.func.count(Owner.id))) == 0


def test_edit_and_delete_owner(admin_client):
    o = Owner(unit=unit("102"), name="Bob", role="owner")
    db.session.add(o)
    db.session.commit()
    admin_client.post(f"/owners/{o.id}/edit", data={"name": "Bob Okafor", "role": "owner", "phone": "555"})
    assert db.session.get(Owner, o.id).name == "Bob Okafor"
    admin_client.post(f"/owners/{o.id}/delete")
    assert db.session.get(Owner, o.id) is None


def test_sheet_owners_are_read_only(admin_client):
    o = Owner(unit=unit("102"), name="Bob", role="owner", origin="sheet")
    db.session.add(o)
    db.session.commit()
    admin_client.post(f"/owners/{o.id}/delete")
    assert db.session.get(Owner, o.id) is not None


def test_directory_search_and_csv(admin_client):
    u101, u102 = unit("101"), unit("102")
    db.session.add_all([Owner(unit=u101, name="Alice Chen", email="alice@x.com", role="owner"),
                        Owner(unit=u102, name="Bob Okafor", phone="514-555-0199", role="tenant")])
    db.session.commit()
    page = admin_client.get("/owners/").data
    assert b"Alice Chen" in page and b"mailto:?bcc=alice@x.com" in page
    assert b"Bob" not in admin_client.get("/owners/?q=alice").data.split(b"<tbody>")[1]
    csv = admin_client.get("/owners/export.csv?role=tenant")
    assert csv.mimetype == "text/csv" and b"Bob Okafor" in csv.data and b"Alice" not in csv.data


def test_owner_contacts_are_admin_only(alice_client):
    assert alice_client.get("/owners/").status_code == 403
    assert alice_client.get("/owners/export.csv").status_code == 403
    assert alice_client.get(f"/units/{unit('101').id}/owners/new").status_code == 403
