import functools
import logging
import re
import traceback
from datetime import datetime, date, timedelta
from types import SimpleNamespace
from urllib.parse import urlparse
from flask import Blueprint, render_template, request, session, redirect, url_for, make_response
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

from .models import db, Bike, Analysis, User, UserAnalysis, PriceSuggestion, AnalysisLog, BikePriceHistory, PartPriceHistory
from .scraper import fetch_html, ScrapeError
from .ai_analyzer import extract_bike_info, AnalysisError, ServiceBusyError
from .exchange_rate import get_exchange_rates
from .price_calculator import get_or_fetch_part, calculate_parts_sum

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


_HISTORY_WEEKS = 150


def _serialize_history(rows) -> list[dict]:
    return [{"x": r.recorded_at.isoformat(), "y": r.price_krw} for r in rows]


def build_price_history(bike: Bike, parts: dict) -> dict:
    """
    world_tour 플랜 전용 — 최근 150주 가격 이력 조회.
    bike / frameset / groupset / wheelset 순서로 반환.
    데이터가 없는 부품은 빈 리스트.
    """
    cutoff = datetime.utcnow() - timedelta(weeks=_HISTORY_WEEKS)

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

# AI가 추출하는 부품 키 목록
PART_KEYS = ["groupset", "wheelset", "frameset", "saddle", "handlebar"]

# 스크래핑 에러 코드별 (메시지, hint)
SCRAPE_ERRORS = {
    "connection_error": (
        "사이트에 접근할 수 없습니다.",
        "링크가 올바른지 확인하거나, 잠시 후 다시 시도해주세요.",
    ),
    "timeout": (
        "사이트 응답이 너무 느립니다.",
        "잠시 후 다시 시도하거나, 다른 판매처 링크로 분석해보세요.",
    ),
    "blocked": (
        "해당 사이트는 자동 접근을 허용하지 않습니다.",
        "다른 판매처의 동일 제품 링크로 다시 시도해보세요.",
    ),
    "not_found": (
        "페이지를 찾을 수 없습니다.",
        "링크가 만료되었거나 삭제된 것 같습니다. 판매처에서 링크를 다시 확인해주세요.",
    ),
    "http_error": (
        "사이트 접근 중 오류가 발생했습니다.",
        "잠시 후 다시 시도해주세요.",
    ),
    "unknown": (
        "사이트 접근 중 오류가 발생했습니다.",
        "잠시 후 다시 시도해주세요.",
    ),
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
        plan = (user.plan if user else None) or "free"

        if plan in ("pro", "world_tour") or (user and user.role == "admin"):
            return False, False, 0

        if plan == "continental":
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

        # free 로그인 유저: 횟수 제한 없음
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
    )
    db.session.add(user)
    db.session.commit()
    return redirect(url_for("main.login") + "?registered=1")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        registered = request.args.get("registered")
        return render_template("login.html", registered=registered)

    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not _EMAIL_RE.match(email) or len(password) < 8:
        return render_template("login.html", error="이메일 또는 비밀번호를 확인해주세요.", email=email)

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return render_template("login.html", error="이메일 또는 비밀번호가 올바르지 않습니다.", email=email)

    user.last_login_at = datetime.utcnow()
    db.session.commit()

    session["user_id"]       = str(user.id)
    session["user_email"]    = user.email
    session["user_nickname"] = user.nickname
    session["user_role"]     = user.role
    return redirect(url_for("main.index"))


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.index"))


def admin_required(f):
    """role='admin'인 로그인 사용자만 허용. 그 외는 메인으로 리다이렉트."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("user_role") != "admin":
            return redirect(url_for("main.index"))
        return f(*args, **kwargs)
    return wrapper


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

    return render_template(
        "admin.html",
        total_users=total_users,
        total_analyses=total_analyses,
        recent=recent,
        users=users,
        pending=pending,
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


@bp.route("/history")
def history():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    rows = (
        db.session.query(UserAnalysis, Analysis, Bike)
        .join(Analysis, Analysis.id == UserAnalysis.analysis_id)
        .join(Bike,     Bike.id     == Analysis.bike_id)
        .filter(UserAnalysis.user_id == session["user_id"])   # 반드시 본인 데이터만
        .order_by(UserAnalysis.viewed_at.desc())
        .all()
    )

    history_items = [
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

    return render_template("history.html", history=history_items)


@bp.route("/analyze", methods=["POST"])
def analyze():
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

    # STEP 1: 스크래핑
    print("[STEP 1] 스크래핑 시작...")
    try:
        page_text = fetch_html(url)
        print(f"[STEP 1] 완료 ({len(page_text)}자)")
    except ScrapeError as e:
        print(f"[STEP 1] 실패: {e}")
        msg, hint = SCRAPE_ERRORS.get(e.code, SCRAPE_ERRORS["unknown"])
        return _err(msg, hint, url=url)

    if not page_text:
        print("[STEP 1] 0자 반환 — 지원하지 않는 사이트")
        return _err("페이지 정보를 불러올 수 없습니다.", "해당 사이트는 현재 지원하지 않습니다. 다른 판매처의 동일 제품 링크로 다시 시도해주세요.", url=url)

    # STEP 2: AI 분석
    print("[STEP 2] AI 분석 시작...")
    exchange_rates = get_exchange_rates()
    try:
        info = extract_bike_info(page_text, exchange_rates=exchange_rates)
        print(f"[STEP 2] 완료: {info['brand']} / {info['model_name']} / {info.get('model_year')}")
    except AnalysisError as e:
        print(f"[STEP 2] 실패: {e}")
        return _err(
            "자전거 정보를 확인할 수 없습니다.",
            "자전거 판매 페이지가 맞는지 확인하거나, 구동계·모델명이 명시된 다른 페이지로 시도해주세요.",
            url=url,
        )
    except ServiceBusyError:
        print("[STEP 2] Rate limit 재시도 실패")
        return _err(
            "현재 서비스가 혼잡합니다.",
            "1~2분 후 다시 시도해주세요.",
            url=url,
        )

    try:
        # STEP 3: bikes 테이블 조회 또는 생성
        bike = Bike.query.filter_by(
            brand=info["brand"],
            model_name=info["model_name"],
            model_year=info.get("model_year"),
        ).first()

        is_new_bike = bike is None
        bike_price_changed = False
        if is_new_bike:
            print(f"[CACHE MISS] bikes — 신규 생성: {info['brand']} {info['model_name']} {info.get('model_year')}")
        else:
            print(f"[CACHE HIT]  bikes — 기존 조회: {bike.brand} {bike.model_name} {bike.model_year}")

        if is_new_bike:
            bike = Bike(
                brand=info["brand"],
                model_name=info["model_name"],
                model_year=info.get("model_year"),
                price_krw=info.get("price_krw"),
                official_url=url,
                frame_material=info.get("frame_material", "unknown"),
                frame_material_confidence=info.get("frame_material_confidence", 0),
                frame_material_source=info.get("frame_material_source", "unknown"),
                brake_type=info.get("brake_type", "unknown"),
            )
        else:
            # 기존 bike — 신규 스크랩가가 있고 기존 가격과 다르면 업데이트
            new_price = info.get("price_krw")
            if new_price and bike.price_krw != new_price:
                bike.price_krw = new_price
                bike_price_changed = True

        # STEP 4: 부품 조회 (세션에 bike 추가 전에 실행 — autoflush 방지)
        parts = {}
        for key in PART_KEYS:
            if key == "frameset":
                # 프레임셋은 AI 추출값 무시 — bike model_name으로 항상 parts DB에 저장
                parts[key] = get_or_fetch_part(
                    part_name=bike.model_name,
                    part_name_normalized=bike.model_name,
                    part_type="frameset",
                )
                continue
            part_info = info.get(key, {})
            if not part_info or not part_info.get("part_name"):
                parts[key] = None
                continue
            parts[key] = get_or_fetch_part(
                part_name=part_info["part_name"],
                part_name_normalized=part_info["part_name_normalized"],
                part_type=key,
            )

        # groupset은 NOT NULL — 없으면 케이스 6
        if parts["groupset"] is None:
            db.session.rollback()
            return _err(
                "구동계 정보를 확인할 수 없습니다.",
                "구동계(브랜드·모델명)가 명시된 판매 페이지 링크로 다시 시도해주세요.",
                url=url,
            )

        bike.groupset_id = parts["groupset"].id
        bike.wheelset_id = parts["wheelset"].id if parts["wheelset"] else None
        bike.saddle_id = parts["saddle"].id if parts["saddle"] else None
        bike.last_verified_at = datetime.utcnow()

        if is_new_bike:
            db.session.add(bike)
        db.session.flush()  # bike.id 확정 (groupset_id 세팅 완료 후라 안전)

        # bike 가격 이력 저장 (신규 저장 또는 변경 시)
        if is_new_bike and bike.price_krw:
            record_bike_price_history(bike, bike.price_krw)
        elif bike_price_changed:
            record_bike_price_history(bike, bike.price_krw)

        # STEP 5: 가격 계산
        part_list = [p for p in parts.values() if p is not None]
        parts_sum_krw, missing_parts = calculate_parts_sum(part_list)

        # AI가 부품 자체를 추출 못한 경우(None)도 missing_parts에 포함
        for key in PART_KEYS:
            if parts[key] is None and key not in missing_parts:
                missing_parts.append(key)

        bike_price = info.get("price_krw") or bike.price_krw or 0
        saving_krw = parts_sum_krw - bike_price
        saving_pct = round(saving_krw / parts_sum_krw * 100, 1) if parts_sum_krw else 0

        analysis = Analysis(
            bike_id=bike.id,
            parts_sum_krw=parts_sum_krw,
            saving_krw=saving_krw,
            saving_pct=saving_pct,
            missing_parts=missing_parts,
            analyzed_at=datetime.utcnow(),
        )
        db.session.add(analysis)
        db.session.flush()  # analysis.id 확정

        # STEP 6: 로그인 상태면 히스토리 저장
        if session.get("user_id"):
            ua = UserAnalysis(
                user_id=session["user_id"],
                analysis_id=analysis.id,
            )
            db.session.add(ua)

        log = AnalysisLog(
            ip_address=ip,
            user_id=session.get("user_id"),
            is_detailed=not detail_limited,
        )
        db.session.add(log)
        db.session.commit()
        print(f"[STEP 5] 완료 — 부품합산: {parts_sum_krw:,}원 / 완성차: {bike_price:,}원 / 절약: {saving_krw:,}원")

    except Exception as e:
        db.session.rollback()
        logger.error("분석 중 예외 발생 | url=%s\n%s", url, traceback.format_exc())
        return _err(
            "일시적인 오류가 발생했습니다.",
            "잠시 후 다시 시도해주세요. 문제가 반복되면 다른 링크로 시도해보세요.",
            url=url,
        )

    # 플랜별 blur 모드 결정
    if not session.get("user_id"):
        blur_mode = "guest"
        blur_reset_minutes = 0
    elif detail_limited:
        blur_mode = "continental"
        blur_reset_minutes = reset_minutes
    else:
        blur_mode = None
        blur_reset_minutes = 0

    # world_tour 플랜 및 admin만 가격 이력 그래프 데이터 전달
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
