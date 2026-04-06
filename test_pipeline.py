"""
백엔드 end-to-end 테스트 (템플릿 없이 터미널 출력)
실행: python3 test_pipeline.py <URL>
"""
import sys
from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.models import db
from app.scraper import fetch_html, ScrapeError
from app.ai_analyzer import extract_bike_info, AnalysisError
from app.price_calculator import get_or_fetch_part, calculate_parts_sum
from app.models import Bike, Analysis
from datetime import datetime

PART_KEYS = ["groupset", "wheelset", "frameset", "saddle", "handlebar"]

def run(url):
    app = create_app()
    with app.app_context():
        print(f"\n{'='*60}")
        print(f"URL: {url}")
        print('='*60)

        # STEP 1
        print("\n[STEP 1] 스크래핑 중...")
        try:
            page_text = fetch_html(url)
            print(f"  → 성공 ({len(page_text)}자)")
        except ScrapeError as e:
            print(f"  → 실패: {e}")
            return

        # STEP 2
        print("\n[STEP 2] AI 분석 중...")
        try:
            info = extract_bike_info(page_text)
            print(f"  → 브랜드: {info['brand']}")
            print(f"  → 모델명: {info['model_name']}")
            print(f"  → 연식:   {info.get('model_year')}")
            print(f"  → 가격:   {info.get('price_krw'):,}원" if info.get('price_krw') else "  → 가격:   없음")
            print(f"  → 프레임: {info.get('frame_material')} (신뢰도 {info.get('frame_material_confidence')})")
            print(f"  → 브레이크: {info.get('brake_type')}")
            for key in PART_KEYS:
                p = info.get(key, {})
                if p and p.get("part_name"):
                    print(f"  → {key:10s}: {p['part_name']} ({p['part_name_normalized']})")
        except AnalysisError as e:
            print(f"  → 실패: {e}")
            return

        # STEP 3
        print("\n[STEP 3] bikes 테이블 확인...")
        bike = Bike.query.filter_by(
            brand=info["brand"],
            model_name=info["model_name"],
            model_year=info.get("model_year"),
        ).first()
        is_new_bike = bike is None
        if bike:
            print(f"  → 기존 레코드 발견 (id: {bike.id})")
        else:
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
            print(f"  → 신규 생성 예정 (parts 세팅 후 저장)")

        # STEP 4
        print("\n[STEP 4] 부품 가격 조회 중...")
        parts = {}
        for key in PART_KEYS:
            part_info = info.get(key, {})
            if not part_info or not part_info.get("part_name"):
                parts[key] = None
                print(f"  → {key:10s}: 정보 없음 (missing)")
                continue
            parts[key] = get_or_fetch_part(
                part_name=part_info["part_name"],
                part_name_normalized=part_info["part_name_normalized"],
                part_type=key,
            )
            p = parts[key]
            if p is None:
                print(f"  → {key:10s}: DB에 없음 (missing) — {part_info['part_name']}")
            elif p.price_krw:
                print(f"  → {key:10s}: {p.price_krw:,}원 ({p.official_url or 'URL 없음'})")
            else:
                print(f"  → {key:10s}: 가격 없음 ({part_info['part_name']})")

        if parts["groupset"] is None:
            print("\n  [케이스 6] 구동계 정보 없음 → 분석 불가")
            return

        bike.groupset_id = parts["groupset"].id
        bike.wheelset_id = parts["wheelset"].id if parts["wheelset"] else None
        bike.saddle_id = parts["saddle"].id if parts["saddle"] else None
        bike.last_verified_at = datetime.utcnow()

        if is_new_bike:
            db.session.add(bike)
        db.session.flush()  # bike.id 확정 (groupset_id 세팅 완료 후라 안전)

        # STEP 5
        print("\n[STEP 5] 가격 계산...")
        part_list = [p for p in parts.values() if p is not None]
        parts_sum_krw, missing_parts = calculate_parts_sum(part_list)
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

        print(f"\n{'='*60}")
        print(f"  분석 결과: {info['brand']} {info['model_name']}")
        print(f"{'='*60}")
        print(f"  완성차 가격:     {bike_price:>12,}원")
        print(f"  부품 합산 가격:  {parts_sum_krw:>12,}원")
        print(f"  절약 금액:       {saving_krw:>12,}원  ({saving_pct}%)")
        if missing_parts:
            print(f"  비교 제외 부품:  {', '.join(missing_parts)}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://mbscorp.co.kr/prod/prod_detail.html?base_seq=KXLYZp9y8tOmOzk_EWWBoQ"
    run(url)
