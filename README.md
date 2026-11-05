# Condo Management

A web app for running a condominium. Managers use it to look after units, residents, billing and maintenance. Owners and tenants use it to check their account, report problems, read notices and book amenities.

Built with Flask, SQLAlchemy and SQLite. It renders pages on the server and needs no JavaScript build step.

## Features

| Area | Managers | Owners / tenants |
|---|---|---|
| **Dashboard** | Occupancy, outstanding dues, units that owe money, active maintenance, upcoming bookings | Their unit's balance, their requests and bookings, latest notices |
| **Units** | Create, edit and delete units (floor, bedrooms, area, parking, monthly dues) | — |
| **People** | Manage accounts and roles, assign them to units, deactivate them, search | Edit their phone number and password |
| **Billing** | Bill monthly dues for every unit in one step (running it twice for the same month bills nobody twice), add special assessments or fines, record payments, see a statement with a running balance | Read their own unit's statement |
| **Maintenance** | Triage requests by status and priority, comment on them | Submit requests for their unit or a common area, comment, close or reopen their own |
| **Notices** | Post, pin, edit and delete notices | Read them |
| **Amenities** | Set up amenities with opening hours and a maximum booking length | Book time slots (overlaps, past times, out-of-hours and over-long bookings are refused) and cancel their own |

Money is stored in integer cents. Every form has CSRF protection, and after login the app only redirects to addresses on its own site.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Load demo data (16 units, residents, dues, requests, bookings)
.venv/bin/flask --app condo seed
.venv/bin/flask --app condo run
```

Then open http://127.0.0.1:5000 and sign in with a demo account:

- Manager: `admin@example.com` / `admin1234`
- Resident: `alice@example.com` / `resident123`

### Starting with an empty database

```bash
.venv/bin/flask --app condo init-db
.venv/bin/flask --app condo create-admin
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | `dev-change-me` | Session signing key. **Set this in production.** |
| `DATABASE_URL` | `sqlite:///instance/condo.db` | Any SQLAlchemy database URL |
| `CONDO_NAME` | `Maple Court Condominiums` | Name shown in the header |

## Tests

```bash
.venv/bin/pytest
```

## Project layout

```
condo/
  __init__.py      app factory, error handlers
  models.py        User, Unit, Charge, Payment, MaintenanceRequest, Announcement, Amenity, Booking
  cli.py           init-db, create-admin, seed
  views/           one blueprint per area
  templates/       Jinja templates
  static/style.css
tests/
```
