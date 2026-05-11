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
    """Returns the start of the day N days ago in UTC, anchoring to the local timezone."""
    try:
        # Asia/Seoul is the legacy anchor (the app currently runs from Korea).
        # This can be parameterized later if we move the deployment.
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
    Add a row to bike_price_history when the complete-bike price is newly saved or changed.
    Skips saving if the price is identical. None prices are not recorded.
    The caller is responsible for session flush/commit.

    Args:
        force: If True, add a new row even if the price matches the previous row.
               Used to leave a "no change" confirmation stamp on the chart during
               natural worker firings.
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
    world_tour plan only — fetch the last 3 years of price history.
    Returns in the order bike / frameset / groupset / wheelset.
    Parts with no data return an empty list.
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
    """Helper for rendering the error page."""
    return render_template("error.html", message=message, hint=hint, url=url, **kwargs)


# Per-plan analysis quota limits
_WINDOW_HOURS = 5
_GUEST_LIMIT = 3
_CONTINENTAL_LIMIT = 10

# ════════════════════════════════════════════════════════════════════
# PROMO_PRO_FOR_CONTINENTAL — temporary promotional privilege boost (added 2026-04-29)
# ────────────────────────────────────────────────────────────────────
# While True, continental users are treated as 'pro' inside _effective_plan().
# Effect: unlimited analyses + part-price blur lifted (same as pro).
# The user.plan column is not touched, so my page / billing / labels are unaffected.
# The price-history chart is gated on an exact 'world_tour' match, so it is NOT unlocked.
#
# How to revert when the promotion ends — grep this file for
# 'PROMO_PRO_FOR_CONTINENTAL' and you'll find 2 places (this constant block
# and the branch inside _effective_plan()). Delete both blocks to restore the
# original behavior.
# ════════════════════════════════════════════════════════════════════
_PROMO_PRO_FOR_CONTINENTAL = True


def _effective_plan(user) -> str:
    """Helper that blocks privileges when plan_expires_at has passed but the worker
    has not yet downgraded the user. admin is always unlimited regardless of plan,
    so callers must separately check user.role == 'admin'."""
    if user is None:
        return "continental"
    if user.plan == "continental":
        # ── PROMO_PRO_FOR_CONTINENTAL: promo-only branch (revert: delete this if block) ──
        if _PROMO_PRO_FOR_CONTINENTAL:
            return "pro"
        # ── PROMO_PRO_FOR_CONTINENTAL END ──
        return "continental"
    if user.plan_expires_at and user.plan_expires_at <= datetime.utcnow():
        return "continental"
    return user.plan


def _get_client_ip() -> str:
    # ProxyFix(x_for=1) trusts X-Forwarded-For for one Railway proxy hop and
    # adjusts remote_addr accordingly. Reading the header directly would let
    # clients bypass the guest rate limit by sending an arbitrary X-Forwarded-For,
    # so we must use the adjusted remote_addr only.
    return (request.remote_addr or "").strip()


def _check_rate_limit(ip: str):
    """
    Returns (blocked, detail_limited, reset_minutes)
    - blocked=True      → analysis itself is blocked (guest exceeded 3 in 5h)
    - detail_limited=True → analysis allowed but part prices are blurred (continental exceeded 10)
    - reset_minutes     → if blocked, minutes remaining until usable again
    """
    user_id = session.get("user_id")
    window_start = datetime.utcnow() - timedelta(hours=_WINDOW_HOURS)

    if user_id:
        user = db.session.get(User, user_id)
        plan = _effective_plan(user)

        if plan in ("pro", "world_tour") or (user and user.role == "admin"):
            return False, False, 0

        # continental: 10 analyses per 5 hours; over the limit, blur part prices
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

    # Apply the IP-based 5-hour window only for logged-out users.
    # The user_id IS NULL filter prevents records from logged-in users on the same IP
    # from being counted toward the guest quota.
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
    bike = SimpleNamespace(brand="Fantasia", model_name="Radar 9 ARC Gen.3", model_year=2025)
    parts = {
        "groupset":  SimpleNamespace(part_name="Shimano Ultegra Di2 R8150", part_type="groupset",  price_krw=2_300_000),
        "wheelset":  SimpleNamespace(part_name="DT Swiss ARC 1100 DICUT DB 55", part_type="wheelset", price_krw=4_750_000),
        "frameset":  None,
        "saddle":    SimpleNamespace(part_name="Selle Italia Novus Boost EVO", part_type="saddle", price_krw=None),
        "handlebar": SimpleNamespace(part_name="Controltech Sirocco FL4", part_type="handlebar", price_krw=None),
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
    ("groupset",  "Groupset"),
    ("wheelset",  "Wheelset"),
    ("frameset",  "Frameset"),
    ("saddle",    "Saddle"),
    ("handlebar", "Handlebar"),
]

# Part keys that have an FK on the bikes table (others are always None)
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
    # FK-backed parts use the actual Part object; FK-less parts (frameset/handlebar)
    # are filled with None so all 5 rows always render.
    parts = [
        (key, label, getattr(bike, key) if key in _BIKE_FK_PARTS else None)
        for key, label in _SUGGEST_PARTS
    ]

    if request.method == "GET":
        return render_template("suggest.html", analysis=analysis, bike=bike,
                               parts=parts, errors={}, form_prices={}, form_urls={})

    # POST — validate and persist
    # The form labels this field as "Corrected price (USD)", so user input is in USD.
    # Convert USD → KRW before saving since storage is still KRW.
    from .exchange_rate import get_exchange_rates
    rate = get_exchange_rates().get("USD", 1470)

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
                usd_input = float(price_raw.replace(",", "").replace(" ", "").replace("$", ""))
                if usd_input <= 0:
                    suggested_price = None
                else:
                    suggested_price = int(round(usd_input * rate))
            except ValueError:
                errors[key] = "Please enter a valid USD amount."

        if source_url and suggested_price is None and key not in errors:
            errors[key] = "Please enter the price in USD."

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


# ── User feedback ──────────────────────────────────────────────

_FEEDBACK_TEXT_MAX = 2000
_EXIT_FEEDBACK_COOLDOWN_HOURS = 336  # 14 days


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
        errors["rating"] = "Please select a satisfaction rating."
    else:
        try:
            rating = int(rating_raw)
            if rating < 1 or rating > 10:
                errors["rating"] = "Please choose a score between 1 and 10."
        except ValueError:
            errors["rating"] = "Please enter a valid score."

    for key, label in (("pain_point", "Pain point"),
                       ("good_point", "What you liked"),
                       ("message_to_dev", "Message to the developer")):
        if len(form[key]) > _FEEDBACK_TEXT_MAX:
            errors[key] = f"{label} must be {_FEEDBACK_TEXT_MAX} characters or fewer."

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
    """Result-page exit popup — quick feedback that only collects a rating.
    (Temporarily replaced by the survey popup. Currently unused — kept for future restoration.)
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
    if v in ("yes", "y", "true", "1"):
        return True
    if v in ("no", "n", "false", "0"):
        return False
    return None


@bp.route("/feedback/survey", methods=["POST"])
def feedback_survey():
    """Result-page exit popup — 4-question survey (3 yes/no + 1 free-text)."""
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
    """Called when the survey popup is actually shown — denominator for response rate."""
    imp = SurveyImpression(user_id=session.get("user_id") or None)
    db.session.add(imp)
    db.session.commit()
    return jsonify({"ok": True})


# ── Authentication ────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _form_ctx(**kwargs):
    """Helper to preserve user input when re-rendering the signup form."""
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
        return render_template("register.html", error="Please enter a valid email address.", **ctx)
    if len(password) < 8:
        return render_template("register.html", error="Password must be at least 8 characters.", **ctx)
    if not name:
        return render_template("register.html", error="Please enter your name.", **ctx)
    if not nickname:
        return render_template("register.html", error="Please enter a nickname.", **ctx)
    if not birth_date:
        return render_template("register.html", error="Please enter your date of birth.", **ctx)
    if not privacy:
        return render_template("register.html", error="Please agree to the collection and use of personal information.", **ctx)

    try:
        birth_date_parsed = datetime.strptime(birth_date, "%Y-%m-%d").date()
    except ValueError:
        return render_template("register.html", error="The date of birth format is invalid.", **ctx)

    if User.query.filter_by(email=email).first():
        return render_template("register.html", error="This email is already in use.", **ctx)
    if User.query.filter_by(nickname=nickname).first():
        return render_template("register.html", error="This nickname is already in use.", **ctx)

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
        return render_template("login.html", error="Please check your email or password.", email=email)

    user = User.query.filter_by(email=email).first()
    if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
        return render_template("login.html", error="Email or password is incorrect.", email=email)

    user.last_login_at = datetime.utcnow()
    db.session.commit()

    _login_user_session(user)
    return redirect(url_for("main.index"))


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.index"))


# ── Google OAuth login ────────────────────────────────────────

def _login_user_session(user: User):
    """Populate session values on login — shared by /login and OAuth callback."""
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
        logger.warning("Google OAuth token exchange failed: %s", e)
        return redirect(url_for("main.login") + "?oauth_error=1")

    userinfo = token.get("userinfo")
    if not userinfo:
        try:
            userinfo = oauth.google.userinfo(token=token)
        except Exception as e:
            logger.error("Google userinfo lookup failed: %s", e)
            return redirect(url_for("main.login") + "?oauth_error=1")

    sub   = userinfo.get("sub")
    email = (userinfo.get("email") or "").strip().lower()
    name  = userinfo.get("name") or None

    if not sub or not email:
        logger.warning("Google userinfo missing required fields: sub=%s email=%s", sub, email)
        return redirect(url_for("main.login") + "?oauth_error=1")

    # 1) Match provider + sub → returning login
    user = User.query.filter_by(provider="google", provider_user_id=sub).first()

    # 2) Match by email → auto-link to an existing local account
    if not user:
        user = User.query.filter_by(email=email).first()
        if user:
            user.provider = "google"
            user.provider_user_id = sub

    # 3) New signup
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
    # New OAuth signups don't go through /login?registered=1, so we pass a one-shot
    # signal via query param → the index page fires the funnel event.
    if is_new_signup:
        return redirect(url_for("main.index", signup="google"))
    return redirect(url_for("main.index"))


# ── Password reset ────────────────────────────────────────────

_RESET_TTL_MINUTES   = 30
_RESET_RATE_WINDOW_H = 1     # 1 hour
_RESET_RATE_LIMIT    = 3     # max requests per account within the window


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    email = request.form.get("email", "").strip().lower()
    if not _EMAIL_RE.match(email):
        return render_template("forgot_password.html",
                               error="Please enter a valid email address.", email=email)

    # Prevent user enumeration — return the same response regardless of whether the email exists.
    user = User.query.filter_by(email=email).first()

    if user:
        # Rate limit — at most 3 requests per account per hour
        window_start = datetime.utcnow() - timedelta(hours=_RESET_RATE_WINDOW_H)
        recent_count = (
            PasswordResetToken.query
            .filter(PasswordResetToken.user_id == user.id,
                    PasswordResetToken.created_at >= window_start)
            .count()
        )
        if recent_count >= _RESET_RATE_LIMIT:
            logger.warning("password reset rate limit — user=%s count=%d", user.email, recent_count)
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
                               error="Password must be at least 8 characters.")
    if password != confirm:
        return render_template("reset_password.html", token=token,
                               error="Passwords do not match.")

    user = db.session.get(User, prt.user_id)
    if not user:
        return render_template("reset_password.html", invalid=True)

    user.password_hash = generate_password_hash(password)
    prt.used_at = datetime.utcnow()

    # Invalidate all other unused tokens for the same user
    (PasswordResetToken.query
        .filter(PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.id != prt.id)
        .update({"used_at": datetime.utcnow()}, synchronize_session=False))

    db.session.commit()
    return redirect(url_for("main.login") + "?reset=1")


def admin_required(f):
    """Allow only logged-in users with role='admin'. Others redirect to the main page."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("user_role") != "admin":
            return redirect(url_for("main.index"))
        return f(*args, **kwargs)
    return wrapper


def login_required(f):
    """Allow only logged-in users. Logged-out users are redirected to /login."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("main.login"))
        return f(*args, **kwargs)
    return wrapper


_MYPAGE_TABS = {"general", "account", "billing", "usage", "history"}


def _current_user_or_logout():
    """Load the User from the session's user_id. If missing, clear the session and return None."""
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
    """Data for the My Page billing tab — current subscription, card, and recent payments."""
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
        messages["success"] = "Your changes have been saved."
    if request.args.get("paid") == "1":
        messages["success"] = "Payment complete. Enjoy the ride!"
    if request.args.get("canceled") == "1":
        messages["success"] = "Your subscription has been canceled. You can keep using the service until the paid period ends."
    if request.args.get("resumed") == "1":
        messages["success"] = "Your subscription has been reactivated."
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
        return _mypage_render(user, "account", {"error": "Please enter a nickname."})
    if len(new_nick) > 30:
        return _mypage_render(user, "account", {"error": "Nickname must be 30 characters or fewer."})
    if new_nick == user.nickname:
        return redirect(url_for("main.mypage", tab="account"))
    if User.query.filter(User.nickname == new_nick, User.id != user.id).first():
        return _mypage_render(user, "account", {"error": "This nickname is already in use."})
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
        return _mypage_render(user, "account", {"error": "Google-linked accounts cannot change their email address."})
    new_email = request.form.get("email", "").strip().lower()
    if not _EMAIL_RE.match(new_email):
        return _mypage_render(user, "account", {"error": "Please enter a valid email address."})
    if new_email == user.email:
        return redirect(url_for("main.mypage", tab="account"))
    if User.query.filter(User.email == new_email, User.id != user.id).first():
        return _mypage_render(user, "account", {"error": "This email is already in use."})
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
        return _mypage_render(user, "account", {"error": "Social-login accounts cannot set a password."})
    current = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm = request.form.get("new_password_confirm", "")
    if not check_password_hash(user.password_hash, current):
        return _mypage_render(user, "account", {"error": "Current password is incorrect."})
    if len(new_password) < 8:
        return _mypage_render(user, "account", {"error": "New password must be at least 8 characters."})
    if new_password != confirm:
        return _mypage_render(user, "account", {"error": "New password and confirmation do not match."})
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

    # --- KPI metrics ---
    stats = {}

    # Today's midnight in Asia/Seoul → naive UTC (Analysis/User.created_at are naive UTC)
    try:
        from zoneinfo import ZoneInfo
        _kst = ZoneInfo("Asia/Seoul")
        _today_kst = datetime.now(_kst).replace(hour=0, minute=0, second=0, microsecond=0)
        today_utc = _today_kst.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    except Exception:
        # Fallback when zoneinfo is unavailable: hard-coded UTC+9
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

    # Survey response rate KPI — responses / impressions
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

    # UserFeedback does not have a feedback_type column.
    # Count as "full" if any of pain_point/good_point/message_to_dev is non-empty,
    # otherwise count as "quick".
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

    # Payment KPI
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

    # Revenue chart for the last 30 days
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

    # Totals
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
            "nickname":   (u.nickname if u else "Guest"),
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
            "nickname":   (u.nickname if u else "Guest"),
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

    # Yes/No aggregation (Q1–Q3)
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

    # Reconstruct full part objects (incl. frameset/handlebar) from parts_snapshot
    part_objects = _load_parts_for_result(analysis)
    part_labels = {
        "groupset":  "Groupset",
        "wheelset":  "Wheelset",
        "frameset":  "Frameset",
        "saddle":    "Saddle",
        "handlebar": "Handlebar",
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

    proposer = "Guest"
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
        nickname=(user.nickname if user else "Guest"),
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
    """The Celery instance for the current Flask app."""
    return current_app.extensions["celery"]


@bp.route("/analyze", methods=["POST"])
def analyze():
    """Validate URL and rate limit, enqueue a Celery task, and render the loading page.

    The actual scraping / AI / DB work runs in a separate worker via
    app.tasks.analyze_bike_task. The frontend polls /analyze/status/<task_id>
    and redirects to /result/<analysis_id> when the analysis is ready."""
    url = request.form.get("url", "").strip()
    if not url:
        return _err(
            "Please enter a link.",
            "Paste the link to the bike listing page you want to analyze.",
            url=url,
        )
    if len(url) > 2000:
        return _err(
            "Invalid link.",
            "Please copy the link from the address bar and paste it again.",
        )
    if urlparse(url).scheme not in ("http", "https"):
        return _err(
            "Unsupported link format.",
            "Please enter a link that starts with http:// or https:// and points to a bike listing page.",
        )

    # Prevent SSRF — reject URLs that resolve to private / loopback / link-local IPs
    from .scraper import ScrapeError, assert_safe_url
    try:
        assert_safe_url(url)
    except ScrapeError:
        return _err(
            "Unsupported link format.",
            "Please enter a publicly accessible bike listing link.",
        )

    ip = _get_client_ip()
    blocked, detail_limited, reset_minutes = _check_rate_limit(ip)

    if blocked:
        return redirect(url_for("main.index", limit="true", reset_minutes=reset_minutes))

    print(f"[ANALYZE] request URL: {url} | ip={ip} | detail_limited={detail_limited}")

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
    """Polls Celery task state. Returns JSON — used by the loading page JS."""
    celery = _get_celery()
    async_result = celery.AsyncResult(task_id)
    state = async_result.state

    # REVOKED: canceled by clicking the logo — the client has likely already moved to /,
    # but clean up just in case the polling request is still alive.
    if state == "REVOKED":
        return jsonify({"state": "revoked"})

    if state in ("PENDING", "RECEIVED", "STARTED", "RETRY"):
        return jsonify({"state": "pending"})

    if state == "FAILURE":
        # The task function catches exceptions and returns an error dict, so reaching this
        # branch means the task itself crashed.
        logger.error("analyze task FAILURE | task_id=%s | %s", task_id, async_result.traceback)
        return jsonify({
            "state": "error",
            "message": "A temporary error occurred.",
            "hint": "Please try again in a moment.",
            "url": "",
        })

    if state == "SUCCESS":
        result = async_result.result or {}
        if result.get("status") == "success":
            return jsonify({"state": "success", "analysis_id": result["analysis_id"]})
        if result.get("status") == "error":
            return jsonify({
                "state": "error",
                "message": result.get("message", "An error occurred."),
                "hint": result.get("hint", ""),
                "url": result.get("url", ""),
            })

    return jsonify({"state": "pending"})


@bp.route("/analyze/cancel/<task_id>", methods=["POST"])
@csrf.exempt
def analyze_cancel(task_id):
    """Revoke the task — terminate the worker process with SIGTERM to stop it immediately.

    Returns 204 with no body to support sendBeacon."""
    celery = _get_celery()
    celery.control.revoke(task_id, terminate=True, signal="SIGTERM")
    print(f"[CANCEL] task revoked: id={task_id}")
    return ("", 204)


@bp.route("/analyze/error")
def analyze_error():
    """Error-render endpoint that loading.html navigates to after detecting a task failure."""
    return _err(
        message=request.args.get("message", "An error occurred."),
        hint=request.args.get("hint", ""),
        url=request.args.get("url", ""),
    )


# Part keys — used to reconstruct Analysis.parts_snapshot. Keep in sync with task.PART_KEYS.
_RESULT_PART_KEYS = ["groupset", "wheelset", "frameset", "saddle", "handlebar"]
_BIKE_FK_PART_KEYS = {"groupset", "wheelset", "saddle"}


def _load_parts_for_result(analysis: Analysis) -> dict:
    """Reconstruct the parts dict for the result screen. Prefers parts_snapshot; falls back to bike FK + model_name."""
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

    # Fallback for historical analyses that have no parts_snapshot
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
    # handlebar cannot be recovered from historical data → leave as None
    return parts


@bp.route("/result/<analysis_id>")
def result(analysis_id):
    """Render the result screen using the analysis_id left by the task. Safe against form refresh."""
    analysis = Analysis.query.filter_by(id=analysis_id).first()
    if not analysis:
        return redirect(url_for("main.index"))

    bike = analysis.bike
    parts = _load_parts_for_result(analysis)

    bike_price = bike.price_krw or 0

    # Blur mode — re-derive from the rate limit for the current session/IP
    ip = _get_client_ip()
    _blocked, detail_limited, reset_minutes = _check_rate_limit(ip)

    if not session.get("user_id"):
        blur_mode = "guest"
        blur_reset_minutes = 0
        # ── PROMO_PRO_FOR_CONTINENTAL: promo-only branch — also lifts blur for guests (revert: delete this if block) ──
        if _PROMO_PRO_FOR_CONTINENTAL:
            blur_mode = None
        # ── PROMO_PRO_FOR_CONTINENTAL END ──
    elif detail_limited:
        blur_mode = "continental"
        blur_reset_minutes = reset_minutes
    else:
        blur_mode = None
        blur_reset_minutes = 0

    # Price-history graph is shown only to world_tour / admin
    price_history = None
    user_id = session.get("user_id")
    if user_id:
        user = db.session.get(User, user_id)
        if user and (_effective_plan(user) == "world_tour" or user.role == "admin"):
            price_history = build_price_history(bike, parts)

    # Exit survey popup — if a logged-in user has already responded within the cooldown,
    # don't render it at all on the server side.
    # Guests get the cooldown enforced client-side via localStorage.
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


# ── OG share card image ───────────────────────────────────
# 1200×630 PNG exposed as og:image when the result URL is shared on
# messengers / X / Threads, etc.
# Analysis rows are immutable, so we can disk-cache per analysis_id.

@bp.route("/og/result/<analysis_id>.png")
def og_result_image(analysis_id):
    analysis = Analysis.query.filter_by(id=analysis_id).first()
    if not analysis:
        return ("", 404)

    # Keep the import inside the try block so any failure (Pillow missing, fonts
    # unavailable, etc.) is captured in the log. Previously, an import failure
    # would surface as a raw ImportError → 500, so the real cause didn't make it
    # into the Railway logs.
    try:
        from .og_image import get_or_render_og
        bike = analysis.bike
        png = get_or_render_og(
            analysis_id=analysis.id,
            saving_krw=analysis.saving_krw or 0,
            saving_pct=analysis.saving_pct,
            bike_brand=bike.brand if bike else None,
            bike_model=bike.model_name if bike else None,
            bike_year=bike.model_year if bike else None,
        )
    except Exception:
        logger.exception("OG image render failed analysis_id=%s", analysis_id)
        return ("", 500)

    resp = make_response(png)
    resp.headers["Content-Type"] = "image/png"
    # 30-day cache — analysis snapshots don't change, so it's safe for CDNs / SNS crawlers to cache aggressively.
    resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return resp


# ── Billing (Toss Payments billing keys) ──────────────────────

from calendar import monthrange


def _add_months(dt: datetime, months: int) -> datetime:
    """N calendar months from `dt`, with end-of-month clamping (e.g. 1/31 + 1 month → 2/28)."""
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
    """Update the user's plan / expiration / next billing date on a successful payment."""
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
    """Checkout page. Registers a card via the Toss SDK → issues a billing key."""
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))

    if plan not in _VALID_PLANS or cycle not in _VALID_CYCLES:
        return redirect(url_for("main.pricing"))

    amount = billing_api.get_price(plan, cycle)
    if amount is None:
        return redirect(url_for("main.pricing"))

    # Use user.id as customerKey directly (UUID — Toss customerKey is 1–50 chars: alphanumeric / _ / - / = / . / @)
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
    """Toss successUrl callback.

    Query: customerKey, authKey, plan, cycle
    1) authKey + customerKey → issue billingKey
    2) Charge billingKey once immediately → complete the first payment
    3) Update user.plan, record the payment
    """
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))

    auth_key     = (request.args.get("authKey") or "").strip()
    customer_key = (request.args.get("customerKey") or "").strip()
    plan         = (request.args.get("plan") or "").strip()
    cycle        = (request.args.get("cycle") or "").strip()

    if not auth_key or not customer_key or plan not in _VALID_PLANS or cycle not in _VALID_CYCLES:
        return _err("Invalid payment information.",
                    "Please try the payment again.", url=url_for("main.pricing"))

    # Verify the customerKey belongs to this user — prevents intercepting another user's callback
    if customer_key != str(user.id):
        logger.warning("billing customerKey mismatch user=%s req=%s", user.id, customer_key)
        return _err("Payment information does not match.",
                    "Please log in again and try the payment.", url=url_for("main.pricing"))

    amount = billing_api.get_price(plan, cycle)
    if amount is None:
        return _err("Invalid plan information.",
                    "Please try the payment again.", url=url_for("main.pricing"))

    # 1) Issue billing key
    try:
        bk_res = billing_api.issue_billing_key(auth_key, customer_key)
    except billing_api.BillingError as e:
        logger.error("billingKey issue failed user=%s code=%s msg=%s", user.id, e.code, e.message)
        return _err("Card registration failed.",
                    e.message or "Please try again.", url=url_for("main.pricing"))

    billing_key  = bk_res.get("billingKey")
    card_company = bk_res.get("cardCompany") or (bk_res.get("card") or {}).get("issuerCode")
    card_number  = bk_res.get("cardNumber") or (bk_res.get("card") or {}).get("number")

    if not billing_key:
        logger.error("billingKey issue response is missing billingKey user=%s res=%s", user.id, bk_res)
        return _err("Card registration failed.",
                    "Please try the payment again.", url=url_for("main.pricing"))

    user.billing_key          = billing_key
    user.billing_customer_key = customer_key
    user.billing_card_company = card_company
    user.billing_card_number  = card_number
    db.session.commit()

    # 2) Charge once immediately — create the payment row first, then call Toss
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
        logger.error("first charge failed user=%s code=%s msg=%s", user.id, e.code, e.message)
        return _err("Payment failed.",
                    e.message or "Please try again with a different card.",
                    url=url_for("main.pricing"))

    if charge_res.get("status") != "DONE":
        payment.status = "failed"
        payment.failure_reason = f"unexpected status: {charge_res.get('status')}"
        db.session.commit()
        logger.error("first charge unexpected response user=%s res=%s", user.id, charge_res)
        return _err("Payment processing did not complete.",
                    "Please try again in a moment.", url=url_for("main.pricing"))

    paid_at = datetime.utcnow()
    payment.status           = "paid"
    payment.toss_payment_key = charge_res.get("paymentKey")
    payment.paid_at          = paid_at

    _apply_paid_subscription(user, plan, cycle, paid_at)
    db.session.commit()

    # Sync plan into the session (used by the base.html header)
    session["user_plan"] = user.plan

    return redirect(url_for("main.mypage", tab="billing", paid=1))


@bp.route("/billing/fail")
@login_required
def billing_fail():
    """Toss failUrl callback — card registration / payment failure."""
    code    = request.args.get("code", "")
    message = request.args.get("message", "The payment was canceled or failed.")
    logger.info("billing fail user=%s code=%s msg=%s", session.get("user_id"), code, message)
    return _err("Payment was not completed.", message, url=url_for("main.pricing"))


@bp.route("/billing/cancel", methods=["POST"])
@login_required
def billing_cancel():
    """Cancel subscription — skip auto-charge on the next billing date and downgrade at expiration."""
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))

    if user.subscription_status != "active":
        return redirect(url_for("main.mypage", tab="billing"))

    user.subscription_status = "canceled"
    # Set next_billing_at to None — the cron only charges active subscriptions
    user.next_billing_at = None
    db.session.commit()
    return redirect(url_for("main.mypage", tab="billing", canceled=1))


@bp.route("/billing/resume", methods=["POST"])
@login_required
def billing_resume():
    """Undo cancellation — reactivate before the expiration date."""
    user = _current_user_or_logout()
    if not user:
        return redirect(url_for("main.login"))

    if user.subscription_status != "canceled":
        return redirect(url_for("main.mypage", tab="billing"))
    if not user.plan_expires_at or user.plan_expires_at <= datetime.utcnow():
        return redirect(url_for("main.mypage", tab="billing"))
    if not user.billing_key:
        # If the card info is gone, send them to register again first
        return redirect(url_for("main.pricing"))

    user.subscription_status = "active"
    user.next_billing_at = user.plan_expires_at
    db.session.commit()
    return redirect(url_for("main.mypage", tab="billing", resumed=1))
