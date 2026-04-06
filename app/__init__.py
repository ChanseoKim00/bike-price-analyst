import os
from flask import Flask
from .models import db


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    from .routes import bp
    app.register_blueprint(bp)

    return app
