from datetime import timedelta

import pytest

from condo.extensions import db
from condo.models import Notification, Poll, PollVoter, Unit, User, utcnow

from .conftest import login


def unit(number):
    return db.session.scalar(db.select(Unit).where(Unit.number == number))


def owner(email, name, unit_obj):
    u = User(email=email, name=name, role="owner", unit=unit_obj)
    u.set_password("password123")
    db.session.add(u)
    return u


@pytest.fixture
def building(app):
    """101 (60%) has Alice; 102 (40%) has two owners, Carl and Dana."""
    unit("101").ownership_share = 0.6
    unit("102").ownership_share = 0.4
    owner("carl@test.com", "Carl", unit("102"))
    owner("dana@test.com", "Dana", unit("102"))
    db.session.commit()


def deadline(hours=48):
    from condo.views.votes import to_local
    return to_local(utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def draft(admin_client, **extra):
    data = {"title": "Approve the roof budget?", "audience": "owners", "weighting": "share",
            "deadline": deadline(), "options": ["Yes", "No", "Abstain"]}
    data.update(extra)
    admin_client.post("/votes/new", data=data)
    return db.session.scalar(db.select(Poll).order_by(Poll.id.desc()))


def notes_for(email):
    u = db.session.scalar(db.select(User).where(User.email == email))
    return [n.title for n in db.session.scalars(db.select(Notification).where(Notification.user_id == u.id))]


def as_user(client, email):
    client.post("/logout")
    login(client, email)


def test_draft_tells_nobody_and_publish_weights_by_share(building, admin_client):
    p = draft(admin_client)
    assert p.status == "draft" and [o.text for o in p.options] == ["Yes", "No", "Abstain"]
    assert notes_for("alice@test.com") == []
    page = admin_client.get(f"/votes/{p.id}").data.decode()
    assert "Draft: nobody has been notified" in page and "Voters if published now (3)" in page

    admin_client.post(f"/votes/{p.id}/publish")
    weights = {v.user.email: round(v.weight, 4) for v in p.voters}
    assert weights == {"alice@test.com": 0.6, "carl@test.com": 0.2, "dana@test.com": 0.2}
    assert p.state == "open" and any(t.startswith("New vote") for t in notes_for("dana@test.com"))


def test_one_vote_per_unit_and_per_person(building, admin_client):
    p = draft(admin_client, weighting="unit")
    admin_client.post(f"/votes/{p.id}/publish")
    assert {v.user.email: round(v.weight, 4) for v in p.voters} == {"alice@test.com": 0.5, "carl@test.com": 0.25,
                                                                    "dana@test.com": 0.25}
    q = draft(admin_client, weighting="person", audience="units", unit_ids=[str(unit("102").id)])
    admin_client.post(f"/votes/{q.id}/publish")
    assert {v.user.email: round(v.weight, 4) for v in q.voters} == {"carl@test.com": 0.5, "dana@test.com": 0.5}


def test_voting_privacy_and_results_after_deadline(building, app, admin_client):
    p = draft(admin_client)
    admin_client.post(f"/votes/{p.id}/publish")
    yes, no = p.options[0], p.options[1]
    client = admin_client
    as_user(client, "carl@test.com")
    page = client.get(f"/votes/{p.id}").data.decode()
    assert "Your vote counts for 20" in page and "Results are shown after the deadline" in page
    client.post(f"/votes/{p.id}/vote", data={"option_id": yes.id})
    as_user(client, "alice@test.com")
    client.post(f"/votes/{p.id}/vote", data={"option_id": no.id})
    client.post(f"/votes/{p.id}/vote", data={"option_id": yes.id})  # changes her mind
    page = client.get(f"/votes/{p.id}").data.decode()
    assert "Ballots" not in page and "Carl" not in page and "Results" not in page.split("Your vote")[0]

    as_user(client, "admin@test.com")
    page = client.get(f"/votes/{p.id}").data.decode()
    assert "Ballots" in page and "Carl" in page and "live, board only" in page
    r = p.results()
    assert round(r["rows"][0]["pct_of_cast"]) == 100 and round(r["turnout"]) == 80 and r["voted"] == 2

    p.deadline = utcnow() - timedelta(minutes=1)  # deadline passes
    db.session.commit()
    as_user(client, "dana@test.com")
    page = client.get(f"/votes/{p.id}").data.decode()
    assert "Results" in page and "You did not vote." in page and "Carl" not in page
    assert client.post(f"/votes/{p.id}/vote", data={"option_id": yes.id}).status_code == 302
    assert db.session.scalar(db.select(PollVoter).join(User).where(User.email == "dana@test.com")).option_id is None
    assert any(t.startswith("Results are in") for t in notes_for("dana@test.com"))


def test_reminder_a_day_before_and_close_early(building, app, admin_client):
    from condo.views.votes import check_polls
    p = draft(admin_client, deadline=deadline(hours=30))
    admin_client.post(f"/votes/{p.id}/publish")
    with app.test_request_context():
        assert check_polls(utcnow()) == 0
        assert check_polls(utcnow() + timedelta(hours=7)) == 1
    assert any(t.startswith("Reminder: vote on") for t in notes_for("carl@test.com"))
    admin_client.post(f"/votes/{p.id}/close")
    assert p.state == "closed" and any(t.startswith("Results are in") for t in notes_for("alice@test.com"))


def test_who_can_do_what(building, app, admin_client):
    p = draft(admin_client)
    client = admin_client
    as_user(client, "alice@test.com")
    assert client.get(f"/votes/{p.id}").status_code == 403  # drafts are board-only
    assert client.get("/votes/new").status_code == 403
    as_user(client, "admin@test.com")
    client.post(f"/votes/{p.id}/publish")
    as_user(client, "bob@test.com")  # a tenant: not invited
    assert client.get(f"/votes/{p.id}").status_code == 403
    as_user(client, "alice@test.com")
    assert client.post(f"/votes/{p.id}/close").status_code == 403
    assert "Approve the roof budget?" in client.get("/votes/").data.decode()


def test_publish_needs_shares_and_accounts(app, admin_client):
    p = draft(admin_client)  # unit 101 has no share in the base fixture
    admin_client.post(f"/votes/{p.id}/publish")
    assert p.status == "draft"


def test_validation(admin_client):
    resp = admin_client.post("/votes/new", data={"title": "", "audience": "owners", "weighting": "share",
                                                 "deadline": "2020-01-01T10:00", "options": ["Only one"]})
    assert resp.status_code == 400 and db.session.scalar(db.select(Poll)) is None


def test_back_link_and_menu(building, admin_client):
    p = draft(admin_client)
    assert "← Back to Votes" in admin_client.get(f"/votes/{p.id}").data.decode()
    assert 'class="label">Votes</span>' in admin_client.get("/").data.decode()
