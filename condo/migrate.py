"""Minimal additive schema upgrades.

`db.create_all()` creates missing tables but never adds columns to tables that
already exist. Each entry below is a column added after the first release; it is
added with ALTER TABLE when absent. Only nullable columns belong here, so the
upgrade is safe on tables that already hold rows.
"""
from sqlalchemy import inspect, text

from .extensions import db

ADDED_COLUMNS = [
    ("unit", "owner_name", "VARCHAR(200)"),
    ("unit", "ownership_share", "FLOAT"),
    ("charge", "origin", "VARCHAR(20)"),
    ("payment", "origin", "VARCHAR(20)"),
    ("payment", "notes", "VARCHAR(255)"),
]


def upgrade_schema():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    preparer = db.engine.dialect.identifier_preparer
    with db.engine.begin() as conn:
        for table, column, ddl_type in ADDED_COLUMNS:
            if table not in tables:
                continue  # create_all() builds it with every column
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {preparer.quote(table)} ADD COLUMN {preparer.quote(column)} {ddl_type}"))
