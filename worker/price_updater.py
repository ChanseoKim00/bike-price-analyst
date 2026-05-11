"""
Auto price-refresh worker.

What it does:
  1) For every row in `parts` whose TTL has expired, re-query via AI web search.
     → If the price changed, update `parts` and write a row into `part_price_history`.
  2) For every row in `bikes` with `stale=True`, re-scrape from `official_url`.
     → If the price changed, update `bikes` and write a row into `bike_price_history`.

Run:
  python -m worker.price_updater

Scheduled in Railway Cron Job for every Monday 03:00 KST (Monday 18:00 UTC).
"""
import os
import sys
import time
import traceback
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

# Add the project root to sys.path so `python worker/price_updater.py` works directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.models import db, Part, Bike
from app.price_calculator import (
    _search_price_with_ai,
    record_part_price_history,
    SKIP_AI_SEARCH_TYPES,
    RETRY_ON_NULL_TYPES,
    TTL_DAYS,
)
from app.routes import record_bike_price_history
from app.scraper import fetch_html, ScrapeError
from app.ai_analyzer import extract_bike_info, AnalysisError, ServiceBusyError
from app.exchange_rate import get_exchange_rates


def _ttl_expired(part: Part) -> bool:
    """Whether the part has expired per part.ttl_days, measured from last_verified_at."""
    if part.last_verified_at is None:
        return True
    ttl = timedelta(days=part.ttl_days or TTL_DAYS.get(part.part_type, 90))
    return datetime.utcnow() - part.last_verified_at >= ttl


def update_parts() -> dict:
    """Re-query TTL-expired parts. Returns an aggregate stats dict."""
    stats = {"total": 0, "skipped": 0, "unchanged": 0, "updated": 0, "failed": 0}

    # frameset is not subject to AI search → exclude it
    parts = (
        Part.query
        .filter(~Part.part_type.in_(SKIP_AI_SEARCH_TYPES))
        .all()
    )
    expired = [p for p in parts if _ttl_expired(p)]
    stats["total"] = len(expired)
    print(f"[PARTS] TTL-expired candidates: {len(expired)}")

    for idx, part in enumerate(expired, 1):
        # null price and a type we don't re-search → skip
        if part.price_krw is None and part.part_type not in RETRY_ON_NULL_TYPES:
            stats["skipped"] += 1
            print(f"  [{idx}/{len(expired)}] SKIP {part.part_type}: {part.part_name} (kept null)")
            continue

        try:
            result = _search_price_with_ai(part.part_name, part.part_type)
        except Exception as e:
            stats["failed"] += 1
            print(f"  [{idx}/{len(expired)}] FAIL {part.part_type}: {part.part_name} — {e}")
            continue

        now = datetime.utcnow()
        part.last_checked_at = now

        new_price = result.get("price_krw")
        if not new_price:
            # Search miss — keep the existing price
            stats["failed"] += 1
            db.session.commit()
            print(f"  [{idx}/{len(expired)}] MISS {part.part_type}: {part.part_name} (search miss)")
            continue

        if part.price_krw == new_price:
            part.last_verified_at = now  # same price — only refresh the verified timestamp
            # Stamp a confirmation row so the price-history graph stays continuous even with no change
            record_part_price_history(part, new_price, recorded_at=now, force=True)
            db.session.commit()
            stats["unchanged"] += 1
            print(f"  [{idx}/{len(expired)}] KEEP {part.part_type}: {part.part_name} ({new_price:,} KRW)")
            continue

        old_price = part.price_krw
        part.price_krw = new_price
        part.official_url = result.get("official_url") or part.official_url
        part.last_verified_at = now
        record_part_price_history(part, new_price, recorded_at=now)
        db.session.commit()
        stats["updated"] += 1
        old_str = f"{old_price:,} KRW" if old_price else "null"
        print(f"  [{idx}/{len(expired)}] UPDATE {part.part_type}: {part.part_name} ({old_str} → {new_price:,} KRW)")

        # Short pause to stay under rate limits
        time.sleep(1)

    return stats


def update_bikes() -> dict:
    """Re-query bikes where stale=True. Returns an aggregate stats dict."""
    stats = {"total": 0, "unchanged": 0, "updated": 0, "failed": 0}

    bikes = Bike.query.filter_by(stale=True).all()
    stats["total"] = len(bikes)
    print(f"[BIKES] stale=True candidates: {len(bikes)}")

    exchange_rates = get_exchange_rates() if bikes else None

    for idx, bike in enumerate(bikes, 1):
        if not bike.official_url:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] FAIL {bike.brand} {bike.model_name}: missing official_url")
            continue

        try:
            page_text = fetch_html(bike.official_url)
        except ScrapeError as e:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] FAIL {bike.brand} {bike.model_name}: scrape failed ({e.code})")
            continue

        if not page_text:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] FAIL {bike.brand} {bike.model_name}: empty page body")
            continue

        try:
            info = extract_bike_info(page_text, exchange_rates=exchange_rates)
        except (AnalysisError, ServiceBusyError) as e:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] FAIL {bike.brand} {bike.model_name}: AI analysis failed ({e})")
            continue

        new_price = info.get("price_krw")
        if not new_price:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] MISS {bike.brand} {bike.model_name}: price extraction failed")
            continue

        if bike.price_krw == new_price:
            # Stamp a confirmation row so the price-history graph stays continuous even with no change
            record_bike_price_history(bike, new_price, force=True)
            db.session.commit()
            stats["unchanged"] += 1
            print(f"  [{idx}/{len(bikes)}] KEEP {bike.brand} {bike.model_name} ({new_price:,} KRW)")
            continue

        old_price = bike.price_krw
        bike.price_krw = new_price
        bike.last_verified_at = datetime.utcnow()
        record_bike_price_history(bike, new_price, recorded_at=bike.last_verified_at)
        db.session.commit()
        stats["updated"] += 1
        old_str = f"{old_price:,} KRW" if old_price else "null"
        print(f"  [{idx}/{len(bikes)}] UPDATE {bike.brand} {bike.model_name} ({old_str} → {new_price:,} KRW)")

        time.sleep(1)

    return stats


def main() -> int:
    started = datetime.utcnow()
    print(f"[START] price_updater — {started.isoformat()} UTC")

    app = create_app()
    with app.app_context():
        try:
            parts_stats = update_parts()
            bikes_stats = update_bikes()
        except Exception:
            traceback.print_exc()
            return 1

    elapsed = (datetime.utcnow() - started).total_seconds()
    print(
        f"[DONE] elapsed={elapsed:.1f}s | "
        f"parts total={parts_stats['total']} "
        f"updated={parts_stats['updated']} unchanged={parts_stats['unchanged']} "
        f"skipped={parts_stats['skipped']} failed={parts_stats['failed']} | "
        f"bikes total={bikes_stats['total']} "
        f"updated={bikes_stats['updated']} unchanged={bikes_stats['unchanged']} "
        f"failed={bikes_stats['failed']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
