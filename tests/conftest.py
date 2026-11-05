import os

import pytest

from condo import create_app
from condo.extensions import db
from condo.models import Amenity, Unit, User


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False,
                      "SQLALCHEMY_DATABASE_URI": os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:"), "SECRET_KEY": "test"})
    with app.app_context():
        db.create_all()
        u1 = Unit(number="101", floor=1, monthly_fee_cents=40000)
        u2 = Unit(number="102", floor=1, monthly_fee_cents=35000)
        u3 = Unit(number="201", floor=2, monthly_fee_cents=0)
        admin = User(email="admin@test.com", name="Admin", role="admin")
        alice = User(email="alice@test.com", name="Alice", role="owner", unit=u1)
        bob = User(email="bob@test.com", name="Bob", role="tenant", unit=u2)
        for u in (admin, alice, bob):
            u.set_password("password123")
        db.session.add_all([u1, u2, u3, admin, alice, bob,
                            Amenity(name="Gym", open_hour=6, close_hour=22, max_hours=2)])
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, email, password="password123"):
    return client.post("/login", data={"email": email, "password": password})


@pytest.fixture
def admin_client(client):
    login(client, "admin@test.com")
    return client


@pytest.fixture
def alice_client(client):
    login(client, "alice@test.com")
    return client
