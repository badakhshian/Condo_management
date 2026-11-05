import os

from flask import Flask

from .extensions import csrf, db, login_manager


def _database_url(instance_path):
    """Pick the database URL.

    Uses DATABASE_URL (or POSTGRES_URL, which the Vercel/Neon integration sets).
    Without one, falls back to a local SQLite file; on Vercel, where the code
    directory is read-only, that file lives in /tmp and is NOT persistent.
    """
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if url:
        # SQLAlchemy needs an explicit driver for psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url
    folder = "/tmp" if os.environ.get("VERCEL") else instance_path
    return "sqlite:///" + os.path.join(folder, "condo.db")


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass  # read-only filesystem (e.g. serverless)

    on_vercel = bool(os.environ.get("VERCEL"))
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
        SQLALCHEMY_DATABASE_URI=_database_url(app.instance_path),
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        CONDO_NAME=os.environ.get("CONDO_NAME", "Maple Court Condominiums"),
        SESSION_COOKIE_SECURE=on_vercel,
        REMEMBER_COOKIE_SECURE=on_vercel,
        AUTO_INIT_DB=on_vercel or bool(os.environ.get("AUTO_INIT_DB")),
    )
    if test_config:
        app.config.update(test_config)
    app.config["EPHEMERAL_DB"] = on_vercel and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from . import models  # noqa: F401  (register models)
    from .views import amenities, announcements, auth, billing, dashboard, maintenance, residents, units

    for bp in (auth.bp, dashboard.bp, units.bp, residents.bp, billing.bp,
               maintenance.bp, announcements.bp, amenities.bp):
        app.register_blueprint(bp)

    from .cli import register_cli
    register_cli(app)

    from .utils import money
    app.jinja_env.filters["money"] = money

    @app.context_processor
    def inject_globals():
        return {"condo_name": app.config["CONDO_NAME"], "ephemeral_db": app.config["EPHEMERAL_DB"],
                "STATUS_LABELS": models.REQUEST_STATUSES, "PRIORITY_LABELS": models.REQUEST_PRIORITIES}

    from flask import render_template

    messages = {400: "That request could not be processed.",
                403: "You don't have permission to view this page.",
                404: "We couldn't find that page."}

    def handle_error(err):
        code = getattr(err, "code", 500)
        return render_template("error.html", code=code,
                               message=messages.get(code, "Something went wrong.")), code

    for code in messages:
        app.register_error_handler(code, handle_error)

    if app.config["AUTO_INIT_DB"]:
        from .cli import bootstrap
        with app.app_context():
            bootstrap()

    return app
