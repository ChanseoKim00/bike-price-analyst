"""
Celery task for the analyze workflow.

POST /analyze just validates and enqueues this task → the frontend polls
/analyze/status/<task_id>. Clicking the logo hits /analyze/cancel/<task_id>, which
revokes the task with terminate=True, killing the worker process for an immediate stop.

Return format (stored in Celery result backend, returned verbatim by the status endpoint):
  success: {"status": "success", "analysis_id": "<uuid>"}
  error:   {"status": "error", "message": "...", "hint": "...", "url": "..."}

When revoked, the task itself enters the REVOKED state and this function never returns.
"""
import logging
import traceback
from datetime import datetime

from .ai_analyzer import AnalysisError, ServiceBusyError, extract_bike_info
from .celery_app import celery
from .exchange_rate import get_exchange_rates
from .models import Analysis, AnalysisLog, Bike, UserAnalysis, db
from .price_calculator import calculate_parts_sum, get_or_fetch_part
from .scraper import ScrapeError, fetch_html

logger = logging.getLogger(__name__)

# Part keys — same order as routes.PART_KEYS
PART_KEYS = ["groupset", "wheelset", "frameset", "saddle", "handlebar"]

# (message, hint) per scrape error code — keep in sync with routes.SCRAPE_ERRORS
SCRAPE_ERRORS = {
    "connection_error": (
        "Could not reach the site.",
        "Please check that the link is correct, or try again in a moment.",
    ),
    "timeout": (
        "The site is responding too slowly.",
        "Please try again in a moment, or try a link from a different retailer.",
    ),
    "blocked": (
        "This site does not allow automated access.",
        "Please try the same product on a different retailer's page.",
    ),
    "not_found": (
        "Page not found.",
        "The link may have expired or been removed. Please verify the link with the retailer.",
    ),
    "http_error": (
        "An error occurred while accessing the site.",
        "Please try again in a moment.",
    ),
    "unknown": (
        "An error occurred while accessing the site.",
        "Please try again in a moment.",
    ),
}


def _err(message, hint, url):
    return {"status": "error", "message": message, "hint": hint, "url": url}


@celery.task(bind=True, name="app.tasks.analyze_bike")
def analyze_bike_task(self, url: str, user_id: str | None, ip: str, is_detailed: bool) -> dict:
    # Avoid circular import — reuse the helper from routes
    from .routes import record_bike_price_history

    print(f"[TASK {self.request.id}] analyze start — url={url} user_id={user_id} ip={ip} detailed={is_detailed}")

    # STEP 1: scrape
    try:
        page_text = fetch_html(url)
        print(f"[TASK {self.request.id}] STEP 1 done ({len(page_text)} chars)")
    except ScrapeError as e:
        print(f"[TASK {self.request.id}] STEP 1 failed: {e}")
        msg, hint = SCRAPE_ERRORS.get(e.code, SCRAPE_ERRORS["unknown"])
        return _err(msg, hint, url)

    if not page_text:
        return _err(
            "Could not load the page contents.",
            "This site is not supported right now. Please try the same product on a different retailer's link.",
            url,
        )

    # STEP 2: AI analysis
    exchange_rates = get_exchange_rates()
    try:
        info = extract_bike_info(page_text, exchange_rates=exchange_rates)
        print(f"[TASK {self.request.id}] STEP 2 done: {info['brand']} / {info['model_name']} / {info.get('model_year')}")
    except AnalysisError as e:
        print(f"[TASK {self.request.id}] STEP 2 failed: {e}")
        return _err(
            "Could not identify the bike information.",
            "Please make sure this is a bike product page, or try a page that lists the groupset and model name.",
            url,
        )
    except ServiceBusyError:
        print(f"[TASK {self.request.id}] STEP 2 rate-limit retries exhausted")
        return _err(
            "The service is currently busy.",
            "Please try again in 1-2 minutes.",
            url,
        )

    # STEP 3+: persist to DB
    try:
        bike = Bike.query.filter_by(
            brand=info["brand"],
            model_name=info["model_name"],
            model_year=info.get("model_year"),
        ).first()

        is_new_bike = bike is None
        bike_price_changed = False

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
            new_price = info.get("price_krw")
            if new_price and bike.price_krw != new_price:
                bike.price_krw = new_price
                bike_price_changed = True

        parts = {}
        for key in PART_KEYS:
            if key == "frameset":
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

        if parts["groupset"] is None:
            db.session.rollback()
            return _err(
                "Could not identify the groupset.",
                "Please try a product page that explicitly lists the groupset (brand and model name).",
                url,
            )

        bike.groupset_id = parts["groupset"].id
        bike.wheelset_id = parts["wheelset"].id if parts["wheelset"] else None
        bike.saddle_id = parts["saddle"].id if parts["saddle"] else None
        bike.last_verified_at = datetime.utcnow()

        if is_new_bike:
            db.session.add(bike)
        db.session.flush()  # finalize bike.id

        if is_new_bike and bike.price_krw:
            record_bike_price_history(bike, bike.price_krw)
        elif bike_price_changed:
            record_bike_price_history(bike, bike.price_krw)

        part_list = [p for p in parts.values() if p is not None]
        parts_sum_krw, missing_parts = calculate_parts_sum(part_list)

        for key in PART_KEYS:
            if parts[key] is None and key not in missing_parts:
                missing_parts.append(key)

        bike_price = info.get("price_krw") or bike.price_krw or 0
        saving_krw = parts_sum_krw - bike_price
        saving_pct = round(saving_krw / parts_sum_krw * 100, 1) if parts_sum_krw else 0

        parts_snapshot = {
            key: (str(parts[key].id) if parts.get(key) is not None else None)
            for key in PART_KEYS
        }

        analysis = Analysis(
            bike_id=bike.id,
            parts_sum_krw=parts_sum_krw,
            saving_krw=saving_krw,
            saving_pct=saving_pct,
            missing_parts=missing_parts,
            parts_snapshot=parts_snapshot,
            analyzed_at=datetime.utcnow(),
        )
        db.session.add(analysis)
        db.session.flush()  # finalize analysis.id

        if user_id:
            db.session.add(UserAnalysis(user_id=user_id, analysis_id=analysis.id))

        db.session.add(AnalysisLog(
            ip_address=ip,
            user_id=user_id,
            is_detailed=is_detailed,
        ))
        db.session.commit()

        print(f"[TASK {self.request.id}] done — parts sum {parts_sum_krw:,} KRW / complete bike {bike_price:,} KRW / savings {saving_krw:,} KRW")
        return {"status": "success", "analysis_id": str(analysis.id)}

    except Exception:
        db.session.rollback()
        logger.error("analyze task exception | url=%s\n%s", url, traceback.format_exc())
        return _err(
            "A temporary error occurred.",
            "Please try again in a moment. If it keeps happening, try a different link.",
            url,
        )
