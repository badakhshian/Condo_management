from condo.extensions import db
from condo.models import MaintenanceRequest, Owner, Unit, User


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def back(page):
    html = page.data.decode()
    if 'class="back-link"' not in html:
        return None
    return html.split('class="back-link" href="')[1].split('"')[0], html.split("← Back to ")[1].split("<")[0]


def test_menu_pages_have_no_back_link(admin_client):
    for path in ("/", "/units/", "/directory/owners", "/directory/board", "/accounting/", "/maintenance/",
                 "/announcements/", "/documents/", "/amenities/"):
        assert back(admin_client.get(path)) is None, path


def test_detail_pages_point_to_their_parent(admin_client):
    u = unit("101")
    o = Owner(unit=u, name="Ann", role="owner")
    db.session.add(o)
    db.session.commit()
    expected = {
        f"/units/{u.id}": ("/units/", "Units"),
        f"/units/{u.id}/edit": (f"/units/{u.id}", "Unit"),
        f"/units/{u.id}/owners/new": (f"/units/{u.id}", "Unit"),
        f"/owners/{o.id}/edit": (f"/units/{u.id}", "Unit"),
        f"/owners/{o.id}/edit?next=directory": ("/directory/", "Directory"),
        "/directory/tenants/new": ("/directory/tenants", "Directory"),
        f"/units/{u.id}/insurance/owners/new": (f"/units/{u.id}", "Unit"),
        f"/accounting/unit/{u.id}": ("/accounting/", "Accounting"),
        "/accounting/costs/new": ("/accounting/", "Accounting"),
        "/accounting/payments/new": ("/accounting/", "Accounting"),
        "/maintenance/new": ("/maintenance/", "Maintenance"),
        "/documents/new": ("/documents/", "Documents"),
        "/announcements/new": ("/announcements/", "Notices"),
        "/residents/new": ("/directory/accounts", "Accounts"),
        "/sheet/": ("/accounting/", "Accounting"),
        "/import/": ("/units/", "Units"),
        "/account": ("/", "Dashboard"),
    }
    for path, want in expected.items():
        assert back(admin_client.get(path)) == want, path


def test_resident_back_links_go_to_dashboard(alice_client):
    u = db.session.scalar(db.select(User).where(User.email == "alice@test.com")).unit
    req = MaintenanceRequest(unit=u, submitted_by_id=db.session.scalar(db.select(User.id).where(
        User.email == "alice@test.com")), title="Tap", description="Drips")
    db.session.add(req)
    db.session.commit()
    assert back(alice_client.get(f"/accounting/unit/{u.id}")) == ("/accounting/", "Accounting")
    assert back(alice_client.get(f"/maintenance/{req.id}")) == ("/maintenance/", "Maintenance")


def test_back_link_follows_where_you_came_from(admin_client):
    u = unit("101")
    o = Owner(unit=u, name="Ann", role="owner", email="ann@x.com")
    db.session.add(o)
    db.session.commit()
    # From the Directory (with a search): back to that exact Directory view.
    directory = admin_client.get("/directory/owners?q=ann").data.decode()
    link = directory.split('href="/owners/')[1].split('"')[0]
    edit_url = "/owners/" + link.replace("&amp;", "&")
    page = admin_client.get(edit_url)
    assert back(page) == ("/directory/owners?q=ann", "Directory")
    # Saving goes back there too.
    resp = admin_client.post(edit_url, data={"name": "Ann B", "role": "owner"})
    assert resp.headers["Location"] == "/directory/owners?q=ann"

    # From the unit page: back to the unit.
    unit_page = admin_client.get(f"/units/{u.id}").data.decode()
    link = unit_page.split('href="/owners/')[1].split('"')[0]
    edit_url = "/owners/" + link.replace("&amp;", "&")
    assert back(admin_client.get(edit_url)) == (f"/units/{u.id}", "unit 101")
    resp = admin_client.post(edit_url, data={"name": "Ann B", "role": "owner"})
    assert resp.headers["Location"] == f"/units/{u.id}"


def test_account_pages_back_to_accounts_tab(admin_client):
    bob = db.session.scalar(db.select(User).where(User.email == "bob@test.com"))
    accounts = admin_client.get("/directory/accounts?q=bob").data.decode()
    assert f"/residents/{bob.id}/edit?back=/directory/accounts?q%3Dbob" in accounts or \
        f"/residents/{bob.id}/edit?back=%2Fdirectory%2Faccounts%3Fq%3Dbob" in accounts
    page = admin_client.get(f"/residents/{bob.id}/edit?back=/directory/accounts%3Fq%3Dbob")
    assert back(page) == ("/directory/accounts?q=bob", "Directory")


def test_back_param_cannot_leave_the_site(admin_client):
    u = unit("101")
    o = Owner(unit=u, name="Ann", role="owner")
    db.session.add(o)
    db.session.commit()
    for evil in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
        page = admin_client.get(f"/owners/{o.id}/edit", query_string={"back": evil})
        assert back(page) == (f"/units/{u.id}", "Unit")
        resp = admin_client.post(f"/owners/{o.id}/edit", query_string={"back": evil}, data={"name": "Ann", "role": "owner"})
        assert resp.headers["Location"] == f"/units/{u.id}"
