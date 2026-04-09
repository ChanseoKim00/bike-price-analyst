import logging
import traceback
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlparse
from flask import Blueprint, render_template, request

logger = logging.getLogger(__name__)

from .models import db, Bike, Analysis
from .scraper import fetch_html, ScrapeError
from .ai_analyzer import extract_bike_info, AnalysisError, ServiceBusyError
from .price_calculator import get_or_fetch_part, calculate_parts_sum

bp = Blueprint("main", __name__)

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


def _err(message, hint, url=""):
    """에러 페이지 렌더링 헬퍼"""
    return render_template("error.html", message=message, hint=hint, url=url)


@bp.route("/")
def index():
    return render_template("index.html")


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
    return render_template("index.html", bike=bike, parts=parts, analysis=analysis, bike_price=6_822_000)


@bp.route("/preview/error")
def preview_error():
    return render_template("error.html", url="")


@bp.route("/suggest")
def suggest():
    return render_template("coming_soon.html")


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

    print(f"[ANALYZE] 요청 URL: {url}")

    # STEP 1: 스크래핑
    print("[STEP 1] 스크래핑 시작...")
    try:
        page_text = fetch_html(url)
        print(f"[STEP 1] 완료 ({len(page_text)}자)")
    except ScrapeError as e:
        print(f"[STEP 1] 실패: {e}")
        msg, hint = SCRAPE_ERRORS.get(e.code, SCRAPE_ERRORS["unknown"])
        return _err(msg, hint, url=url)

    # STEP 2: AI 분석
    print("[STEP 2] AI 분석 시작...")
    try:
        info = extract_bike_info(page_text)
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

        # STEP 4: 부품 조회 (세션에 bike 추가 전에 실행 — autoflush 방지)
        parts = {}
        for key in PART_KEYS:
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

    return render_template(
        "index.html",
        bike=bike,
        parts=parts,
        analysis=analysis,
        bike_price=bike_price,
    )
