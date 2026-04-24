import functools
import hashlib
import logging
import re
import secrets
from datetime import datetime, date, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from flask import Blueprint, current_app, jsonify, render_template, request, session, redirect, url_for, make_response
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

from .models import db, Bike, Analysis, User, UserAnalysis, PriceSuggestion, AnalysisLog, BikePriceHistory, PartPriceHistory, Part, PasswordResetToken, UserFeedback
from .email_sender import send_password_reset_email
from .utils.nickname import generate_unique_nickname
from .price_calculator import _normalize_part_name

bp = Blueprint("main", __name__)


def record_bike_price_history(
    bike: Bike,
    new_price: int | None,
    recorded_at: datetime | None = None,
    force: bool = False,
) -> bool:
    """
    완성차 가격이 신규 저장되거나 변경될 때 bike_price_history에 row 추가.
    동일 가격이면 저장하지 않음. None 가격은 기록 대상 아님.
    세션 flush/commit은 호출자가 책임진다.

    Args:
        force: True면 직전 row와 가격이 같아도 새 row를 추가. 워커 자연 발화
               시 "변동 없음" 확인 도장을 그래프에 남기기 위한 용도.
    """
    if new_price is None:
        return False

    if not force:
        last = (
            BikePriceHistory.query
            .filter_by(bike_id=bike.id)
            .order_by(BikePriceHistory.recorded_at.desc())
            .first()
        )
        if last and last.price_krw == new_price:
            return False

    db.session.add(BikePriceHistory(
        bike_id=bike.id,
        price_krw=new_price,
        recorded_at=recorded_at or datetime.utcnow(),
    ))
    return True


_HISTORY_DAYS = 365 * 3


def _serialize_history(rows) -> list[dict]:
    return [{"x": r.recorded_at.isoformat(), "y": r.price_krw} for r in rows]


def build_price_history(bike: Bike, parts: dict) -> dict:
    """
    world_tour 플랜 전용 — 최근 3년 가격 이력 조회.
    bike / frameset / groupset / wheelset 순서로 반환.
    데이터가 없는 부품은 빈 리스트.
    """
    cutoff = datetime.utcnow() - timedelta(days=_HISTORY_DAYS)

    bike_rows = (
        BikePriceHistory.query
        .filter(BikePriceHistory.bike_id == bike.id,
                BikePriceHistory.recorded_at >= cutoff)
        .order_by(BikePriceHistory.recorded_at.asc())
        .all()
    )

    def _part_history(part):
        if part is None:
            return []
        rows = (
            PartPriceHistory.query
            .filter(PartPriceHistory.part_id == part.id,
                    PartPriceHistory.recorded_at >= cutoff)
            .order_by(PartPriceHistory.recorded_at.asc())
            .all()
        )
        return _serialize_history(rows)

    return {
        "bike":     _serialize_history(bike_rows),
        "frameset": _part_history(parts.get("frameset")),
        "groupset": _part_history(parts.get("groupset")),
        "wheelset": _part_history(parts.get("wheelset")),
    }

def _err(message, hint, url="", **kwargs):
    """에러 페이지 렌더링 헬퍼"""
    return render_template("error.html", message=message, hint=hint, url=url, **kwargs)


# 플랜별 분석 횟수 제한
_WINDOW_HOURS = 5
_GUEST_LIMIT = 3
_CONTINENTAL_LIMIT = 10


def _get_client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


def _check_rate_limit(ip: str):
    """
    Returns (blocked, detail_limited, reset_minutes)
    - blocked=True      → 분석 자체 차단 (비로그인 5시간 3회 초과)
    - detail_limited=True → 분석은 되지만 부품가 블러 처리 (continental 10회 초과)
    - reset_minutes     → 차단된 경우 재이용 가능까지 남은 분
    """
    user_id = session.get("user_id")
    window_start = datetime.utcnow() - timedelta(hours=_WINDOW_HOURS)

    if user_id:
        user = db.session.get(User, user_id)
        plan = (user.plan if user else None) or "continental"

        if plan in ("pro", "world_tour") or (user and user.role == "admin"):
            return False, False, 0

        # continental: 5시간 10회, 초과 시 부품가 블러
        count = AnalysisLog.query.filter(
            AnalysisLog.user_id == user_id,
            AnalysisLog.is_detailed == True,
            AnalysisLog.analyzed_at >= window_start,
        ).count()
        if count >= _CONTINENTAL_LIMIT:
            oldest = AnalysisLog.query.filter(
                AnalysisLog.user_id == user_id,
                AnalysisLog.is_detailed == True,
                AnalysisLog.analyzed_at >= window_start,
            ).order_by(AnalysisLog.analyzed_at.asc()).first()
            if oldest:
                reset_at = oldest.analyzed_at + timedelta(hours=_WINDOW_HOURS)
                reset_minutes = max(1, int((reset_at - datetime.utcnow()).total_seconds() / 60) + 1)
            else:
                reset_minutes = _WINDOW_HOURS * 60
            return False, True, reset_minutes
        return False, False, 0

    # 비로그인 유저만 IP 기준 5시간 윈도우 적용
    # user_id IS NULL 조건을 걸어야 같은 IP에서 로그인한 사용자의 기록이 비로그인 카운트에 섞이지 않음
    count = AnalysisLog.query.filter(
        AnalysisLog.ip_address == ip,
        AnalysisLog.user_id.is_(None),
        AnalysisLog.analyzed_at >= window_start,
    ).count()

    if count >= _GUEST_LIMIT:
        oldest = AnalysisLog.query.filter(
            AnalysisLog.ip_address == ip,
            AnalysisLog.user_id.is_(None),
            AnalysisLog.analyzed_at >= window_start,
        ).order_by(AnalysisLog.analyzed_at.asc()).first()
        if oldest:
            reset_at = oldest.analyzed_at + timedelta(hours=_WINDOW_HOURS)
            reset_minutes = max(1, int((reset_at - datetime.utcnow()).total_seconds() / 60) + 1)
        else:
            reset_minutes = _WINDOW_HOURS * 60
        return True, False, reset_minutes

    return False, False, 0


@bp.route("/")
def index():
    return render_template("index.html", price_history=None)


@bp.route("/pricing")
def pricing():
    return render_template("pricing.html")


@bp.route("/preview/result")
def preview_result():
    bike = SimpleNamespace(brand="Fantasia", model_name="레이다 9 ARC Gen.3", model_year=2025)
    parts = {
        "groupset":  SimpleNamespace(part_name="시마노 울테그라 Di2 R8150", part_type="groupset",  price_krw=2_300_000),
        "wheelset":  SimpleNamespace(part_name="디티스위스 ARC 1100 DICUT DB 55", part_type="wheelset", price_krw=4_750_000),
        "frameset":  None,
        "saddle":    SimpleNamespace(part_name="셀레이탈리아 노부스 부스트 EVO", part_type="saddle", price_krw=None),
        "handlebar": SimpleNamespace(part_name="컨트롤텍 시로코 FL4", part_type="handlebar", price_krw=None),
    }
    analysis = SimpleNamespace(
        parts_sum_krw=7_050_000,
        saving_krw=228_000,
        saving_pct=3.2,
        missing_parts=["frameset", "saddle", "handlebar"],
    )
    return render_template("index.html", bike=bike, parts=parts, analysis=analysis, bike_price=6_822_000, price_history=None)


@bp.route("/preview/error")
def preview_error():
    return render_template("error.html", url="")


_SUGGEST_PARTS = [
    ("groupset",  "구동계"),
    ("wheelset",  "휠셋"),
    ("frameset",  "프레임셋"),
    ("saddle",    "안장"),
    ("handlebar", "핸들바"),
]

# bikes 테이블에 FK가 있는 부품 키 (나머지는 항상 None)
_BIKE_FK_PARTS = {"groupset", "wheelset", "saddle"}


@bp.route("/suggest", methods=["GET", "POST"])
def suggest():
    analysis_id = (request.args.get("analysis_id") or request.form.get("analysis_id", "")).strip()
    if not analysis_id:
        return redirect(url_for("main.index"))

    analysis = Analysis.query.filter_by(id=analysis_id).first()
    if not analysis:
        return redirect(url_for("main.index"))

    bike = analysis.bike
    # FK가 있는 부품은 실제 Part 객체, 없는 부품(frameset/handlebar)은 None으로 항상 5개 표시
    parts = [
        (key, label, getattr(bike, key) if key in _BIKE_FK_PARTS else None)
        for key, label in _SUGGEST_PARTS
    ]

    if request.method == "GET":
        return render_template("suggest.html", analysis=analysis, bike=bike,
                               parts=parts, errors={}, form_prices={}, form_urls={})

    # POST — 유효성 검증 및 저장
    suggestions  = {}
    errors       = {}
    form_prices  = {}
    form_urls    = {}

    for key, label, part in parts:
        price_raw  = request.form.get(f"price_{key}", "").strip()
        source_url = request.form.get(f"url_{key}", "").strip() or None

        suggested_price = None
        if price_raw:
            try:
                suggested_price = int(price_raw.replace(",", "").replace(" ", ""))
                if suggested_price <= 0:
                    suggested_price = None
            except ValueError:
                errors[key] = "올바른 숫자를 입력해주세요."

        if source_url and suggested_price is None and key not in errors:
            errors[key] = "가격을 입력해주세요."

        suggestions[key] = {"suggested_price_krw": suggested_price, "source_url": source_url}
        form_prices[key] = price_raw
        form_urls[key]   = source_url or ""

    if errors:
        return render_template("suggest.html", analysis=analysis, bike=bike,
                               parts=parts, errors=errors,
                               form_prices=form_prices, form_urls=form_urls)

    ps = PriceSuggestion(
        analysis_id=analysis.id,
        user_id=session.get("user_id") or None,
        suggestions=suggestions,
    )
    db.session.add(ps)
    db.session.commit()
    return redirect(url_for("main.suggest_complete"))


@bp.route("/suggest/complete")
def suggest_complete():
    return render_template("suggest_complete.html")


# ── 유저 피드백 ────────────────────────────────────────────────

_FEEDBACK_TEXT_MAX = 2000


@bp.route("/feedback", methods=["GET", "POST"])
def feedback():
    if request.method == "GET":
        return render_template("feedback.html", form={}, errors={})

    rating_raw     = request.form.get("rating", "").strip()
    pain_point     = request.form.get("pain_point", "").strip()
    good_point     = request.form.get("good_point", "").strip()
    message_to_dev = request.form.get("message_to_dev", "").strip()

    form = {
        "rating":         rating_raw,
        "pain_point":     pain_point,
        "good_point":     good_point,
        "message_to_dev": message_to_dev,
    }
    errors = {}

    rating = None
    if not rating_raw:
        errors["rating"] = "만족도를 선택해주세요."
    else:
        try:
            rating = int(rating_raw)
            if rating < 1 or rating > 10:
                errors["rating"] = "1~10 사이의 점수를 선택해주세요."
        except ValueError:
            errors["rating"] = "올바른 점수를 선택해주세요."

    for key, label in (("pain_point", "불편한 점"),
                       ("good_point", "좋은 점"),
                       ("message_to_dev", "개발자에게 하고 싶은 말")):
        if len(form[key]) > _FEEDBACK_TEXT_MAX:
            errors[key] = f"{label}은 {_FEEDBACK_TEXT_MAX}자 이내로 입력해주세요."

    if errors:
        return render_template("feedback.html", form=form, errors=errors)

    fb = UserFeedback(
        user_id=session.get("user_id") or None,
        rating=rating,
        pain_point=pain_point or None,
        good_point=good_point or None,
        message_to_dev=message_to_dev or None,
    )
    db.session.add(fb)
    db.session.commit()
    return redirect(url_for("main.feedback_complete"))


@bp.route("/feedback/complete")
def feedback_complete():
    return render_template("feedback_complete.html")


# ── 인증 ──────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _form_ctx(**kwargs):
    """회원가입 폼 재출력 시 입력값 유지용 헬퍼"""
    return kwargs


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    email      = request.form.get("email", "").strip().lower()
    password   = request.form.get("password", "")
    name       = request.form.get("name", "").strip()
    nickname   = request.form.get("nickname", "").strip()
    birth_date = request.form.get("birth_date", "").strip()
    privacy    = request.form.get("privacy_agreed")

    ctx = dict(email=email, name=name, nickname=nickname, birth_date=birth_date)

    if not _EMAIL_RE.match(email):
        return render_template("register.html", error="올바른 이메일 형식을 입력해주세요.", **ctx)
    if len(password) < 8:
        return render_template("register.html", error="비밀번호는 최소 8자 이상이어야 합니다.", **ctx)
    if not name:
        return render_template("register.html", error="이름을 입력해주세요.", **ctx)
    if not nickname:
        return render_template("register.html", error="닉네임을 입력해주세요.", **ctx)
    if not birth_date:
        return render_template("register.html", error="생년월일을 입력해주세요.", **ctx)
    if not privacy:
        return render_template("register.html", error="개인정보 수집·이용에 동의해주세요.", **ctx)

    try:
        birth_date_parsed = datetime.strptime(birth_date, "%Y-%m-%d").date()
    except ValueError:
        return render_template("register.html", error="생년월일 형식이 올바르지 않습니다.", **ctx)

    if User.query.filter_by(email=email).first():
        return render_template("register.html", error="이미 사용 중인 이메일입니다.", **ctx)
    if User.query.filter_by(nickname=nickname).first():
        return render_template("register.html", error="이미 사용 중인 닉네임입니다.", **ctx)

    user = User(
        email=email,
        password_hash=generate_password_hash(password),
        name=name,
        nickname=nickname,
        birth_date=birth_date_parsed,
        privacy_agreed_at=datetime.utcnow(),
        provider="local",
    )
    db.session.add(user)
    db.session.commit()
    return redirect(url_for("main.login") + "?registered=1")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        registered  = request.args.get("registered")
        reset       = request.args.get("reset")
        oauth_error = request.args.get("oauth_error")
        return render_template("login.html",
                               registered=registered, reset=reset, oauth_error=oauth_error)

    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not _EMAIL_RE.match(email) or len(password) < 8:
        return render_template("login.html", error="이메일 또는 비밀번호를 확인해주세요.", email=email)

    user = User.query.filter_by(email=email).first()
    if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
        return render_template("login.html", error="이메일 또는 비밀번호가 올바르지 않습니다.", email=email)

    user.last_login_at = datetime.utcnow()
    db.session.commit()

    _login_user_session(user)
    return redirect(url_for("main.index"))


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.index"))


# ── Google OAuth 로그인 ───────────────────────────────────────

def _login_user_session(user: User):
    """로그인 세션 값 채우기 — /login, OAuth 콜백 공용."""
    session["user_id"]       = str(user.id)
    session["user_email"]    = user.email
    session["user_nickname"] = user.nickname
    session["user_role"]     = user.role
    session["user_plan"]     = user.plan


@bp.route("/auth/google/login")
def google_login():
    from . import oauth
    redirect_uri = url_for("main.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route("/auth/google/callback")
def google_callback():
    from . import oauth
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        logger.warning("Google OAuth 토큰 교환 실패: %s", e)
        return redirect(url_for("main.login") + "?oauth_error=1")

    userinfo = token.get("userinfo")
    if not userinfo:
        try:
            userinfo = oauth.google.userinfo(token=token)
        except Exception as e:
            logger.error("Google userinfo 조회 실패: %s", e)
            return redirect(url_for("main.login") + "?oauth_error=1")

    sub   = userinfo.get("sub")
    email = (userinfo.get("email") or "").strip().lower()
    name  = userinfo.get("name") or None

    if not sub or not email:
        logger.warning("Google userinfo 필수값 누락: sub=%s email=%s", sub, email)
        return redirect(url_for("main.login") + "?oauth_error=1")

    # 1) provider + sub 매칭 → 재로그인
    user = User.query.filter_by(provider="google", provider_user_id=sub).first()

    # 2) email 매칭 → 기존 로컬 계정 자동 연결
    if not user:
        user = User.query.filter_by(email=email).first()
        if user:
            user.provider = "google"
            user.provider_user_id = sub

    # 3) 신규 가입
    if not user:
        user = User(
            email=email,
            password_hash=None,
            name=name,
            nickname=generate_unique_nickname(),
            birth_date=None,
            privacy_agreed_at=datetime.utcnow(),
            provider="google",
            provider_user_id=sub,
        )
        db.session.add(user)

    user.last_login_at = datetime.utcnow()
    db.session.commit()

    _login_user_session(user)
    return redirect(url_for("main.index"))


# ── 비밀번호 재설정 ────────────────────────────────────────────

_RESET_TTL_MINUTES   = 30
_RESET_RATE_WINDOW_H = 1     # 1시간
_RESET_RATE_LIMIT    = 3     # 같은 계정당 윈도우 내 최대 요청 수


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    email = request.form.get("email", "").strip().lower()
    if not _EMAIL_RE.match(email):
        return render_template("forgot_password.html",
                               error="올바른 이메일 형식을 입력해주세요.", email=email)

    # User enumeration 방지 — 가입 여부와 무관하게 동일 응답
    user = User.query.filter_by(email=email).first()

    if user:
        # Rate limit — 같은 계정당 1시간 내 3회
        window_start = datetime.utcnow() - timedelta(hours=_RESET_RATE_WINDOW_H)
        recent_count = (
            PasswordResetToken.query
            .filter(PasswordResetToken.user_id == user.id,
                    PasswordResetToken.created_at >= window_start)
            .count()
        )
        if recent_count >= _RESET_RATE_LIMIT:
            logger.warning("비밀번호 재설정 rate limit — user=%s count=%d", user.email, recent_count)
        else:
            raw_token = secrets.token_urlsafe(32)
            prt = PasswordResetToken(
                user_id=user.id,
                token_hash=_hash_token(raw_token),
                expires_at=datetime.utcnow() + timedelta(minutes=_RESET_TTL_MINUTES),
            )
            db.session.add(prt)
            db.session.commit()

            reset_url = url_for("main.reset_password", token=raw_token, _external=True)
            send_password_reset_email(user.email, reset_url)

    return render_template("forgot_password_sent.html", email=email)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    prt = PasswordResetToken.query.filter_by(token_hash=_hash_token(token)).first()

    if not prt or prt.used_at is not None or prt.expires_at < datetime.utcnow():
        return render_template("reset_password.html", invalid=True)

    if request.method == "GET":
        return render_template("reset_password.html", token=token)

    password = request.form.get("password", "")
    confirm  = request.form.get("password_confirm", "")

    if len(password) < 8:
        return render_template("reset_password.html", token=token,
                               error="비밀번호는 최소 8자 이상이어야 합니다.")
    if password != confirm:
        return render_template("reset_password.html", token=token,
                               error="비밀번호가 일치하지 않습니다.")

    user = db.session.get(User, prt.user_id)
    if not user:
        return render_template("reset_password.html", invalid=True)

    user.password_hash = generate_password_hash(password)
    prt.used_at = datetime.utcnow()

    # 같은 사용자의 다른 미사용 토큰도 모두 무효화
    (PasswordResetToken.query
        .filter(PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.id != prt.id)
        .update({"used_at": datetime.utcnow()}, synchronize_session=False))

    db.session.commit()
    return redirect(url_for("main.login") + "?reset=1")


def admin_required(f):
    """role='admin'인 로그인 사용자만 허용. 그 외는 메인으로 리다이렉트."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("user_role") != "admin":
            return redirect(url_for("main.index"))
        return f(*args, **kwargs)
    return wrapper


def login_required(f):
    """로그인 사용자만 허용. 비로그인은 /login으로 리다이렉트."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("main.login"))
        return f(*args, **kwargs)
    return wrapper


_MYPAGE_TABS = {"general", "account", "billing", "usage", "history"}


def _current_user_or_logout():
    """세션의 user_id로 User 로드. 없으면 세션 클리어 후 None."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = db.session.get(User, user_id)
    if not user:
        session.clear()
    return user


def _load_history_items(user_id):
    rows = (
        db.session.query(UserAnalysis, Analysis, Bike)
        .join(Analysis, Analysis.id == UserAnalysis.analysis_id)
        .join(Bike,     Bike.id     == Analysis.bike_id)
        .filter(UserAnalysis.user_id == user_id)
        .order_by(UserAnalysis.viewed_at.desc())
        .all()
    )
    return [
        {
            "brand":      bike.brand,
            "model_name": bike.model_name,
            "model_year": bike.model_year,
            "saving_krw": analysis.saving_krw,
            "saving_pct": analysis.saving_pct,
            "viewed_at":  ua.viewed_at,
        }
        for ua, analysis, bike in rows
    ]


def _mypage_render(user, tab, messages=None):
    if tab not in _MYPAGE_TABS:
        tab = "general"
    history_items = _load_history_items(user.id) if tab == "history" else None
    return render_template(
        "mypage.html",
        user=user,
        tab=tab,
        messages=messages or {},
        history_items=history_items,
    )


@bp.route("/mypage")
@login_required
def mypage():
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))
    tab = request.args.get("tab", "general")
    messages = {}
    if request.args.get("saved") == "1":
        messages["success"] = "변경사항이 저장되었습니다."
    return _mypage_render(user, tab, messages)


@bp.route("/mypage/profile", methods=["POST"])
@login_required
def mypage_profile():
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))
    user.notifications_enabled = bool(request.form.get("notifications_enabled"))
    db.session.commit()
    return redirect(url_for("main.mypage", tab="general", saved=1))


@bp.route("/mypage/account/nickname", methods=["POST"])
@login_required
def mypage_account_nickname():
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))
    new_nick = request.form.get("nickname", "").strip()
    if not new_nick:
        return _mypage_render(user, "account", {"error": "닉네임을 입력해주세요."})
    if len(new_nick) > 30:
        return _mypage_render(user, "account", {"error": "닉네임은 30자 이하로 입력해주세요."})
    if new_nick == user.nickname:
        return redirect(url_for("main.mypage", tab="account"))
    if User.query.filter(User.nickname == new_nick, User.id != user.id).first():
        return _mypage_render(user, "account", {"error": "이미 사용 중인 닉네임입니다."})
    user.nickname = new_nick
    db.session.commit()
    session["user_nickname"] = new_nick
    return redirect(url_for("main.mypage", tab="account", saved=1))


@bp.route("/mypage/account/email", methods=["POST"])
@login_required
def mypage_account_email():
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))
    if user.provider == "google":
        return _mypage_render(user, "account", {"error": "Google 연동 계정은 이메일을 변경할 수 없습니다."})
    new_email = request.form.get("email", "").strip().lower()
    if not _EMAIL_RE.match(new_email):
        return _mypage_render(user, "account", {"error": "올바른 이메일 형식을 입력해주세요."})
    if new_email == user.email:
        return redirect(url_for("main.mypage", tab="account"))
    if User.query.filter(User.email == new_email, User.id != user.id).first():
        return _mypage_render(user, "account", {"error": "이미 사용 중인 이메일입니다."})
    user.email = new_email
    db.session.commit()
    session["user_email"] = new_email
    return redirect(url_for("main.mypage", tab="account", saved=1))


@bp.route("/mypage/account/password", methods=["POST"])
@login_required
def mypage_account_password():
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))
    if not user.password_hash:
        return _mypage_render(user, "account", {"error": "소셜 로그인 계정은 비밀번호를 설정할 수 없습니다."})
    current = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm = request.form.get("new_password_confirm", "")
    if not check_password_hash(user.password_hash, current):
        return _mypage_render(user, "account", {"error": "현재 비밀번호가 올바르지 않습니다."})
    if len(new_password) < 8:
        return _mypage_render(user, "account", {"error": "새 비밀번호는 최소 8자 이상이어야 합니다."})
    if new_password != confirm:
        return _mypage_render(user, "account", {"error": "새 비밀번호와 확인이 일치하지 않습니다."})
    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return redirect(url_for("main.mypage", tab="account", saved=1))


@bp.route("/admin")
@admin_required
def admin():
    total_users    = User.query.count()
    total_analyses = Analysis.query.count()

    recent_analyses = (
        db.session.query(Analysis, Bike)
        .join(Bike, Bike.id == Analysis.bike_id)
        .order_by(Analysis.analyzed_at.desc())
        .limit(10)
        .all()
    )
    recent = [
        {
            "bike_name":   f"{bike.brand} {bike.model_name}" + (f" ({bike.model_year})" if bike.model_year else ""),
            "analyzed_at": analysis.analyzed_at,
            "saving_krw":  analysis.saving_krw,
        }
        for analysis, bike in recent_analyses
    ]

    users = (
        User.query
        .order_by(User.created_at.desc())
        .all()
    )

    pending_rows = (
        db.session.query(PriceSuggestion, Analysis, Bike)
        .join(Analysis, Analysis.id == PriceSuggestion.analysis_id)
        .join(Bike,     Bike.id     == Analysis.bike_id)
        .filter(PriceSuggestion.status == "pending")
        .order_by(PriceSuggestion.created_at.desc())
        .all()
    )
    pending = [
        {
            "id":         str(ps.id),
            "bike_name":  f"{bike.brand} {bike.model_name}" + (f" ({bike.model_year})" if bike.model_year else ""),
            "created_at": ps.created_at,
            "status":     ps.status,
        }
        for ps, analysis, bike in pending_rows
    ]

    feedback_rows = (
        db.session.query(UserFeedback, User)
        .outerjoin(User, User.id == UserFeedback.user_id)
        .order_by(UserFeedback.created_at.desc())
        .all()
    )
    feedbacks = [
        {
            "id":         str(fb.id),
            "nickname":   (u.nickname if u else "비회원"),
            "plan":       (u.plan if u else "-"),
            "created_at": fb.created_at,
            "rating":     fb.rating,
        }
        for fb, u in feedback_rows
    ]

    return render_template(
        "admin.html",
        total_users=total_users,
        total_analyses=total_analyses,
        recent=recent,
        users=users,
        pending=pending,
        feedbacks=feedbacks,
    )


@bp.route("/admin/suggestion/<suggestion_id>")
@admin_required
def admin_suggestion(suggestion_id):
    ps = PriceSuggestion.query.filter_by(id=suggestion_id).first()
    if not ps:
        return redirect(url_for("main.admin"))

    analysis = ps.analysis
    bike     = analysis.bike

    # 부품별 현재 정보 (FK가 없는 frameset/handlebar는 None)
    part_objects = {
        "groupset":  bike.groupset,
        "wheelset":  bike.wheelset,
        "saddle":    bike.saddle,
        "frameset":  None,
        "handlebar": None,
    }
    part_labels = {
        "groupset":  "구동계",
        "wheelset":  "휠셋",
        "frameset":  "프레임셋",
        "saddle":    "안장",
        "handlebar": "핸들바",
    }

    rows = []
    for key in ("groupset", "wheelset", "frameset", "saddle", "handlebar"):
        part      = part_objects[key]
        suggested = ps.suggestions.get(key, {}) or {}
        rows.append({
            "label":           part_labels[key],
            "part_name":       part.part_name if part else None,
            "current_price":   part.price_krw if part else None,
            "suggested_price": suggested.get("suggested_price_krw"),
            "source_url":      suggested.get("source_url"),
        })

    proposer = "비회원"
    if ps.user_id:
        u = User.query.filter_by(id=ps.user_id).first()
        if u:
            proposer = u.email

    bike_name = f"{bike.brand} {bike.model_name}" + (f" ({bike.model_year})" if bike.model_year else "")

    return render_template(
        "admin_suggestion.html",
        ps=ps,
        bike_name=bike_name,
        proposer=proposer,
        rows=rows,
    )


@bp.route("/admin/suggestion/<suggestion_id>/approve", methods=["POST"])
@admin_required
def admin_suggestion_approve(suggestion_id):
    ps = PriceSuggestion.query.filter_by(id=suggestion_id).first()
    if not ps or ps.status != "pending":
        return redirect(url_for("main.admin"))

    bike = ps.analysis.bike
    now = datetime.utcnow()

    fk_parts = {
        "groupset": bike.groupset,
        "wheelset": bike.wheelset,
        "saddle":   bike.saddle,
    }

    for key, suggested in (ps.suggestions or {}).items():
        if not suggested:
            continue
        new_price = suggested.get("suggested_price_krw")
        if not new_price:
            continue

        if key in fk_parts:
            part = fk_parts[key]
        elif key == "frameset":
            part = Part.query.filter_by(
                part_name_normalized=_normalize_part_name(bike.model_name),
                part_type="frameset",
            ).first()
        else:
            # handlebar 등 Part로 저장되지 않는 항목은 건너뜀
            continue

        if part is None:
            continue

        part.price_krw = int(new_price)
        part.last_verified_at = now

    ps.status = "approved"
    db.session.commit()
    return redirect(url_for("main.admin"))


@bp.route("/admin/suggestion/<suggestion_id>/reject", methods=["POST"])
@admin_required
def admin_suggestion_reject(suggestion_id):
    ps = PriceSuggestion.query.filter_by(id=suggestion_id).first()
    if not ps or ps.status != "pending":
        return redirect(url_for("main.admin"))

    ps.status = "rejected"
    db.session.commit()
    return redirect(url_for("main.admin"))


@bp.route("/admin/feedback/<feedback_id>")
@admin_required
def admin_feedback(feedback_id):
    fb = UserFeedback.query.filter_by(id=feedback_id).first()
    if not fb:
        return redirect(url_for("main.admin"))

    user = fb.user
    return render_template(
        "admin_feedback.html",
        fb=fb,
        nickname=(user.nickname if user else "비회원"),
        plan=(user.plan if user else "-"),
        email=(user.email if user else None),
    )


@bp.route("/history")
def history():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    history_items = _load_history_items(session["user_id"])
    return render_template("history.html", history=history_items)


def _get_celery():
    """현재 Flask 앱의 celery 인스턴스."""
    return current_app.extensions["celery"]


@bp.route("/analyze", methods=["POST"])
def analyze():
    """URL·rate limit 검증 후 Celery task를 enqueue하고 로딩 페이지를 렌더링한다.

    실제 스크래핑·AI·DB 작업은 app.tasks.analyze_bike_task 가 별도 워커에서 수행.
    분석 결과는 /analyze/status/<task_id> 폴링으로 받아 /result/<analysis_id> 로 리다이렉트."""
    url = request.form.get("url", "").strip()
    if not url:
        return _err(
            "링크를 입력해주세요.",
            "분석할 자전거 판매 페이지 링크를 입력창에 붙여넣어 주세요.",
            url=url,
        )
    if len(url) > 2000:
        return _err(
            "올바르지 않은 링크입니다.",
            "주소창에서 링크를 다시 복사해 붙여넣어 주세요.",
        )
    if urlparse(url).scheme not in ("http", "https"):
        return _err(
            "지원하지 않는 링크 형식입니다.",
            "http:// 또는 https://로 시작하는 자전거 판매 페이지 링크를 입력해주세요.",
        )

    ip = _get_client_ip()
    blocked, detail_limited, reset_minutes = _check_rate_limit(ip)

    if blocked:
        return redirect(url_for("main.index", limit="true", reset_minutes=reset_minutes))

    print(f"[ANALYZE] 요청 URL: {url} | ip={ip} | detail_limited={detail_limited}")

    from .tasks import analyze_bike_task
    task = analyze_bike_task.delay(
        url=url,
        user_id=session.get("user_id"),
        ip=ip,
        is_detailed=not detail_limited,
    )
    print(f"[ANALYZE] task enqueued: id={task.id}")
    return render_template("loading.html", task_id=task.id)


@bp.route("/analyze/status/<task_id>")
def analyze_status(task_id):
    """Celery task 상태 폴링. JSON 반환 — 로딩 페이지 JS에서 사용."""
    celery = _get_celery()
    async_result = celery.AsyncResult(task_id)
    state = async_result.state

    # REVOKED: 로고 클릭으로 취소된 상태 — 클라이언트에선 이미 /로 이동했겠지만 혹시 살아있으면 정리.
    if state == "REVOKED":
        return jsonify({"state": "revoked"})

    if state in ("PENDING", "RECEIVED", "STARTED", "RETRY"):
        return jsonify({"state": "pending"})

    if state == "FAILURE":
        # task 함수 안에서 예외가 잡혀 error dict를 반환하는 구조이므로 여기 오는 건 task 자체의 크래시.
        logger.error("analyze task FAILURE | task_id=%s | %s", task_id, async_result.traceback)
        return jsonify({
            "state": "error",
            "message": "일시적인 오류가 발생했습니다.",
            "hint": "잠시 후 다시 시도해주세요.",
            "url": "",
        })

    if state == "SUCCESS":
        result = async_result.result or {}
        if result.get("status") == "success":
            return jsonify({"state": "success", "analysis_id": result["analysis_id"]})
        if result.get("status") == "error":
            return jsonify({
                "state": "error",
                "message": result.get("message", "오류가 발생했습니다."),
                "hint": result.get("hint", ""),
                "url": result.get("url", ""),
            })

    return jsonify({"state": "pending"})


@bp.route("/analyze/cancel/<task_id>", methods=["POST"])
def analyze_cancel(task_id):
    """task를 revoke — 워커 프로세스를 SIGTERM으로 종료해 즉시 중단.

    sendBeacon 지원을 위해 응답 본문 없이 204로 반환."""
    celery = _get_celery()
    celery.control.revoke(task_id, terminate=True, signal="SIGTERM")
    print(f"[CANCEL] task revoked: id={task_id}")
    return ("", 204)


@bp.route("/analyze/error")
def analyze_error():
    """loading.html이 task 실패를 감지한 뒤 이동하는 에러 렌더 엔드포인트."""
    return _err(
        message=request.args.get("message", "오류가 발생했습니다."),
        hint=request.args.get("hint", ""),
        url=request.args.get("url", ""),
    )


# 부품 키 — Analysis.parts_snapshot 재구성용. task.PART_KEYS와 동기화.
_RESULT_PART_KEYS = ["groupset", "wheelset", "frameset", "saddle", "handlebar"]
_BIKE_FK_PART_KEYS = {"groupset", "wheelset", "saddle"}


def _load_parts_for_result(analysis: Analysis) -> dict:
    """분석 결과 화면용 parts dict 재구성. parts_snapshot 우선, 없으면 bike FK + model_name 폴백."""
    bike = analysis.bike
    parts: dict = {key: None for key in _RESULT_PART_KEYS}

    snapshot = analysis.parts_snapshot or {}
    if snapshot:
        ids = [v for v in snapshot.values() if v]
        if ids:
            rows = Part.query.filter(Part.id.in_(ids)).all()
            by_id = {str(p.id): p for p in rows}
            for key in _RESULT_PART_KEYS:
                pid = snapshot.get(key)
                if pid:
                    parts[key] = by_id.get(str(pid))
        return parts

    # parts_snapshot이 없는 과거 분석 폴백
    parts["groupset"] = bike.groupset
    parts["wheelset"] = bike.wheelset
    parts["saddle"] = bike.saddle
    parts["frameset"] = (
        Part.query
        .filter_by(
            part_name_normalized=_normalize_part_name(bike.model_name),
            part_type="frameset",
        )
        .first()
    )
    # handlebar는 과거 데이터에서 복원 불가 → None
    return parts


@bp.route("/result/<analysis_id>")
def result(analysis_id):
    """분석 완료 후 task가 남긴 analysis_id로 결과 화면을 렌더. 폼 리프레시에도 안전."""
    analysis = Analysis.query.filter_by(id=analysis_id).first()
    if not analysis:
        return redirect(url_for("main.index"))

    bike = analysis.bike
    parts = _load_parts_for_result(analysis)

    bike_price = bike.price_krw or 0

    # blur 모드 — 현재 세션/IP 기준 rate limit으로 재도출
    ip = _get_client_ip()
    _blocked, detail_limited, reset_minutes = _check_rate_limit(ip)

    if not session.get("user_id"):
        blur_mode = "guest"
        blur_reset_minutes = 0
    elif detail_limited:
        blur_mode = "continental"
        blur_reset_minutes = reset_minutes
    else:
        blur_mode = None
        blur_reset_minutes = 0

    # world_tour / admin만 가격 이력 그래프
    price_history = None
    user_id = session.get("user_id")
    if user_id:
        user = db.session.get(User, user_id)
        if user and (user.plan == "world_tour" or user.role == "admin"):
            price_history = build_price_history(bike, parts)

    return render_template(
        "index.html",
        bike=bike,
        parts=parts,
        analysis=analysis,
        bike_price=bike_price,
        blur_mode=blur_mode,
        blur_reset_minutes=blur_reset_minutes,
        price_history=price_history,
    )
