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
        return render_template("error.html", message="URL을 입력해주세요.", url=url)
    if len(url) > 2000:
        return render_template("error.html", message="URL이 너무 깁니다.", url="")
    if urlparse(url).scheme not in ("http", "https"):
        return render_template("error.html", message="올바른 URL을 입력해주세요. http:// 또는 https://로 시작하는 자전거 판매 페이지 링크가 필요합니다.", url="")

    print(f"[ANALYZE] 요청 URL: {url}")

    # STEP 1: 스크래핑
    print("[STEP 1] 스크래핑 시작...")
    try:
        page_text = fetch_html(url)
        print(f"[STEP 1] 완료 ({len(page_text)}자)")
    except ScrapeError as e:
        print(f"[STEP 1] 실패: {e}")
        return render_template("error.html", message=str(e), url=url)

    # STEP 2: AI 분석
    print("[STEP 2] AI 분석 시작...")
    try:
        info = extract_bike_info(page_text)
        print(f"[STEP 2] 완료: {info['brand']} / {info['model_name']} / {info.get('model_year')}")
    except AnalysisError as e:
        print(f"[STEP 2] 실패: {e}")
        return render_template("error.html", message=str(e), url=url)
    except ServiceBusyError as e:
        print(f"[STEP 2] Rate limit 재시도 실패: {e}")
        return render_template("error.html", message=str(e), url=url)

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
            return render_template("error.html", message="구동계 정보를 확인할 수 없습니다.", url=url)

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
        return render_template("error.html", message="분석 중 오류가 발생했습니다.", url=url)

    return render_template(
        "index.html",
        bike=bike,
        parts=parts,
        analysis=analysis,
        bike_price=bike_price,
    )
