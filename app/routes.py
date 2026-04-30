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
from sqlalchemy import func, or_, and_, cast, Date


def _admin_chart_since_utc(days: int = 30):
    """KST 기준 최근 `days`일의 시작 시각을 UTC naive datetime으로 반환."""
    try:
        from zoneinfo import ZoneInfo
        _kst = ZoneInfo("Asia/Seoul")
        _now_kst = datetime.now(_kst)
        return (_now_kst - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow() - timedelta(days=days)

logger = logging.getLogger(__name__)

from . import csrf
from .models import db, Bike, Analysis, User, UserAnalysis, PriceSuggestion, AnalysisLog, BikePriceHistory, PartPriceHistory, Part, PasswordResetToken, UserFeedback, Payment, SurveyResponse, SurveyImpression
from .email_sender import send_password_reset_email
from .utils.nickname import generate_unique_nickname
from .price_calculator import _normalize_part_name
from . import billing as billing_api

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

# ════════════════════════════════════════════════════════════════════
# PROMO_PRO_FOR_CONTINENTAL — 홍보 기간 임시 권한 부스트 (2026-04-29 추가)
# ────────────────────────────────────────────────────────────────────
# True인 동안 continental 유저를 _effective_plan()에서 'pro'로 취급한다.
# 효과: 분석 횟수 무제한 + 부품가 블러 해제 (pro와 동일).
# user.plan 컬럼은 손대지 않으므로 마이페이지/결제/표시는 영향 없음.
# 가격 이력 그래프는 'world_tour' 정확 매칭이라 풀리지 않음.
#
# 홍보 종료 시 되돌리는 법 — 이 파일에서 'PROMO_PRO_FOR_CONTINENTAL'를
# grep하면 2곳(이 상수 블록 + _effective_plan() 내부 분기)이 나옴.
# 두 블록을 모두 삭제하면 원본 그대로 복원된다.
# ════════════════════════════════════════════════════════════════════
_PROMO_PRO_FOR_CONTINENTAL = True


def _effective_plan(user) -> str:
    """plan_expires_at이 지났는데 워커가 아직 다운그레이드 안 했을 때 권한을 차단하기 위한 보조.
    admin은 항상 무제한이라 plan과 무관하므로 호출자가 별도로 user.role == 'admin'을 검사해야 한다."""
    if user is None:
        return "continental"
    if user.plan == "continental":
        # ── PROMO_PRO_FOR_CONTINENTAL: 홍보 기간 한정 (revert: 이 if 블록 삭제) ──
        if _PROMO_PRO_FOR_CONTINENTAL:
            return "pro"
        # ── PROMO_PRO_FOR_CONTINENTAL END ──
        return "continental"
    if user.plan_expires_at and user.plan_expires_at <= datetime.utcnow():
        return "continental"
    return user.plan


def _get_client_ip() -> str:
    # ProxyFix(x_for=1)가 Railway 프록시 1홉만큼 X-Forwarded-For를 신뢰해 remote_addr를 보정한다.
    # 헤더를 직접 읽으면 클라이언트가 임의 X-Forwarded-For를 보내 게스트 rate limit를 우회하게 되므로
    # 반드시 보정된 remote_addr만 사용한다.
    return (request.remote_addr or "").strip()


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
        plan = _effective_plan(user)

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
_EXIT_FEEDBACK_COOLDOWN_HOURS = 336  # 14일


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


@bp.route("/feedback/quick", methods=["POST"])
def feedback_quick():
    """결과 페이지 이탈 팝업 — 점수만 받는 간단 피드백.
    (설문조사 팝업으로 임시 전환됨. 호출자 없음 — 추후 복원 시 재사용.)
    """
    rating_raw = request.form.get("rating", "").strip()
    try:
        rating = int(rating_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_rating"}), 400
    if rating < 1 or rating > 10:
        return jsonify({"ok": False, "error": "out_of_range"}), 400

    fb = UserFeedback(
        user_id=session.get("user_id") or None,
        rating=rating,
        pain_point=None,
        good_point=None,
        message_to_dev=None,
    )
    db.session.add(fb)
    db.session.commit()
    return jsonify({"ok": True})


_SURVEY_TEXT_MAX = 2000


def _parse_yn(raw: str | None) -> bool | None:
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("yes", "y", "true", "1", "예", "네"):
        return True
    if v in ("no", "n", "false", "0", "아니요", "아니오"):
        return False
    return None


@bp.route("/feedback/survey", methods=["POST"])
def feedback_survey():
    """결과 페이지 이탈 팝업 — 4문항 설문(예/아니요 3 + 자유입력 1)."""
    q1 = _parse_yn(request.form.get("q1"))
    q2 = _parse_yn(request.form.get("q2"))
    q3 = _parse_yn(request.form.get("q3"))
    q4 = (request.form.get("q4") or "").strip()

    if q1 is None or q2 is None or q3 is None:
        return jsonify({"ok": False, "error": "missing_answer"}), 400
    if len(q4) > _SURVEY_TEXT_MAX:
        return jsonify({"ok": False, "error": "too_long"}), 400

    sr = SurveyResponse(
        user_id=session.get("user_id") or None,
        q1_useful=q1,
        q2_price_diff=q2,
        q3_paid_intent=q3,
        q4_feature_request=(q4 or None),
    )
    db.session.add(sr)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/feedback/survey/impression", methods=["POST"])
def feedback_survey_impression():
    """설문 팝업이 실제로 노출됐을 때 호출 — 응답률 분모."""
    imp = SurveyImpression(user_id=session.get("user_id") or None)
    db.session.add(imp)
    db.session.commit()
    return jsonify({"ok": True})


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
    is_new_signup = False
    if not user:
        is_new_signup = True
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
    # 신규 OAuth 가입은 /login?registered=1 우회 경로가 없으므로 쿼리로 1회 신호 전달 → index에서 funnel 이벤트 발화.
    if is_new_signup:
        return redirect(url_for("main.index", signup="google"))
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


def _load_billing_context(user: User) -> dict:
    """마이페이지 결제 탭용 데이터 — 현재 구독, 카드, 최근 결제 내역."""
    payments = (
        Payment.query
        .filter_by(user_id=user.id)
        .order_by(Payment.created_at.desc())
        .limit(20)
        .all()
    )
    return {
        "billing_payments": payments,
        "plan_label":  billing_api.PLAN_LABELS.get(user.plan, user.plan),
        "cycle_label": billing_api.CYCLE_LABELS.get(user.subscription_cycle or "", ""),
    }


def _mypage_render(user, tab, messages=None):
    if tab not in _MYPAGE_TABS:
        tab = "general"
    history_items = _load_history_items(user.id) if tab == "history" else None
    extra = _load_billing_context(user) if tab == "billing" else {}
    return render_template(
        "mypage.html",
        user=user,
        tab=tab,
        messages=messages or {},
        history_items=history_items,
        **extra,
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
    if request.args.get("paid") == "1":
        messages["success"] = "결제가 완료되었습니다. 즐거운 라이딩 되세요!"
    if request.args.get("canceled") == "1":
        messages["success"] = "구독이 취소되었습니다. 결제된 기간 만료까지 계속 이용할 수 있습니다."
    if request.args.get("resumed") == "1":
        messages["success"] = "구독을 다시 활성화했습니다."
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

    # --- KPI 지표 ---
    stats = {}

    # KST 기준 오늘 자정 → UTC naive 로 변환 (Analysis/User.created_at 은 naive UTC)
    try:
        from zoneinfo import ZoneInfo
        _kst = ZoneInfo("Asia/Seoul")
        _today_kst = datetime.now(_kst).replace(hour=0, minute=0, second=0, microsecond=0)
        today_utc = _today_kst.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    except Exception:
        # zoneinfo 미지원 폴백: UTC+9 고정
        _now_kst_naive = datetime.utcnow() + timedelta(hours=9)
        today_utc = _now_kst_naive.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=9)

    try:
        stats["new_users_today"] = User.query.filter(User.created_at >= today_utc).count()
    except Exception:
        stats["new_users_today"] = None

    try:
        plan_counts = db.session.query(User.plan, func.count()).group_by(User.plan).all()
        stats["plan_dist"] = {(plan or "continental"): cnt for plan, cnt in plan_counts}
    except Exception:
        stats["plan_dist"] = None

    try:
        stats["analyses_today"] = Analysis.query.filter(Analysis.analyzed_at >= today_utc).count()
    except Exception:
        stats["analyses_today"] = None

    try:
        stats["analyses_guest"]  = AnalysisLog.query.filter(AnalysisLog.user_id.is_(None)).count()
        stats["analyses_member"] = AnalysisLog.query.filter(AnalysisLog.user_id.isnot(None)).count()
    except Exception:
        stats["analyses_guest"]  = None
        stats["analyses_member"] = None

    try:
        avg = db.session.query(func.avg(UserFeedback.rating)).scalar()
        stats["avg_rating"] = round(float(avg), 1) if avg is not None else None
    except Exception:
        stats["avg_rating"] = None

    # 설문 응답률 KPI — 응답수 / 노출수
    try:
        sv_resp_count = SurveyResponse.query.count()
        sv_imp_count  = SurveyImpression.query.count()
        stats["survey_responses"]   = sv_resp_count
        stats["survey_impressions"] = sv_imp_count
        stats["survey_response_rate"] = (
            round(sv_resp_count * 100.0 / sv_imp_count, 1) if sv_imp_count else None
        )
    except Exception:
        stats["survey_responses"]     = None
        stats["survey_impressions"]   = None
        stats["survey_response_rate"] = None

    # UserFeedback 에는 feedback_type 컬럼이 없음.
    # pain_point/good_point/message_to_dev 중 하나라도 비어있지 않으면 full, 전부 비어있으면 quick 으로 집계.
    try:
        detail_present = or_(
            and_(UserFeedback.pain_point.isnot(None),     UserFeedback.pain_point     != ""),
            and_(UserFeedback.good_point.isnot(None),     UserFeedback.good_point     != ""),
            and_(UserFeedback.message_to_dev.isnot(None), UserFeedback.message_to_dev != ""),
        )
        _full_count = UserFeedback.query.filter(detail_present).count()
        _total_fb   = UserFeedback.query.count()
        stats["feedback_full"]  = _full_count
        stats["feedback_quick"] = _total_fb - _full_count
    except Exception:
        stats["feedback_full"]  = None
        stats["feedback_quick"] = None

    # 결제 KPI
    try:
        stats["paid_today"] = (
            db.session.query(func.count(Payment.id))
            .filter(Payment.status == "paid", Payment.paid_at >= today_utc)
            .scalar() or 0
        )
        stats["revenue_today"] = (
            db.session.query(func.coalesce(func.sum(Payment.amount_krw), 0))
            .filter(Payment.status == "paid", Payment.paid_at >= today_utc)
            .scalar() or 0
        )
        stats["active_subscribers"] = (
            User.query.filter(User.subscription_status == "active").count()
        )
    except Exception:
        stats["paid_today"]         = None
        stats["revenue_today"]      = None
        stats["active_subscribers"] = None

    return render_template(
        "admin.html",
        total_users=total_users,
        total_analyses=total_analyses,
        pending=pending,
        stats=stats,
    )


@bp.route("/admin/payments")
@admin_required
def admin_payments():
    rows = (
        db.session.query(Payment, User)
        .outerjoin(User, User.id == Payment.user_id)
        .order_by(Payment.created_at.desc())
        .limit(200)
        .all()
    )
    payments = [
        {
            "id":         str(p.id),
            "email":      (u.email if u else "-"),
            "plan":       p.plan,
            "cycle":      p.cycle,
            "amount_krw": p.amount_krw,
            "status":     p.status,
            "charge_type": p.charge_type,
            "paid_at":    p.paid_at,
            "created_at": p.created_at,
            "failure_reason": p.failure_reason,
        }
        for p, u in rows
    ]

    # 최근 30일 매출 차트
    try:
        since_utc = _admin_chart_since_utc(30)
        chart_rows = (
            db.session.query(cast(Payment.paid_at, Date), func.sum(Payment.amount_krw))
            .filter(Payment.status == "paid", Payment.paid_at >= since_utc)
            .group_by(cast(Payment.paid_at, Date))
            .order_by(cast(Payment.paid_at, Date).asc())
            .all()
        )
        revenue_chart = [{"x": str(d), "y": int(s or 0)} for d, s in chart_rows]
    except Exception:
        revenue_chart = []

    # 합계
    try:
        total_revenue = (
            db.session.query(func.coalesce(func.sum(Payment.amount_krw), 0))
            .filter(Payment.status == "paid")
            .scalar() or 0
        )
    except Exception:
        total_revenue = 0

    return render_template("admin_payments.html",
                           payments=payments,
                           revenue_chart=revenue_chart,
                           total_revenue=total_revenue)


@bp.route("/admin/users")
@admin_required
def admin_users():
    users = (
        User.query
        .order_by(User.created_at.desc())
        .all()
    )

    try:
        since_utc = _admin_chart_since_utc(30)
        rows = (
            db.session.query(cast(User.created_at, Date), func.count())
            .filter(User.created_at >= since_utc)
            .group_by(cast(User.created_at, Date))
            .order_by(cast(User.created_at, Date).asc())
            .all()
        )
        signup_chart = [{"x": str(d), "y": cnt} for d, cnt in rows]
    except Exception:
        signup_chart = []

    return render_template("admin_users.html", users=users, signup_chart=signup_chart)


@bp.route("/admin/analyses")
@admin_required
def admin_analyses():
    rows = (
        db.session.query(Analysis, Bike)
        .join(Bike, Bike.id == Analysis.bike_id)
        .order_by(Analysis.analyzed_at.desc())
        .limit(200)
        .all()
    )
    recent = [
        {
            "bike_name":   f"{bike.brand} {bike.model_name}" + (f" ({bike.model_year})" if bike.model_year else ""),
            "analyzed_at": analysis.analyzed_at,
            "saving_krw":  analysis.saving_krw,
        }
        for analysis, bike in rows
    ]

    try:
        since_utc = _admin_chart_since_utc(30)
        chart_rows = (
            db.session.query(cast(Analysis.analyzed_at, Date), func.count())
            .filter(Analysis.analyzed_at >= since_utc)
            .group_by(cast(Analysis.analyzed_at, Date))
            .order_by(cast(Analysis.analyzed_at, Date).asc())
            .all()
        )
        analyses_chart = [{"x": str(d), "y": cnt} for d, cnt in chart_rows]
    except Exception:
        analyses_chart = []

    return render_template("admin_analyses.html", recent=recent, analyses_chart=analyses_chart)


@bp.route("/admin/feedbacks")
@admin_required
def admin_feedbacks():
    rows = (
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
            "has_details": bool(fb.pain_point or fb.good_point or fb.message_to_dev),
        }
        for fb, u in rows
    ]

    try:
        since_utc = _admin_chart_since_utc(30)
        chart_rows = (
            db.session.query(cast(UserFeedback.created_at, Date), func.avg(UserFeedback.rating))
            .filter(UserFeedback.created_at >= since_utc)
            .group_by(cast(UserFeedback.created_at, Date))
            .order_by(cast(UserFeedback.created_at, Date).asc())
            .all()
        )
        rating_chart = [{"x": str(d), "y": round(float(avg), 1)} for d, avg in chart_rows if avg is not None]
    except Exception:
        rating_chart = []

    try:
        rating_dist_rows = (
            db.session.query(UserFeedback.rating, func.count())
            .filter(UserFeedback.rating.isnot(None))
            .group_by(UserFeedback.rating)
            .order_by(UserFeedback.rating.asc())
            .all()
        )
        dist_map = {r: cnt for r, cnt in rating_dist_rows}
        rating_dist = [{"score": i, "count": dist_map.get(i, 0)} for i in range(1, 11)]
    except Exception:
        rating_dist = [{"score": i, "count": 0} for i in range(1, 11)]

    return render_template("admin_feedbacks.html", feedbacks=feedbacks, rating_chart=rating_chart, rating_dist=rating_dist)


@bp.route("/admin/surveys")
@admin_required
def admin_surveys():
    rows = (
        db.session.query(SurveyResponse, User)
        .outerjoin(User, User.id == SurveyResponse.user_id)
        .order_by(SurveyResponse.created_at.desc())
        .all()
    )
    responses = [
        {
            "nickname":   (u.nickname if u else "비회원"),
            "plan":       (u.plan if u else "-"),
            "created_at": sr.created_at,
            "q1":         sr.q1_useful,
            "q2":         sr.q2_price_diff,
            "q3":         sr.q3_paid_intent,
            "q4":         sr.q4_feature_request,
        }
        for sr, u in rows
    ]

    try:
        resp_count = SurveyResponse.query.count()
        imp_count  = SurveyImpression.query.count()
    except Exception:
        resp_count = 0
        imp_count  = 0
    response_rate = (round(resp_count * 100.0 / imp_count, 1) if imp_count else None)

    # 예/아니요 집계 (Q1~Q3)
    def _yn_counts(col):
        try:
            yes = SurveyResponse.query.filter(col.is_(True)).count()
            no  = SurveyResponse.query.filter(col.is_(False)).count()
            return {"yes": yes, "no": no}
        except Exception:
            return {"yes": 0, "no": 0}

    yn_summary = {
        "q1": _yn_counts(SurveyResponse.q1_useful),
        "q2": _yn_counts(SurveyResponse.q2_price_diff),
        "q3": _yn_counts(SurveyResponse.q3_paid_intent),
    }

    return render_template(
        "admin_surveys.html",
        responses=responses,
        resp_count=resp_count,
        imp_count=imp_count,
        response_rate=response_rate,
        yn_summary=yn_summary,
    )


@bp.route("/admin/suggestion/<suggestion_id>")
@admin_required
def admin_suggestion(suggestion_id):
    ps = PriceSuggestion.query.filter_by(id=suggestion_id).first()
    if not ps:
        return redirect(url_for("main.admin"))

    analysis = ps.analysis
    bike     = analysis.bike

    # parts_snapshot 기반으로 frameset/handlebar 포함 전체 부품 객체 복원
    part_objects = _load_parts_for_result(analysis)
    part_labels = {
        "groupset":  "구동계",
        "wheelset":  "휠셋",
        "frameset":  "프레임셋",
        "saddle":    "안장",
        "handlebar": "핸들바",
    }

    rows = []
    for key in ("groupset", "wheelset", "frameset", "saddle", "handlebar"):
        part      = part_objects.get(key)
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

    parts = _load_parts_for_result(ps.analysis)
    now = datetime.utcnow()

    for key, suggested in (ps.suggestions or {}).items():
        if not suggested:
            continue
        new_price = suggested.get("suggested_price_krw")
        if not new_price:
            continue

        part = parts.get(key)
        if part is None:
            continue

        part.price_krw = int(new_price)
        new_url = suggested.get("source_url")
        if new_url:
            part.official_url = new_url
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

    if not (fb.pain_point or fb.good_point or fb.message_to_dev):
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

    # SSRF 방지 — 사설/loopback/링크로컬 IP로 해석되는 URL은 거부
    from .scraper import ScrapeError, assert_safe_url
    try:
        assert_safe_url(url)
    except ScrapeError:
        return _err(
            "지원하지 않는 링크 형식입니다.",
            "공개된 자전거 판매 페이지 링크를 입력해주세요.",
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
@csrf.exempt
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
        # ── PROMO_PRO_FOR_CONTINENTAL: 홍보 기간 한정 — 비로그인도 블러 해제 (revert: 이 if 블록 삭제) ──
        if _PROMO_PRO_FOR_CONTINENTAL:
            blur_mode = None
        # ── PROMO_PRO_FOR_CONTINENTAL END ──
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
        if user and (_effective_plan(user) == "world_tour" or user.role == "admin"):
            price_history = build_price_history(bike, parts)

    # 이탈 설문 팝업 — 로그인 유저가 쿨타임 내 응답을 완료했다면 서버에서 아예 렌더하지 않음.
    # 게스트는 클라이언트 localStorage로 쿨타임 적용.
    show_exit_popup = True
    if user_id:
        cutoff = datetime.utcnow() - timedelta(hours=_EXIT_FEEDBACK_COOLDOWN_HOURS)
        if SurveyResponse.query.filter(
            SurveyResponse.user_id == user_id,
            SurveyResponse.created_at >= cutoff,
        ).first():
            show_exit_popup = False

    return render_template(
        "index.html",
        bike=bike,
        parts=parts,
        analysis=analysis,
        bike_price=bike_price,
        blur_mode=blur_mode,
        blur_reset_minutes=blur_reset_minutes,
        price_history=price_history,
        show_exit_popup=show_exit_popup,
        exit_feedback_cooldown_hours=_EXIT_FEEDBACK_COOLDOWN_HOURS,
    )


# ── 결제 (토스페이먼츠 빌링키) ────────────────────────────────

from calendar import monthrange


def _add_months(dt: datetime, months: int) -> datetime:
    """캘린더 기준 N개월 후. 말일 보정 (예: 1/31 + 1개월 → 2/28)."""
    new_month = dt.month + months
    new_year  = dt.year + (new_month - 1) // 12
    new_month = ((new_month - 1) % 12) + 1
    last_day  = monthrange(new_year, new_month)[1]
    return dt.replace(year=new_year, month=new_month, day=min(dt.day, last_day))


def _next_billing_at(cycle: str, base: datetime | None = None) -> datetime:
    base = base or datetime.utcnow()
    if cycle == "yearly":
        return _add_months(base, 12)
    return _add_months(base, 1)


_VALID_PLANS  = {"pro", "world_tour"}
_VALID_CYCLES = {"monthly", "yearly"}


def _apply_paid_subscription(user: User, plan: str, cycle: str, paid_at: datetime) -> None:
    """결제 성공 시 user 플랜/만료일/다음 결제일 갱신."""
    user.plan = plan
    user.subscription_cycle  = cycle
    user.subscription_status = "active"
    next_at = _next_billing_at(cycle, paid_at)
    user.plan_expires_at = next_at
    user.next_billing_at = next_at
    user.billing_failed_count = 0


@bp.route("/billing/checkout/<plan>/<cycle>")
@login_required
def billing_checkout(plan, cycle):
    """결제 페이지. 토스 SDK로 카드 등록 → 빌링키 발급."""
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))

    if plan not in _VALID_PLANS or cycle not in _VALID_CYCLES:
        return redirect(url_for("main.pricing"))

    amount = billing_api.get_price(plan, cycle)
    if amount is None:
        return redirect(url_for("main.pricing"))

    # customerKey는 user.id를 그대로 사용 (UUID — 토스 customerKey는 1~50자, 영숫자/_/-/=/.@)
    customer_key = str(user.id)

    return render_template(
        "checkout.html",
        plan=plan,
        cycle=cycle,
        amount=amount,
        plan_label=billing_api.PLAN_LABELS.get(plan, plan),
        cycle_label=billing_api.CYCLE_LABELS.get(cycle, cycle),
        order_name=billing_api.order_name(plan, cycle),
        customer_key=customer_key,
        customer_email=user.email,
        customer_name=user.name or user.nickname,
        toss_client_key=billing_api.get_client_key(),
    )


@bp.route("/billing/success")
@login_required
def billing_success():
    """토스 successUrl 콜백.

    쿼리: customerKey, authKey, plan, cycle
    1) authKey + customerKey → billingKey 발급
    2) billingKey 즉시 1회 청구 → 첫 결제 완료
    3) user.plan 업데이트, payment 기록
    """
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))

    auth_key     = (request.args.get("authKey") or "").strip()
    customer_key = (request.args.get("customerKey") or "").strip()
    plan         = (request.args.get("plan") or "").strip()
    cycle        = (request.args.get("cycle") or "").strip()

    if not auth_key or not customer_key or plan not in _VALID_PLANS or cycle not in _VALID_CYCLES:
        return _err("결제 정보가 올바르지 않습니다.",
                    "결제를 다시 시도해주세요.", url=url_for("main.pricing"))

    # 본인 customerKey 검증 — 다른 유저의 callback을 가로채지 못하도록
    if customer_key != str(user.id):
        logger.warning("billing customerKey 불일치 user=%s req=%s", user.id, customer_key)
        return _err("결제 정보가 일치하지 않습니다.",
                    "다시 로그인 후 결제를 시도해주세요.", url=url_for("main.pricing"))

    amount = billing_api.get_price(plan, cycle)
    if amount is None:
        return _err("요금제 정보가 올바르지 않습니다.",
                    "결제를 다시 시도해주세요.", url=url_for("main.pricing"))

    # 1) 빌링키 발급
    try:
        bk_res = billing_api.issue_billing_key(auth_key, customer_key)
    except billing_api.BillingError as e:
        logger.error("billingKey 발급 실패 user=%s code=%s msg=%s", user.id, e.code, e.message)
        return _err("카드 등록에 실패했습니다.",
                    e.message or "다시 시도해주세요.", url=url_for("main.pricing"))

    billing_key  = bk_res.get("billingKey")
    card_company = bk_res.get("cardCompany") or (bk_res.get("card") or {}).get("issuerCode")
    card_number  = bk_res.get("cardNumber") or (bk_res.get("card") or {}).get("number")

    if not billing_key:
        logger.error("billingKey 발급 응답에 billingKey 없음 user=%s res=%s", user.id, bk_res)
        return _err("카드 등록에 실패했습니다.",
                    "결제를 다시 시도해주세요.", url=url_for("main.pricing"))

    user.billing_key          = billing_key
    user.billing_customer_key = customer_key
    user.billing_card_company = card_company
    user.billing_card_number  = card_number
    db.session.commit()

    # 2) 즉시 1회 청구 — payment row 미리 생성 후 토스 호출
    order_id = billing_api.make_order_id()
    payment = Payment(
        user_id=user.id,
        plan=plan,
        cycle=cycle,
        amount_krw=amount,
        toss_order_id=order_id,
        charge_type="initial",
        status="pending",
    )
    db.session.add(payment)
    db.session.commit()

    try:
        charge_res = billing_api.charge_billing_key(
            billing_key=billing_key,
            customer_key=customer_key,
            amount=amount,
            order_id=order_id,
            order_name=billing_api.order_name(plan, cycle),
            customer_email=user.email,
            customer_name=user.name or user.nickname,
        )
    except billing_api.BillingError as e:
        payment.status = "failed"
        payment.failure_reason = f"{e.code}: {e.message}"
        db.session.commit()
        logger.error("첫 결제 실패 user=%s code=%s msg=%s", user.id, e.code, e.message)
        return _err("결제에 실패했습니다.",
                    e.message or "다른 카드로 다시 시도해주세요.",
                    url=url_for("main.pricing"))

    if charge_res.get("status") != "DONE":
        payment.status = "failed"
        payment.failure_reason = f"unexpected status: {charge_res.get('status')}"
        db.session.commit()
        logger.error("첫 결제 비정상 응답 user=%s res=%s", user.id, charge_res)
        return _err("결제 처리가 완료되지 않았습니다.",
                    "잠시 후 다시 시도해주세요.", url=url_for("main.pricing"))

    paid_at = datetime.utcnow()
    payment.status           = "paid"
    payment.toss_payment_key = charge_res.get("paymentKey")
    payment.paid_at          = paid_at

    _apply_paid_subscription(user, plan, cycle, paid_at)
    db.session.commit()

    # 세션 plan 동기화 (base.html 헤더 표시용)
    session["user_plan"] = user.plan

    return redirect(url_for("main.mypage", tab="billing", paid=1))


@bp.route("/billing/fail")
@login_required
def billing_fail():
    """토스 failUrl 콜백 — 카드 등록/결제 실패."""
    code    = request.args.get("code", "")
    message = request.args.get("message", "결제가 취소되었거나 실패했습니다.")
    logger.info("billing fail user=%s code=%s msg=%s", session.get("user_id"), code, message)
    return _err("결제가 완료되지 않았습니다.", message, url=url_for("main.pricing"))


@bp.route("/billing/cancel", methods=["POST"])
@login_required
def billing_cancel():
    """구독 취소 — 다음 결제일에 자동결제 안 하고 만료시 다운그레이드."""
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))

    if user.subscription_status != "active":
        return redirect(url_for("main.mypage", tab="billing"))

    user.subscription_status = "canceled"
    # next_billing_at은 None으로 — cron에서 active만 청구
    user.next_billing_at = None
    db.session.commit()
    return redirect(url_for("main.mypage", tab="billing", canceled=1))


@bp.route("/billing/resume", methods=["POST"])
@login_required
def billing_resume():
    """취소 철회 — 만료일 전에 다시 active로."""
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))

    if user.subscription_status != "canceled":
        return redirect(url_for("main.mypage", tab="billing"))
    if not user.plan_expires_at or user.plan_expires_at <= datetime.utcnow():
        return redirect(url_for("main.mypage", tab="billing"))
    if not user.billing_key:
        # 카드 정보가 사라졌으면 재등록부터
        return redirect(url_for("main.pricing"))

    user.subscription_status = "active"
    user.next_billing_at = user.plan_expires_at
    db.session.commit()
    return redirect(url_for("main.mypage", tab="billing", resumed=1))
