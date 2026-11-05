"""Import units, owner names and opening balances from the Excel workbook.

Uploading only parses the file and stores the result; nothing is written to the
units or the ledger until the manager clicks "Load" for each dataset.
"""
import copy

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..importer import ImportError_, month_end, month_label, parse_workbook
from ..models import Charge, ImportBatch, Unit, utcnow
from ..utils import admin_required, money

bp = Blueprint("importer", __name__, url_prefix="/import")

STEPS = ("units", "owners", "balances")


@bp.route("/", methods=["GET", "POST"])
@admin_required
def upload():
    if request.method == "POST":
        file = request.files.get("workbook")
        if not file or not file.filename:
            flash("Choose the Excel file to upload.", "error")
            return render_template("import/upload.html"), 400
        month = None
        raw_month = request.form.get("month", "").strip()
        if raw_month:
            try:
                year, m = (int(x) for x in raw_month.split("-"))
                month = (year, m)
            except ValueError:
                flash("Choose a valid month.", "error")
                return render_template("import/upload.html"), 400
        try:
            payload = parse_workbook(file.stream, month=month)
        except ImportError_ as exc:
            flash(str(exc), "error")
            return render_template("import/upload.html"), 400
        batch = ImportBatch(filename=file.filename[:255], payload=payload, created_by_id=current_user.id)
        db.session.add(batch)
        db.session.commit()
        return redirect(url_for("importer.preview", batch_id=batch.id))
    recent = db.session.scalars(db.select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(5)).all()
    return render_template("import/upload.html", recent=recent)


def _batch(batch_id):
    return db.session.get(ImportBatch, batch_id) or abort(404)


def _existing_units(payload):
    numbers = [u["number"] for u in payload["units"]]
    return {u.number: u for u in db.session.scalars(db.select(Unit).where(Unit.number.in_(numbers)))}


@bp.route("/<int:batch_id>")
@admin_required
def preview(batch_id):
    batch = _batch(batch_id)
    payload = batch.payload
    existing = _existing_units(payload)
    openings = set(db.session.scalars(
        db.select(Unit.number).join(Charge).where(Charge.kind == "opening", Charge.period == payload["month"])
    ))
    totals = {
        "share": sum(u["share"] or 0 for u in payload["units"]),
        "fee": sum(u["fee_cents"] for u in payload["units"]),
        "owed": sum(u["balance_cents"] for u in payload["units"] if u["balance_cents"] > 0),
        "credit": sum(u["balance_cents"] for u in payload["units"] if u["balance_cents"] < 0),
        "interest": sum(u["interest_cents"] for u in payload["units"]),
    }
    return render_template("import/preview.html", batch=batch, payload=payload, existing=existing,
                           openings=openings, totals=totals, month_label=month_label(payload["month"]),
                           month_end=month_end(payload["month"]))


def _mark_loaded(batch, step, summary):
    payload = copy.deepcopy(batch.payload)
    payload["loaded"][step] = {"at": utcnow().isoformat(timespec="minutes"), "by": current_user.name,
                               "summary": summary}
    batch.payload = payload  # reassign so SQLAlchemy sees the JSON change


@bp.route("/<int:batch_id>/load/<step>", methods=["POST"])
@admin_required
def load(batch_id, step):
    if step not in STEPS:
        abort(404)
    batch = _batch(batch_id)
    payload = batch.payload
    if payload["loaded"].get(step):
        flash("That dataset was already loaded from this upload.", "info")
        return redirect(url_for("importer.preview", batch_id=batch.id))
    existing = _existing_units(payload)

    if step == "units":
        created = updated = 0
        for row in payload["units"]:
            unit = existing.get(row["number"])
            if unit is None:
                unit = Unit(number=row["number"])
                db.session.add(unit)
                created += 1
            else:
                updated += 1
            unit.monthly_fee_cents = row["fee_cents"]
            unit.ownership_share = row["share"]
        summary = f"{created} unit(s) created, {updated} updated."

    else:
        missing = [u["number"] for u in payload["units"] if u["number"] not in existing]
        if missing:
            flash(f"Load the units first — missing: {', '.join(missing)}.", "error")
            return redirect(url_for("importer.preview", batch_id=batch.id))

        if step == "owners":
            changed = 0
            for row in payload["units"]:
                name = request.form.get(f"owner_{row['number']}", row["owner"]).strip()[:200] or None
                unit = existing[row["number"]]
                if unit.owner_name != name:
                    unit.owner_name = name
                    changed += 1
            summary = f"Owner names set on {changed} unit(s)."

        else:  # balances
            period = payload["month"]
            due = month_end(period)
            already = set(db.session.scalars(
                db.select(Charge.unit_id).where(Charge.kind == "opening", Charge.period == period)))
            added = skipped = 0
            for row in payload["units"]:
                unit = existing[row["number"]]
                if row["balance_cents"] == 0 or unit.id in already:
                    skipped += 1
                    continue
                note = f", incl. {money(row['interest_cents'])} late interest" if row["interest_cents"] else ""
                kind = "owed" if row["balance_cents"] > 0 else "credit"
                db.session.add(Charge(
                    unit=unit, kind="opening", period=period, due_date=due, amount_cents=row["balance_cents"],
                    description=f"Opening balance ({kind}) from spreadsheet, end of {month_label(period)}{note}"))
                added += 1
            summary = f"Opening balances added to {added} unit(s); {skipped} skipped (zero or already loaded)."

    _mark_loaded(batch, step, summary)
    db.session.commit()
    flash(summary, "success")
    return redirect(url_for("importer.preview", batch_id=batch.id))
