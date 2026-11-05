import os

from flask import Flask

from .extensions import csrf, db, login_manager


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///" + os.path.join(app.instance_path, "condo.db")
        ),
        CONDO_NAME=os.environ.get("CONDO_NAME", "Maple Court Condominiums"),
    )
    if test_config:
        app.config.update(test_config)

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
        return {"condo_name": app.config["CONDO_NAME"],
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

    return app
