import os
from datetime import timedelta
from flask import Flask
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

from .models import db

oauth = OAuth()
csrf = CSRFProtect()


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    # Railway 등 리버스 프록시 뒤에서 url_for(_external=True)가 https 스킴을 생성하도록 교정
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # SECRET_KEY는 반드시 환경변수로 설정해야 함.
    # 폴백을 두면 multi-worker gunicorn 환경에서 워커마다 키가 달라져 세션·CSRF·재설정 토큰이 깨진다.
    secret = os.environ.get("FLASK_SECRET_KEY")
    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY 환경변수가 설정되어 있지 않습니다.")
    app.config["SECRET_KEY"] = secret

    # 세션 쿠키 보안 옵션. 로컬 개발(HTTP)에서만 SESSION_COOKIE_SECURE=false 환경변수로 끌 수 있게 함.
    app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

    # CSRF 토큰 만료를 세션 쿠키 수명(30일)에 위임. 기본 1시간이면 메인 페이지를
    # 오래 열어둔 유저가 "분석" 클릭 시 400 에러로 이탈하는 문제가 생긴다.
    app.config["WTF_CSRF_TIME_LIMIT"] = None

    # OAuth 클라이언트 설정 — authlib이 Flask config에서 자동 로드
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

    # PostHog (퍼널 분석) — 키가 비어 있으면 base.html이 스니펫을 렌더하지 않아 자동 비활성.
    # 로컬/스테이징에서 분석 데이터 섞이지 않게 운영 환경에만 키를 넣는 운영 가정.
    posthog_key  = os.environ.get("POSTHOG_PROJECT_KEY", "")
    posthog_host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")

    @app.context_processor
    def inject_analytics():
        return {"posthog_key": posthog_key, "posthog_host": posthog_host}

    from .routes import bp
    app.register_blueprint(bp)

    from .chatbot import bp as chatbot_bp
    app.register_blueprint(chatbot_bp)
    # 챗봇 fetch는 same-origin이지만 JSON body라 hidden input으로 토큰을 못 싣는다.
    # 우선 챗봇 블루프린트는 CSRF에서 제외 (visitor_id + 일일 한도로 abuse 보호).
    csrf.exempt(chatbot_bp)

    # Celery 인스턴스에 Flask app_context 주입 — task 등록은 celery_app의 include로 처리
    from .celery_app import init_celery_for_flask
    init_celery_for_flask(app)

    return app
