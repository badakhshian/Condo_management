"""Who gets told what. Notifications are stored per user and shown under the bell.

Board positions are matched to website accounts by email (see utils.board_positions_of).
When nobody in a position has an account yet, managers receive its notifications so
nothing waits unseen.
"""
from flask_login import current_user

from .extensions import db
from .i18n import in_language
from .models import BOARD_POSITIONS, Notification, Owner, User

BOARD_KEYS = tuple(BOARD_POSITIONS)


def _active(stmt):
    return db.session.scalars(stmt.where(User.active.is_(True))).all()


def admins():
    """People who manage the building: board members with an account, plus any temporary admin."""
    temp = _active(db.select(User).where(User.role == "admin"))
    return temp + [u for u in with_positions(list(BOARD_KEYS)) if u not in temp]


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
    return with_positions(list(BOARD_KEYS)) or admins()


def everyone():
    return _active(db.select(User))


def _site_language():
    from .i18n import current_language
    return current_language()


def send(users, title, url=None, body=None, kind=None, include_actor=False):
    """Queue a notification for each user (deduplicated). The caller commits."""
    actor_id = current_user.id if getattr(current_user, "is_authenticated", False) else None
    seen = set()
    for user in users:
        if user is None or user.id in seen or (user.id == actor_id and not include_actor):
            continue
        seen.add(user.id)
        # Write it in the recipient's language (titles and bodies may be lazy translations).
        with in_language(getattr(user, "language", None) or _site_language()):
            t, b = str(title), (str(body) if body is not None else None)
        db.session.add(Notification(user_id=user.id, title=t[:255], url=url, body=b, kind=kind))


def unread_count(user):
    if not getattr(user, "is_authenticated", False):
        return 0
    return db.session.scalar(db.select(db.func.count(Notification.id)).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)))
