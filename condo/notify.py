"""Who gets told what. Notifications are stored per user and shown under the bell.

Board positions are matched to website accounts by email (see utils.board_positions_of).
When nobody in a position has an account yet, managers receive its notifications so
nothing waits unseen.
"""
from flask_login import current_user

from .extensions import db
from .models import Notification, Owner, User


def _active(stmt):
    return db.session.scalars(stmt.where(User.active.is_(True))).all()


def admins():
    return _active(db.select(User).where(User.role == "admin"))


def unit_owner_accounts(unit_id):
    return _active(db.select(User).where(User.unit_id == unit_id, User.role == "owner")) if unit_id else []


def with_positions(keys):
    """Accounts of people holding any of these board positions."""
    people = db.session.scalars(db.select(Owner).where(Owner.board_roles.is_not(None), Owner.email.is_not(None))).all()
    emails = {o.email.lower() for o in people if set(o.positions) & set(keys)}
    if not emails:
        return []
    return _active(db.select(User).where(db.func.lower(User.email).in_(emails)))


def president():
    return with_positions(["president"]) or admins()


def accounting():
    return with_positions(["accounting", "treasurer"]) or admins()


def board():
    return with_positions(["president", "vice-president", "treasurer", "secretary", "accounting", "director"]) \
        or admins()


def everyone():
    return _active(db.select(User))


def send(users, title, url=None, body=None, kind=None, include_actor=False):
    """Queue a notification for each user (deduplicated). The caller commits."""
    actor_id = current_user.id if getattr(current_user, "is_authenticated", False) else None
    seen = set()
    for user in users:
        if user is None or user.id in seen or (user.id == actor_id and not include_actor):
            continue
        seen.add(user.id)
        db.session.add(Notification(user_id=user.id, title=title[:255], url=url, body=body, kind=kind))


def unread_count(user):
    if not getattr(user, "is_authenticated", False):
        return 0
    return db.session.scalar(db.select(db.func.count(Notification.id)).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)))
