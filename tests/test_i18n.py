from datetime import date

from condo.extensions import db
from condo.models import FeeSchedule, Notification, Unit, User

from .conftest import login


def user(email):
    return db.session.scalar(db.select(User).where(User.email == email))


def test_switch_to_french_and_back(admin_client):
    page = admin_client.get("/").data.decode()
    assert '<html lang="en">' in page and "Management dashboard" in page
    resp = admin_client.get("/language/fr?back=/accounting/fees")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/accounting/fees")
    assert "lang=fr" in resp.headers.get("Set-Cookie", "")
    assert user("admin@test.com").language == "fr"
    page = admin_client.get("/").data.decode()
    assert '<html lang="fr">' in page and "Tableau de bord de gestion" in page and "Comptabilité" in page
    admin_client.get("/language/en")
    assert "Management dashboard" in admin_client.get("/").data.decode()
    assert admin_client.get("/language/de").status_code == 404


def test_sign_in_page_follows_browser_and_switch(client):
    assert "Se connecter" in client.get("/login", headers={"Accept-Language": "fr-CA,fr;q=0.9"}).data.decode()
    assert "Sign in" in client.get("/login", headers={"Accept-Language": "en-US"}).data.decode()
    client.get("/language/fr")
    assert "Se connecter" in client.get("/login").data.decode()


def test_language_is_remembered_at_sign_in(client):
    client.get("/language/fr")
    login(client, "alice@test.com")
    assert user("alice@test.com").language == "fr"


def test_french_money_dates_and_amounts(admin_client):
    admin_client.get("/language/fr")
    resp = admin_client.post("/accounting/fees/2026", data={"basis": "annual", "amount": "34 691,00",
                                                            "starts_on": "2026-01-01", "ends_on": "2026-12-31"},
                             follow_redirects=True)
    page = resp.data.decode()
    assert db.session.scalar(db.select(FeeSchedule)).amount_cents == 3469100
    assert "Frais de condo enregistrés" in page and "34 691,00 $" in page
    assert "1 janv. 2026" in page or "1 janv. 2026".replace(" ", " ") in page


def test_notifications_use_each_readers_language(app, admin_client):
    # Alice reads French; the admin works in English.
    alice = user("alice@test.com")
    alice.language = "fr"
    db.session.commit()
    admin_client.post("/accounting/fees/2026", data={"basis": "annual", "amount": "1,200", "starts_on": "2026-01-01",
                                                     "ends_on": "2026-12-31"})
    admin_client.post("/accounting/specials/new", data={"title": "Toit", "method": "equal", "total": "300",
                                                        "deadline": "2027-03-31"})
    from condo.models import SpecialContribution
    sc = db.session.scalar(db.select(SpecialContribution))
    admin_client.post(f"/accounting/specials/{sc.id}/bill")
    note = db.session.scalar(db.select(Notification).where(Notification.user_id == alice.id))
    assert note.title.startswith("Cotisation spéciale : 100,00") and "31 mars 2027" in note.title
    assert "Special contribution" in admin_client.get("/accounting/specials/%d" % sc.id).data.decode()


def test_labels_translate(admin_client):
    admin_client.get("/language/fr")
    page = admin_client.get("/maintenance/new").data.decode()
    assert ">Plomberie<" in page and 'value="Plumbing"' in page  # stored in English, shown in French
