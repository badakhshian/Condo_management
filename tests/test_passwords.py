import re

from condo.extensions import db
from condo.models import Owner, Unit, User
from condo.views.residents import generate_password

from .conftest import login


def user(email):
    return db.session.scalar(db.select(User).where(User.email == email))


def test_generated_passwords_are_readable_and_strong():
    pw = generate_password()
    assert re.fullmatch(r"[A-Za-z2-9]{4}-[A-Za-z2-9]{4}-[A-Za-z2-9]{4}", pw)
    assert not set(pw) & set("0O1lI")
    assert len({generate_password() for _ in range(50)}) == 50


def test_board_generates_password_shown_once_and_forces_change(admin_client, client):
    bob = user("bob@test.com")
    page = admin_client.post(f"/residents/{bob.id}/password", data={"action": "generate", "must_change": "on"})
    html = page.data.decode()
    pw = re.search(r'<code id="secret">([^<]+)</code>', html).group(1)
    assert "won't be shown again" in html
    bob = user("bob@test.com")
    assert bob.check_password(pw) and bob.must_change_password
    assert pw not in admin_client.get("/directory/accounts").data.decode()
    assert "temporary" in admin_client.get("/directory/accounts").data.decode()

    admin_client.post("/logout")
    resp = client.post("/login", data={"email": "bob@test.com", "password": pw})
    assert resp.headers["Location"] == "/account"
    assert client.get("/maintenance/").headers["Location"] == "/account"  # locked until changed
    resp = client.post("/account", data={"current_password": pw, "new_password": pw, "confirm_password": pw})
    assert resp.status_code == 400  # must differ
    client.post("/account", data={"current_password": pw, "new_password": "my-own-pass-9",
                                  "confirm_password": "my-own-pass-9"})
    bob = user("bob@test.com")
    assert not bob.must_change_password and bob.check_password("my-own-pass-9")
    assert client.get("/maintenance/").status_code == 200


def test_board_types_a_password(admin_client):
    bob = user("bob@test.com")
    resp = admin_client.post(f"/residents/{bob.id}/password", data={"action": "set", "password": "short",
                                                                    "confirm": "short"})
    assert resp.status_code == 400
    resp = admin_client.post(f"/residents/{bob.id}/password", data={"action": "set", "password": "longenough1",
                                                                    "confirm": "different1"})
    assert resp.status_code == 400
    admin_client.post(f"/residents/{bob.id}/password", data={"action": "set", "password": "longenough1",
                                                             "confirm": "longenough1"})
    bob = user("bob@test.com")
    assert bob.check_password("longenough1") and not bob.must_change_password


def test_own_password_not_changed_here(admin_client):
    admin = user("admin@test.com")
    resp = admin_client.post(f"/residents/{admin.id}/password", data={"action": "generate"})
    assert resp.headers["Location"] == "/account" and user("admin@test.com").check_password("password123")


def test_only_board_sets_passwords(alice_client):
    bob = user("bob@test.com")
    assert alice_client.get(f"/residents/{bob.id}/password").status_code == 403
    assert alice_client.post(f"/residents/{bob.id}/password", data={"action": "generate"}).status_code == 403


def test_create_login_from_directory(admin_client):
    u = db.session.scalar(db.select(Unit).where(Unit.number == "102"))
    db.session.add_all([Owner(unit=u, name="Olivia Owner", role="owner", email="olivia@x.com"),
                        Owner(unit=u, name="Bob", role="tenant", email="bob@test.com")])
    db.session.commit()
    owners = admin_client.get("/directory/owners").data.decode()
    assert "Create login" in owners and "email=olivia@x.com" in owners
    tenants = admin_client.get("/directory/tenants").data.decode()
    bob_id = db.session.scalar(db.select(User.id).where(User.email == "bob@test.com"))
    assert f"/residents/{bob_id}/password" not in tenants and f"/residents/{bob_id}/edit" in tenants
    account = admin_client.get(f"/residents/{bob_id}/edit").data.decode()
    assert f"/residents/{bob_id}/password" in account and f"/residents/{bob_id}/reset-password" in account
    form = admin_client.get(f"/residents/new?name=Olivia+Owner&email=olivia%40x.com&unit_id={u.id}&role=owner").data.decode()
    assert 'value="Olivia Owner"' in form and 'value="olivia@x.com"' in form and "Generate" in form
    admin_client.post("/residents/new", data={"name": "Olivia Owner", "email": "olivia@x.com", "role": "owner",
                                              "unit_id": str(u.id), "password": "Abcd-Efgh-Jkmn", "must_change": "on"})
    assert user("olivia@x.com").must_change_password



def test_reset_password_from_the_account_page(admin_client, client):
    bob = user("bob@test.com")
    page = admin_client.post(f"/residents/{bob.id}/reset-password").data.decode()
    assert "Password reset" in page and "It won't be shown again." in page
    temp = page.split('<code id="secret">')[1].split("</code>")[0]
    assert bob.must_change_password and bob.check_password(temp) and not bob.check_password("password123")
    # Bob signs in with the temporary password and is sent to choose his own.
    client.post("/logout")
    client.post("/login", data={"email": "bob@test.com", "password": temp})
    assert user("bob@test.com").must_change_password
    assert "/account" in client.get("/").headers.get("Location", "")


def test_nobody_resets_their_own_or_without_board_rights(admin_client, client):
    admin = user("admin@test.com")
    admin_client.post(f"/residents/{admin.id}/reset-password")
    assert admin.check_password("password123")
    client.post("/logout")
    login(client, "alice@test.com")
    bob = user("bob@test.com")
    assert client.post(f"/residents/{bob.id}/reset-password").status_code in (302, 403)
    assert bob.check_password("password123")


def test_sign_in_page_explains_forgotten_passwords(client):
    assert "Forgot your password? Ask a board member to reset it for you." in client.get("/login").data.decode()


def test_contact_page_has_the_password_panel(admin_client):
    u = db.session.scalar(db.select(Unit).where(Unit.number == "102"))
    with_login = Owner(unit=u, name="Bob", role="tenant", email="BOB@test.com")
    no_login = Owner(unit=u, name="Olivia Owner", role="owner", email="olivia@x.com")
    db.session.add_all([with_login, no_login])
    db.session.commit()
    bob = user("bob@test.com")
    page = admin_client.get(f"/owners/{with_login.id}/edit").data.decode()
    assert "Sign-in account and password" in page
    assert f"/residents/{bob.id}/password" in page and f"/residents/{bob.id}/reset-password" in page
    page = admin_client.get(f"/owners/{no_login.id}/edit").data.decode()
    assert "No sign-in account yet." in page and "/residents/new?" in page and "email=olivia@x.com" in page
