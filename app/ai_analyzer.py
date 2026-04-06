import json
import os
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


SYSTEM_PROMPT = """
당신은 자전거 판매 페이지 텍스트에서 정보를 추출하는 전문가입니다.
아래 JSON 형식으로만 응답하세요. 설명이나 마크다운 없이 JSON만 출력하세요.

추출 규칙:
- brand: 영문 소문자, 공백은 언더스코어 (예: "specialized", "fantasia", "elfama")
- model_name: 원본 표기 그대로 (예: "Radar 9 ARC Gen.3")
- model_year: 정수 (페이지에 없으면 null)
- price_krw: 정수, 할인가 기준 (없으면 null)
- frame_material: "carbon" | "alloy" | "steel" | "titanium" | "other" | "unknown"
- frame_material_confidence: 0.0~1.0 (명시적 언급이면 1.0, 모델명 추론이면 0.7, 추측이면 0.4)
- frame_material_source: "page_text" | "model_knowledge" | "unknown"
- brake_type: "hydraulic_disc" | "mechanical_disc" | "rim" | "unknown"

부품 필드 (각각 part_name + part_name_normalized):
- part_name: 페이지에 적힌 원본 표기
- part_name_normalized: 영문 소문자 + 언더스코어 (예: "shimano_ultegra_di2_r8150")
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


def extract_bike_info(page_text: str) -> dict:
    """
    스크래핑된 페이지 텍스트에서 자전거 정보를 추출한다.

    Returns:
        dict: 추출된 자전거 정보

    Raises:
        AnalysisError: brand / model_name / groupset 중 하나라도 추출 불가 시
    """
    client = _get_client()

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"아래 자전거 판매 페이지 텍스트에서 정보를 추출해주세요.\n\n{page_text}",
            }
        ],
    )

    raw = message.content[0].text.strip()

    # 마크다운 코드블록 제거 (```json ... ``` 또는 ``` ... ```)
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

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

    return data
