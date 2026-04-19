"""
가격 자동 갱신 워커

실행 내용:
  1) parts 테이블에서 TTL 만료된 row 전체를 AI 웹 검색으로 재조회
     → 가격 변동 있으면 parts 업데이트 + part_price_history 기록
  2) bikes 테이블에서 stale=True인 row를 official_url 재스크랩으로 재조회
     → 가격 변동 있으면 bikes 업데이트 + bike_price_history 기록

실행:
  python -m worker.price_updater

Railway Cron Job에서 매주 월요일 03:00 KST (UTC 월요일 18:00) 로 예약.
"""
import os
import sys
import time
import traceback
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

# 프로젝트 루트를 sys.path에 추가 — `python worker/price_updater.py` 형태 직접 실행 대응
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
    """part.ttl_days 기준 만료 여부 — last_verified_at 기준."""
    if part.last_verified_at is None:
        return True
    ttl = timedelta(days=part.ttl_days or TTL_DAYS.get(part.part_type, 90))
    return datetime.utcnow() - part.last_verified_at >= ttl


def update_parts() -> dict:
    """TTL 만료 parts 재조회. 반환: 집계 dict."""
    stats = {"total": 0, "skipped": 0, "unchanged": 0, "updated": 0, "failed": 0}

    # frameset은 AI 검색 대상 아님 → 제외
    parts = (
        Part.query
        .filter(~Part.part_type.in_(SKIP_AI_SEARCH_TYPES))
        .all()
    )
    expired = [p for p in parts if _ttl_expired(p)]
    stats["total"] = len(expired)
    print(f"[PARTS] TTL 만료 대상: {len(expired)}개")

    for idx, part in enumerate(expired, 1):
        # null 가격이고 재검색 안 하는 타입 → 스킵
        if part.price_krw is None and part.part_type not in RETRY_ON_NULL_TYPES:
            stats["skipped"] += 1
            print(f"  [{idx}/{len(expired)}] SKIP {part.part_type}: {part.part_name} (null 유지)")
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
            # 검색 실패 — 기존 가격 유지
            stats["failed"] += 1
            db.session.commit()
            print(f"  [{idx}/{len(expired)}] MISS {part.part_type}: {part.part_name} (검색 실패)")
            continue

        if part.price_krw == new_price:
            part.last_verified_at = now  # 가격 동일 — 검증일만 갱신
            # 그래프 연속성을 위해 변동 없어도 이력에 확인 도장 찍기
            record_part_price_history(part, new_price, recorded_at=now, force=True)
            db.session.commit()
            stats["unchanged"] += 1
            print(f"  [{idx}/{len(expired)}] KEEP {part.part_type}: {part.part_name} ({new_price:,}원)")
            continue

        old_price = part.price_krw
        part.price_krw = new_price
        part.official_url = result.get("official_url") or part.official_url
        part.last_verified_at = now
        record_part_price_history(part, new_price, recorded_at=now)
        db.session.commit()
        stats["updated"] += 1
        old_str = f"{old_price:,}원" if old_price else "null"
        print(f"  [{idx}/{len(expired)}] UPDATE {part.part_type}: {part.part_name} ({old_str} → {new_price:,}원)")

        # 레이트 리밋 보호용 짧은 대기
        time.sleep(1)

    return stats


def update_bikes() -> dict:
    """stale=True bikes 재조회. 반환: 집계 dict."""
    stats = {"total": 0, "unchanged": 0, "updated": 0, "failed": 0}

    bikes = Bike.query.filter_by(stale=True).all()
    stats["total"] = len(bikes)
    print(f"[BIKES] stale=True 대상: {len(bikes)}개")

    exchange_rates = get_exchange_rates() if bikes else None

    for idx, bike in enumerate(bikes, 1):
        if not bike.official_url:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] FAIL {bike.brand} {bike.model_name}: official_url 없음")
            continue

        try:
            page_text = fetch_html(bike.official_url)
        except ScrapeError as e:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] FAIL {bike.brand} {bike.model_name}: 스크랩 실패 ({e.code})")
            continue

        if not page_text:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] FAIL {bike.brand} {bike.model_name}: 페이지 본문 없음")
            continue

        try:
            info = extract_bike_info(page_text, exchange_rates=exchange_rates)
        except (AnalysisError, ServiceBusyError) as e:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] FAIL {bike.brand} {bike.model_name}: AI 분석 실패 ({e})")
            continue

        new_price = info.get("price_krw")
        if not new_price:
            stats["failed"] += 1
            print(f"  [{idx}/{len(bikes)}] MISS {bike.brand} {bike.model_name}: 가격 추출 실패")
            continue

        if bike.price_krw == new_price:
            # 그래프 연속성을 위해 변동 없어도 이력에 확인 도장 찍기
            record_bike_price_history(bike, new_price, force=True)
            db.session.commit()
            stats["unchanged"] += 1
            print(f"  [{idx}/{len(bikes)}] KEEP {bike.brand} {bike.model_name} ({new_price:,}원)")
            continue

        old_price = bike.price_krw
        bike.price_krw = new_price
        bike.last_verified_at = datetime.utcnow()
        record_bike_price_history(bike, new_price, recorded_at=bike.last_verified_at)
        db.session.commit()
        stats["updated"] += 1
        old_str = f"{old_price:,}원" if old_price else "null"
        print(f"  [{idx}/{len(bikes)}] UPDATE {bike.brand} {bike.model_name} ({old_str} → {new_price:,}원)")

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
