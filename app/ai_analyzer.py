import json
import os
import time
from datetime import datetime
import anthropic

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


class AnalysisError(Exception):
    """필수 항목(brand / model_name / groupset) 추출 실패 시 → 케이스 6 처리용"""
    pass


class ServiceBusyError(Exception):
    """RateLimitError 재시도 후에도 실패 시 → 에러 페이지 처리용"""
    pass


SYSTEM_PROMPT = """
당신은 자전거 판매 페이지 텍스트에서 정보를 추출하는 전문가입니다.
아래 JSON 형식으로만 응답하세요. 설명이나 마크다운 없이 JSON만 출력하세요.

추출 규칙:
- brand: 영문 소문자, 공백은 언더스코어 (예: "specialized", "fantasia", "elfama")
- model_name: 원본 표기 그대로 (예: "Radar 9 ARC Gen.3")
- model_year: 정수. 아래 순서로 찾아라:
  1) 페이지에 연식이 명시된 경우 (예: "2025년형", "2026 model")
  2) 출시 연월 정보 (예: "2025-04 출시")
  3) 리뷰/댓글/문의 날짜 중 가장 이른 연도로 유추 (예: 2025년 댓글 → 2025)
  4) 위 모두 없으면 null
- price_krw: 정수, 할인가 기준 (없으면 null)
- frame_material: "carbon" | "alloy" | "steel" | "titanium" | "other" | "unknown"
- frame_material_confidence: 0.0~1.0 (명시적 언급이면 1.0, 모델명 추론이면 0.7, 추측이면 0.4)
- frame_material_source: "page_text" | "model_knowledge" | "unknown"
- brake_type: "hydraulic_disc" | "mechanical_disc" | "rim" | "unknown"

부품 필드 (각각 part_name + part_name_normalized):
- part_name: 페이지에 적힌 원본 표기
- part_name_normalized: 영문 소문자 + 언더스코어. 브랜드 + 제품 라인 + 전동/기계식 구분까지만 포함.
  모델 번호(R9200, R9250, R8150, R7100 등)와 파생 옵션(파워미터, 크랭크 세트 등)은 제외.
  예: "shimano_dura_ace_di2", "shimano_ultegra_di2", "shimano_105_di2", "shimano_105",
      "sram_red_etap_axs", "sram_force_etap_axs", "campagnolo_super_record"
  주의: R9200과 R9250은 모두 "shimano_dura_ace_di2"로 정규화. 파워미터 포함 여부와 무관하게 동일하게 정규화.
- 페이지에 명시되지 않은 부품은 null

{
  "brand": "string",
  "model_name": "string",
  "model_year": integer or null,
  "price_krw": integer or null,
  "frame_material": "string",
  "frame_material_confidence": float,
  "frame_material_source": "string",
  "brake_type": "string",
  "groupset": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  },
  "wheelset": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  },
  "frameset": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  },
  "saddle": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  },
  "handlebar": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  }
}
""".strip()


YEAR_RETRY_PROMPT = """
이전 분석에서 model_year를 찾지 못했습니다.
아래 페이지 텍스트에서 연식을 다시 찾아주세요.

찾는 순서:
1. "2025년형", "2026 model" 등 명시적 연식 표기
2. 출시 연월 (예: "2025-04 출시" → 2025)
3. 리뷰/댓글/문의 날짜 중 가장 이른 연도로 유추

찾은 연식을 정수 하나만 출력하세요. 못 찾으면 null을 출력하세요.
숫자 또는 null 외에 다른 텍스트는 출력하지 마세요.
""".strip()


def _call_api(client, system: str, user: str) -> str:
    for attempt in range(2):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            raw = message.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return raw
        except anthropic.APIStatusError as e:
            if e.status_code not in (429, 529):
                raise
            if attempt == 0:
                print(f"[RATE LIMIT] AI 분석 {e.status_code} — 60초 대기 후 재시도")
                time.sleep(60)
            else:
                raise ServiceBusyError("일시적으로 서비스가 혼잡합니다. 잠시 후 다시 시도해주세요.")


def extract_bike_info(page_text: str) -> dict:
    """
    스크래핑된 페이지 텍스트에서 자전거 정보를 추출한다.
    model_year가 null이면 한 번 더 시도하고, 그래도 없으면 현재 연도를 기본값으로 사용.

    Returns:
        dict: 추출된 자전거 정보 (model_year는 항상 정수)

    Raises:
        AnalysisError: brand / model_name / groupset 중 하나라도 추출 불가 시
    """
    client = _get_client()

    raw = _call_api(
        client,
        system=SYSTEM_PROMPT,
        user=f"아래 자전거 판매 페이지 텍스트에서 정보를 추출해주세요.\n\n{page_text}",
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise AnalysisError(f"AI 응답을 JSON으로 파싱할 수 없습니다: {raw[:200]}")

    # 필수 항목 검증
    missing = []
    if not data.get("brand"):
        missing.append("brand")
    if not data.get("model_name"):
        missing.append("model_name")
    if not data.get("groupset", {}).get("part_name"):
        missing.append("groupset")

    if missing:
        raise AnalysisError(f"필수 항목 추출 실패: {', '.join(missing)}")

    # model_year 처리: null이면 재시도 → 그래도 없으면 현재 연도
    if not data.get("model_year"):
        retry_raw = _call_api(
            client,
            system=YEAR_RETRY_PROMPT,
            user=page_text,
        )
        try:
            year = int(retry_raw)
            data["model_year"] = year
        except (ValueError, TypeError):
            data["model_year"] = datetime.utcnow().year

    return data
