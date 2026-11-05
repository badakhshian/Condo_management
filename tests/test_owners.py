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
    page = admin_client.get("/directory/owners").data
    assert b"Alice Chen" in page and b"mailto:?bcc=alice@x.com" in page
    assert b"Bob Okafor" not in page.split(b"<tbody>")[1]  # tenants have their own tab
    assert b"Bob Okafor" in admin_client.get("/directory/tenants").data
    assert b"Alice" not in admin_client.get("/directory/owners?q=zzz").data.split(b"<tbody>")[1]
    csv = admin_client.get("/directory/tenants/export.csv")
    assert csv.mimetype == "text/csv" and b"Bob Okafor" in csv.data and b"Alice" not in csv.data


def test_owner_contacts_are_admin_only(alice_client):
    for path in ("/directory/owners", "/directory/tenants", "/directory/accounts",
                 "/directory/owners/export.csv", "/directory/accounts/export.csv", "/directory/tenants/new"):
        assert alice_client.get(path).status_code == 403, path
    assert alice_client.get(f"/units/{unit('101').id}/owners/new").status_code == 403


def test_copy_address_from_another_owner(admin_client):
    u101, u102 = unit("101"), unit("102")
    src = Owner(unit=u101, name="Alice Chen", role="owner", mailing_address="12 Elm St\nLaval QC")
    db.session.add_all([src, Owner(unit=u102, name="Bob", role="owner", mailing_address="9 Oak Ave")])
    db.session.commit()
    page = admin_client.get(f"/units/{u101.id}/owners/new").data
    assert b'<optgroup label="Unit 101">' in page and b"Alice Chen" in page and b"(unit 102)" in page

    # Without JavaScript: choosing a source with an empty address box copies it on the server.
    admin_client.post(f"/units/{u101.id}/owners/new",
                      data={"name": "Wei Chen", "role": "co-owner", "copy_address_from": str(src.id)})
    wei = db.session.scalar(db.select(Owner).where(Owner.name == "Wei Chen"))
    assert wei.mailing_address == "12 Elm St\nLaval QC"

    # A typed address wins over the picker.
    admin_client.post(f"/owners/{wei.id}/edit", data={"name": "Wei Chen", "role": "co-owner",
                                                      "copy_address_from": str(src.id), "mailing_address": "1 New Rd"})
    assert db.session.get(Owner, wei.id).mailing_address == "1 New Rd"


def test_edit_form_does_not_offer_own_address(admin_client):
    o = Owner(unit=unit("101"), name="Solo", role="owner", mailing_address="Only Address 1")
    db.session.add(o)
    db.session.commit()
    assert b"copy_address_from" not in admin_client.get(f"/owners/{o.id}/edit").data
