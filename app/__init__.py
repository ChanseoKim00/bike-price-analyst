import os
from datetime import timedelta
from flask import Flask
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

from .models import db

oauth = OAuth()
csrf = CSRFProtect()


def _krw_to_usd(value, decimals: int = 0) -> str:
    """Convert a KRW integer/float into a comma-formatted USD string.

    Uses the cached BOK exchange rate (with a hardcoded fallback). Returns "0" for
    None / non-numeric input so Jinja never blows up while rendering."""
    try:
        krw = float(value)
    except (TypeError, ValueError):
        return "0"
    if krw == 0:
        return "0"
    try:
        from .exchange_rate import get_exchange_rates
        rate = get_exchange_rates().get("USD", 1470)
    except Exception:
        rate = 1470
    usd = krw / rate
    if decimals == 0:
        return f"{int(round(usd)):,}"
    return f"{usd:,.{decimals}f}"


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    # Behind a reverse proxy (Railway, etc.) so url_for(_external=True) emits https.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # SECRET_KEY must come from the environment. A hardcoded fallback would diverge
    # across gunicorn workers and break sessions / CSRF / reset tokens.
    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY environment variable is not set.")
    app.config["SECRET_KEY"] = secret

    # Session cookie hardening. Only flip SESSION_COOKIE_SECURE off for local HTTP dev.
    app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

    # Tie CSRF token lifetime to the 30-day session cookie. The default 1 hour caused
    # users with an old landing page open to hit a 400 when clicking "Analyze".
    app.config["WTF_CSRF_TIME_LIMIT"] = None

    # OAuth client config — authlib auto-loads from Flask config.
    app.config["GOOGLE_CLIENT_ID"]     = os.environ.get("GOOGLE_CLIENT_ID")
    app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET")

    db.init_app(app)
    csrf.init_app(app)
    oauth.init_app(app)
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

    # USD display filter — converts KRW values to USD using the BOK rate.
    app.jinja_env.filters["usd"] = _krw_to_usd

    # PostHog (funnel analytics). If the key is empty base.html skips the snippet,
    # which is how local/staging stays out of the production project.
    posthog_key  = os.environ.get("POSTHOG_PROJECT_KEY", "")
    posthog_host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")

    @app.context_processor
    def inject_analytics():
        return {"posthog_key": posthog_key, "posthog_host": posthog_host}

    from .routes import bp
    app.register_blueprint(bp)

    from .chatbot import bp as chatbot_bp
    app.register_blueprint(chatbot_bp)
    # Chatbot fetch is same-origin but uses a JSON body, so the hidden CSRF input
    # can't ride along. The blueprint relies on visitor_id + daily quota for abuse control.
    csrf.exempt(chatbot_bp)

    # Inject Flask app_context into Celery — task registration is handled via celery_app's include.
    from .celery_app import init_celery_for_flask
    init_celery_for_flask(app)

    return app
