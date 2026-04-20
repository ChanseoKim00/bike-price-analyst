"""
분석 작업 Celery task.

/analyze POST는 검증만 하고 이 task를 enqueue → 프론트는 /analyze/status/<task_id>로
폴링. 로고 클릭 시 /analyze/cancel/<task_id>가 revoke(terminate=True)로 워커 프로세스를
종료해 즉시 중단된다.

반환 형식 (Celery result backend에 저장, status 엔드포인트가 그대로 내려보냄):
  성공:  {"status": "success", "analysis_id": "<uuid>"}
  실패:  {"status": "error", "message": "...", "hint": "...", "url": "..."}

취소(revoke)된 경우 task 자체가 REVOKED 상태가 되고, 본 함수는 정상 반환값을 남기지 않음.
"""
import logging
import traceback
from datetime import datetime

from celery import shared_task

from .ai_analyzer import AnalysisError, ServiceBusyError, extract_bike_info
from .exchange_rate import get_exchange_rates
from .models import Analysis, AnalysisLog, Bike, UserAnalysis, db
from .price_calculator import calculate_parts_sum, get_or_fetch_part
from .scraper import ScrapeError, fetch_html

logger = logging.getLogger(__name__)

# 부품 키 — routes.PART_KEYS와 동일 순서
PART_KEYS = ["groupset", "wheelset", "frameset", "saddle", "handlebar"]

# 스크래핑 에러 코드별 (메시지, 힌트) — routes.SCRAPE_ERRORS와 동기화
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


def _err(message, hint, url):
    return {"status": "error", "message": message, "hint": hint, "url": url}


@shared_task(bind=True, name="app.tasks.analyze_bike")
def analyze_bike_task(self, url: str, user_id: str | None, ip: str, is_detailed: bool) -> dict:
    # 순환 import 방지 — routes 안의 헬퍼를 여기서 재사용
    from .routes import record_bike_price_history

    print(f"[TASK {self.request.id}] 분석 시작 — url={url} user_id={user_id} ip={ip} detailed={is_detailed}")

    # STEP 1: 스크래핑
    try:
        page_text = fetch_html(url)
        print(f"[TASK {self.request.id}] STEP 1 완료 ({len(page_text)}자)")
    except ScrapeError as e:
        print(f"[TASK {self.request.id}] STEP 1 실패: {e}")
        msg, hint = SCRAPE_ERRORS.get(e.code, SCRAPE_ERRORS["unknown"])
        return _err(msg, hint, url)

    if not page_text:
        return _err(
            "페이지 정보를 불러올 수 없습니다.",
            "해당 사이트는 현재 지원하지 않습니다. 다른 판매처의 동일 제품 링크로 다시 시도해주세요.",
            url,
        )

    # STEP 2: AI 분석
    exchange_rates = get_exchange_rates()
    try:
        info = extract_bike_info(page_text, exchange_rates=exchange_rates)
        print(f"[TASK {self.request.id}] STEP 2 완료: {info['brand']} / {info['model_name']} / {info.get('model_year')}")
    except AnalysisError as e:
        print(f"[TASK {self.request.id}] STEP 2 실패: {e}")
        return _err(
            "자전거 정보를 확인할 수 없습니다.",
            "자전거 판매 페이지가 맞는지 확인하거나, 구동계·모델명이 명시된 다른 페이지로 시도해주세요.",
            url,
        )
    except ServiceBusyError:
        print(f"[TASK {self.request.id}] STEP 2 Rate limit 재시도 실패")
        return _err(
            "현재 서비스가 혼잡합니다.",
            "1~2분 후 다시 시도해주세요.",
            url,
        )

    # STEP 3+: DB 반영
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
                "구동계 정보를 확인할 수 없습니다.",
                "구동계(브랜드·모델명)가 명시된 판매 페이지 링크로 다시 시도해주세요.",
                url,
            )

        bike.groupset_id = parts["groupset"].id
        bike.wheelset_id = parts["wheelset"].id if parts["wheelset"] else None
        bike.saddle_id = parts["saddle"].id if parts["saddle"] else None
        bike.last_verified_at = datetime.utcnow()

        if is_new_bike:
            db.session.add(bike)
        db.session.flush()  # bike.id 확정

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
        db.session.flush()  # analysis.id 확정

        if user_id:
            db.session.add(UserAnalysis(user_id=user_id, analysis_id=analysis.id))

        db.session.add(AnalysisLog(
            ip_address=ip,
            user_id=user_id,
            is_detailed=is_detailed,
        ))
        db.session.commit()

        print(f"[TASK {self.request.id}] 완료 — 부품합산 {parts_sum_krw:,}원 / 완성차 {bike_price:,}원 / 절약 {saving_krw:,}원")
        return {"status": "success", "analysis_id": str(analysis.id)}

    except Exception:
        db.session.rollback()
        logger.error("분석 task 예외 | url=%s\n%s", url, traceback.format_exc())
        return _err(
            "일시적인 오류가 발생했습니다.",
            "잠시 후 다시 시도해주세요. 문제가 반복되면 다른 링크로 시도해보세요.",
            url,
        )
