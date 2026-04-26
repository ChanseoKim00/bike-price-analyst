"""
이미지 전용 상세페이지(사양이 이미지로만 표시되는 한국 쇼핑몰)에서
자전거 부품 사양을 추출하는 모듈.

ai_analyzer.py와 호환되는 JSON 스키마로 부품 5종(frameset, groupset, wheelset,
saddle, handlebar) + frame_material + brake_type을 출력한다.

사용 흐름 (routes/worker에서):
    text, html = scraper.fetch_html_with_raw(url)
    use_image, image_urls = should_use_image_mode(text, html, url)
    if use_image:
        spec = extract_specs_from_images(image_urls)
        # spec의 부품 슬롯들로 ai_analyzer 결과 덮어쓰기
"""
import base64
import hashlib
import io
import json
import os
import time
from urllib.parse import urljoin

import anthropic
import requests
from bs4 import BeautifulSoup
from PIL import Image

try:
    from app.ai_analyzer import ServiceBusyError
except ImportError:
    # 단독 import (Flask 컨텍스트 외) 시 폴백
    class ServiceBusyError(Exception):
        pass


# 이미지 모드 분기 임계치
# 키워드 휴리스틱은 사이트 사이드바/메뉴에 부품 카테고리명("휠셋", "프레임" 등)이
# 그대로 노출되는 케이스에서 false positive가 너무 많아 폐기.
# 대신 ai_analyzer의 실제 추출 결과를 보고 빈 슬롯이 임계치 이상이면 이미지 모드로 보강.
PART_SLOTS = ("frameset", "groupset", "wheelset", "saddle", "handlebar")
NULL_SLOTS_THRESHOLD = 2       # 부품 슬롯 N개 이상 null이면 이미지 모드
IMAGE_COUNT_THRESHOLD = 3      # 동시에 /editor/ 이미지 M장 이상
MAX_IMAGES_TO_SEND = 8         # 토큰 비용 컨트롤

# 이미지 처리
CLAUDE_MAX_BYTES = 5 * 1024 * 1024
TARGET_LONG_EDGE = 1568

# Confidence 컷오프 — 미만 슬롯은 null 강제
CONFIDENCE_FLOOR = 0.7

VISION_MODEL = "claude-sonnet-4-6"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


EXTRACT_PROMPT = """
당신은 자전거 상세페이지 이미지에서 부품 사양을 추출하는 전문가입니다.
첨부된 이미지들은 한 자전거 모델의 상세페이지 이미지(사양표 포함)입니다.

[추출 철학 — 매우 중요]
세부 부품(변속레버, 앞/뒷변속기, 크랭크, 카세트, 체인, 브레이크 등)을 슬롯별로 채우려 하지 마세요.
대신 사양표에 보이는 모든 부품 단서를 모아서, 다음 5개 슬롯으로 합쳐 추론하세요:
  frameset / groupset / wheelset / saddle / handlebar

특히 groupset은 구동계 라인업 하나로 추론합니다:
  - "FC-R8100" + "RD-R8150" + "BR-R8170" 단서 → "Shimano Ultegra Di2"
  - "ST-R7170" 단서만 봐도 → "Shimano 105 Di2"
  - 모델 번호(R9200, R9250, R8150 등)는 라인업명으로 환원

[Shimano 라인업 추론 표]
  R9200/R9250 = Dura-Ace Di2
  R9100/R9150 = Dura-Ace (R9100=기계식, R9150=Di2)
  R8100/R8150/R8170 = Ultegra Di2 (R8100=기계식, R8150/R8170=Di2)
  R7100/R7150/R7170 = 105 Di2 (R7100=기계식, R7150/R7170=Di2)
  R7000 = 105 (구세대 11단)

[SRAM 라인업]
  Red eTap AXS, Force eTap AXS, Rival eTap AXS, Apex eTap AXS

[part_name_normalized 규칙 — ai_analyzer.py와 일치]
영문 소문자 + 언더스코어(_)만. 띄어쓰기/하이픈/대문자 절대 금지.
  groupset 예시:
    "shimano_dura_ace_di2", "shimano_ultegra_di2", "shimano_105_di2",
    "sram_red_etap_axs", "sram_force_etap_axs"
  포함: 브랜드 + 라인업 + 전동/기계식 구분(di2)
  제외: 모델 번호(R9200 등), 'rail', 'system', 'integrated' 같은 접미어,
        'Tubeless', 'TLR' 같은 호환 표기

[Fizik 안장 normalized 규칙]
  fizik_(카테고리)_(라인업)_(레일등급)_(adaptive 여부)
  카테고리: vento / tempo / transiro — 명시 없으면 vento
  라인업: argo / aeris / antares — 명시 없으면 argo
  레일등급: 00 / r1 / r3 / r5 — 명시 없으면 r5

아래 JSON 스키마로만 응답하세요. 마크다운/설명 금지, JSON만 출력.

{
  "frameset": {
    "part_name": null 또는 "원본 표기",
    "part_name_normalized": null 또는 "정규화 영문",
    "evidence": "어느 이미지 어디서 어떤 단서로 추출했는지"
  },
  "groupset": {
    "part_name": null 또는 "원본 표기 또는 라인업명",
    "part_name_normalized": null 또는 "예: shimano_ultegra_di2",
    "evidence": "수집한 단서 나열"
  },
  "wheelset": { ... 동일 구조 ... },
  "saddle": { ... 동일 구조 ... },
  "handlebar": { ... 동일 구조 ... },
  "frame_material": "carbon" | "alloy" | "steel" | "titanium" | "other" | "unknown",
  "brake_type": "hydraulic_disc" | "mechanical_disc" | "rim" | "unknown",
  "_confidence": {
    "frameset": 0.0~1.0, "groupset": 0.0~1.0, "wheelset": 0.0~1.0,
    "saddle": 0.0~1.0, "handlebar": 0.0~1.0,
    "frame_material": 0.0~1.0, "brake_type": 0.0~1.0
  },
  "_evidence_image_indices": [근거 이미지 인덱스],
  "_notes": "특이사항(없으면 빈 문자열)"
}

[환각 방지 규칙]
1. 모르면 null. 그럴듯하게 추측 금지.
   - 텍스트가 흐리면 null
   - 일부 글자만 보이면 null
   - 한글 OCR이 어색하면 null
2. 단서 cross-reference 적극 활용:
   - 어느 한 부품 코드만 또렷이 읽혀도 라인업 추론 가능
   - 단서들이 서로 모순되면 → null + _notes에 명시
3. _confidence 기준:
   - 0.9+: 또렷한 라인업명 또는 모델 번호 직접 읽음
   - 0.7~0.9: 부품 코드 단서로 라인업 추론, 추론은 명확
   - 0.5~0.7: 추론이지만 단서 일부 흐림
   - 0.5 미만: value를 null로 두고 confidence는 0.0
4. 디자인/색상 사진은 무시.
""".strip()


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def extract_detail_images(html: str, base_url: str) -> list:
    """본문 영역 /editor/ 경로 이미지를 사양 후보로 추출."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    seen = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src or "/editor/" not in src:
            continue
        full = urljoin(base_url, src)
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)
    return urls


def count_null_part_slots(ai_result: dict) -> int:
    """ai_analyzer 결과에서 비어있는 부품 슬롯 개수."""
    n = 0
    for slot in PART_SLOTS:
        s = ai_result.get(slot) or {}
        if not s.get("part_name"):
            n += 1
    return n


def should_use_image_mode(ai_result: dict, raw_html: str, base_url: str):
    """
    ai_analyzer 결과를 보고 이미지 모드로 보강할지 판단.

    Args:
        ai_result: ai_analyzer.extract_bike_info() 결과 (부분 결과여도 OK).
                   AnalysisError로 실패한 경우엔 호출 측이 빈 dict나 부분 dict를 넘길 수 있음.
        raw_html: scraper가 받은 raw HTML (정제 전).
        base_url: 페이지 URL — 이미지 절대경로 변환용.

    Returns:
        (use_image: bool, image_urls: list, reason: str)
    """
    null_slots = count_null_part_slots(ai_result)
    image_urls = extract_detail_images(raw_html, base_url)
    use = null_slots >= NULL_SLOTS_THRESHOLD and len(image_urls) >= IMAGE_COUNT_THRESHOLD
    reason = (
        f"null_slots={null_slots}/{len(PART_SLOTS)} (threshold≥{NULL_SLOTS_THRESHOLD}), "
        f"editor_images={len(image_urls)} (threshold≥{IMAGE_COUNT_THRESHOLD})"
    )
    return use, image_urls, reason


def merge_image_specs_into_ai_result(ai_result: dict, image_specs: dict) -> dict:
    """
    텍스트 모드 결과(ai_analyzer)에 이미지 모드 결과를 병합.
    이미 채워진 슬롯은 유지, 비어있는 슬롯만 이미지 모드 결과로 채움.
    """
    merged = dict(ai_result)
    for slot in PART_SLOTS:
        existing = (merged.get(slot) or {}).get("part_name")
        if not existing and image_specs.get(slot, {}).get("part_name"):
            merged[slot] = {
                "part_name": image_specs[slot]["part_name"],
                "part_name_normalized": image_specs[slot]["part_name_normalized"],
            }
    if (not merged.get("frame_material") or merged.get("frame_material") == "unknown") and \
            image_specs.get("frame_material") and image_specs["frame_material"] != "unknown":
        merged["frame_material"] = image_specs["frame_material"]
        merged["frame_material_source"] = "image_extraction"
        merged["frame_material_confidence"] = image_specs.get("_meta", {}).get(
            "raw_confidence", {}
        ).get("frame_material", 0.7)
    if (not merged.get("brake_type") or merged.get("brake_type") == "unknown") and \
            image_specs.get("brake_type") and image_specs["brake_type"] != "unknown":
        merged["brake_type"] = image_specs["brake_type"]
    merged["_image_meta"] = image_specs.get("_meta", {})
    return merged


def hash_image_urls(image_urls) -> str:
    """정렬된 URL 리스트의 SHA256. TTL 갱신 시 동일성 확인용."""
    joined = "\n".join(sorted(image_urls))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _download_as_b64(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = resp.content

    media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if media_type in ("image/jpeg", "image/png", "image/gif", "image/webp") and len(raw) <= CLAUDE_MAX_BYTES:
        return base64.standard_b64encode(raw).decode("ascii"), media_type

    # 5MB 초과 또는 미지원 포맷 → JPEG 재인코딩 + 다운스케일
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    long_edge = max(img.size)
    if long_edge > TARGET_LONG_EDGE:
        scale = TARGET_LONG_EDGE / long_edge
        img = img.resize(
            (int(img.size[0] * scale), int(img.size[1] * scale)),
            Image.LANCZOS,
        )

    quality = 85
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= CLAUDE_MAX_BYTES or quality <= 50:
            break
        quality -= 10
    return base64.standard_b64encode(data).decode("ascii"), "image/jpeg"


def _call_with_retry(client, content, system_blocks):
    for attempt in range(2):
        try:
            return client.messages.create(
                model=VISION_MODEL,
                max_tokens=2048,
                system=system_blocks,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIStatusError as e:
            if e.status_code not in (429, 529):
                raise
            if attempt == 0:
                print(f"[IMAGE_SPEC] AI 분석 {e.status_code} — 60초 대기 후 재시도")
                time.sleep(60)
            else:
                raise ServiceBusyError("일시적으로 서비스가 혼잡합니다. 잠시 후 다시 시도해주세요.")


def _empty_slot():
    return {"part_name": None, "part_name_normalized": None, "evidence": ""}


def extract_specs_from_images(image_urls) -> dict:
    """
    이미지 URL 리스트에서 부품 사양 추출. ai_analyzer.py와 호환되는 부분 dict 반환.

    Returns:
        {
            "frameset": {part_name, part_name_normalized, evidence},
            "groupset": {...}, "wheelset": {...}, "saddle": {...}, "handlebar": {...},
            "frame_material": str,
            "brake_type": str,
            "_meta": {
                "image_count": int,
                "image_url_hash": str,
                "input_tokens": int,
                "output_tokens": int,
                "filtered_low_confidence": [필드명 리스트],
                "raw_confidence": dict,
            }
        }
    """
    if len(image_urls) > MAX_IMAGES_TO_SEND:
        image_urls = image_urls[:MAX_IMAGES_TO_SEND]

    client = _get_client()

    content = []
    for i, url in enumerate(image_urls):
        b64, media = _download_as_b64(url)
        content.append({"type": "text", "text": f"=== 이미지 {i} ==="})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": b64},
        })

    # prompt caching: 시스템 프롬프트(긴 추출 가이드)를 캐시
    # 같은 시간대에 여러 페이지 처리할 때 입력 토큰 비용 절감
    system_blocks = [
        {
            "type": "text",
            "text": EXTRACT_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    msg = _call_with_retry(client, content, system_blocks)

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ServiceBusyError(f"이미지 사양 추출 응답을 JSON으로 파싱할 수 없습니다: {raw[:200]}")

    # confidence 컷오프 — 0.7 미만 슬롯은 null 강제
    confidence = parsed.get("_confidence", {}) or {}
    filtered = []

    for slot in ("frameset", "groupset", "wheelset", "saddle", "handlebar"):
        if confidence.get(slot, 0) < CONFIDENCE_FLOOR:
            existing_evidence = (parsed.get(slot) or {}).get("evidence", "")
            parsed[slot] = {
                "part_name": None,
                "part_name_normalized": None,
                "evidence": existing_evidence,
            }
            filtered.append(slot)
        else:
            slot_data = parsed.get(slot) or _empty_slot()
            parsed[slot] = {
                "part_name": slot_data.get("part_name"),
                "part_name_normalized": slot_data.get("part_name_normalized"),
                "evidence": slot_data.get("evidence", ""),
            }

    if confidence.get("frame_material", 0) < CONFIDENCE_FLOOR:
        parsed["frame_material"] = "unknown"
        filtered.append("frame_material")
    if confidence.get("brake_type", 0) < CONFIDENCE_FLOOR:
        parsed["brake_type"] = "unknown"
        filtered.append("brake_type")

    parsed["_meta"] = {
        "image_count": len(image_urls),
        "image_url_hash": hash_image_urls(image_urls),
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
        "filtered_low_confidence": filtered,
        "raw_confidence": confidence,
    }
    parsed.pop("_confidence", None)
    return parsed
