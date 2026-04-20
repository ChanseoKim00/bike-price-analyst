import os
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

from .models import db

oauth = OAuth()


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    # Railway 등 리버스 프록시 뒤에서 url_for(_external=True)가 https 스킴을 생성하도록 교정
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

    # OAuth 클라이언트 설정 — authlib이 Flask config에서 자동 로드
    app.config["GOOGLE_CLIENT_ID"]     = os.environ.get("GOOGLE_CLIENT_ID")
    app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET")

    db.init_app(app)
    oauth.init_app(app)
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

    from .routes import bp
    app.register_blueprint(bp)

    from .chatbot import bp as chatbot_bp
    app.register_blueprint(chatbot_bp)

    return app
