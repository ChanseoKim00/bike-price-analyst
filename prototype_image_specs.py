"""
이미지 전용 상세페이지(fifty2nd 류)에서 부품 사양을 Claude vision으로 추출하는 프로토타입.

사용:
    export ANTHROPIC_API_KEY=...
    python prototype_image_specs.py <product_url> [이미지개수제한]

예:
    python prototype_image_specs.py https://www.fifty2nd.co.kr/shop/item.php?it_id=1772621065 5
"""
import base64
import io
import json
import os
import sys
from urllib.parse import urljoin

import anthropic
import requests
from bs4 import BeautifulSoup
from PIL import Image


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

VISION_MODEL = "claude-sonnet-4-6"

EXTRACT_PROMPT = """
당신은 자전거 상세페이지 이미지에서 부품 사양을 추출하는 전문가입니다.
첨부된 이미지들은 한 자전거 모델의 상세페이지 이미지(사양표 포함)입니다.

[추출 철학 — 매우 중요]
세부 부품(변속레버, 앞/뒷변속기, 크랭크, 카세트, 체인, 브레이크 등)을 슬롯별로 채우려 하지 마세요.
대신 사양표에 보이는 모든 부품 단서를 모아서, 다음 5개 슬롯으로 합쳐 추론하세요:
  frameset / groupset / wheelset / saddle / handlebar

특히 groupset은 구동계 라인업 하나로 추론합니다:
  - "FC-R8100" + "RD-R8150" + "BR-R8170" 단서 → "Shimano Ultegra Di2" (전동) 하나로 통합
  - "ST-R7170" 단서만 봐도 → "Shimano 105 Di2"
  - 모델 번호(R9200, R9250, R8150 등)는 라인업명으로 환원하라

[Shimano 라인업 추론 표]
  R9200/R9250 = Dura-Ace Di2
  R9100/R9150 = Dura-Ace (R9100=기계식, R9150=Di2)
  R8100/R8150/R8170 = Ultegra Di2 (R8100=기계식, R8150/R8170=Di2)
  R7100/R7150/R7170 = 105 Di2 (R7100=기계식, R7150/R7170=Di2)
  R7000 = 105 (구세대 11단)
  Tiagra = 4700 시리즈

[SRAM 라인업 추론 표]
  Red eTap AXS, Force eTap AXS, Rival eTap AXS, Apex eTap AXS

[part_name_normalized 규칙 — ai_analyzer.py와 일치시킬 것]
영문 소문자 + 언더스코어(_)만. 띄어쓰기/하이픈/대문자 절대 금지.
  groupset: "shimano_dura_ace_di2", "shimano_ultegra_di2", "shimano_105_di2",
            "sram_red_etap_axs", "sram_force_etap_axs"
  포함: 브랜드 + 라인업 + 전동/기계식 구분(di2)
  제외: 모델 번호(R9200 등), 'rail', 'system', 'integrated' 같은 접미어

아래 JSON 스키마로만 응답하세요. 마크다운/설명 금지, JSON만 출력.

{
  "frameset": {
    "part_name": null 또는 "원본 표기",
    "part_name_normalized": null 또는 "정규화된 영문",
    "evidence": "이미지 어디에서 어떤 단서로 추출했는지 짧게"
  },
  "groupset": {
    "part_name": null 또는 "원본 표기 또는 라인업명",
    "part_name_normalized": null 또는 "예: shimano_ultegra_di2",
    "evidence": "수집한 단서 나열 (예: 'FC-R8100, RD-R8150, BR-R8170 → Ultegra Di2 추론')"
  },
  "wheelset": {
    "part_name": null 또는 "원본 표기",
    "part_name_normalized": null 또는 "정규화된 영문",
    "evidence": "..."
  },
  "saddle": {
    "part_name": null 또는 "원본 표기",
    "part_name_normalized": null 또는 "정규화된 영문",
    "evidence": "..."
  },
  "handlebar": {
    "part_name": null 또는 "원본 표기",
    "part_name_normalized": null 또는 "정규화된 영문",
    "evidence": "..."
  },
  "frame_material": "carbon" | "alloy" | "steel" | "titanium" | "other" | "unknown",
  "brake_type": "hydraulic_disc" | "mechanical_disc" | "rim" | "unknown",
  "_confidence": {
    "frameset": 0.0~1.0, "groupset": 0.0~1.0, "wheelset": 0.0~1.0,
    "saddle": 0.0~1.0, "handlebar": 0.0~1.0,
    "frame_material": 0.0~1.0, "brake_type": 0.0~1.0
  },
  "_evidence_image_indices": [근거가 된 이미지 인덱스],
  "_notes": "특이사항(없으면 빈 문자열)"
}

[환각 방지 규칙]
1. 모르면 null. 그럴듯하게 추측 금지.
   - 텍스트가 흐리면 null
   - 일부 글자만 보이면 null
   - 한글 OCR이 어색하면 null
2. 단서 cross-reference 적극 활용:
   - 어느 한 부품 코드만 또렷이 읽혀도 라인업 추론 가능
   - 흐린 글자는 다른 또렷한 단서로 cross-check
   - 단서들이 서로 모순되면(예: Ultegra와 105 둘 다 단서 발견) → null + _notes에 명시
3. _confidence 기준:
   - 0.9+: 또렷한 라인업명 또는 모델 번호 직접 읽음
   - 0.7~0.9: 부품 코드 단서로 라인업 추론, 추론은 명확
   - 0.5~0.7: 추론이지만 단서 일부 흐림
   - 0.5 미만: value를 null로 두고 confidence는 0.0
4. 디자인/색상/감성 사진은 무시.
""".strip()


def extract_detail_images(html: str, base_url: str) -> list[str]:
    """한국 쇼핑몰 상세 이미지 URL 추출.

    영카트/그누보드 계열은 data/editor, upload/editor 등 'editor/' 경로에
    상세설명 본문 이미지를 둔다. 이걸 키워드로 필터링한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        # 본문 상세 이미지는 보통 /editor/ 경로에 위치
        if "/editor/" not in src:
            continue
        full = urljoin(base_url, src)
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)
    return urls


def fetch_with_playwright(url: str) -> str:
    """JS 렌더링이 필요한 페이지용. requests 결과가 부족할 때 호출."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        page.goto(url, wait_until="networkidle", timeout=30000)
        # 상세정보/스펙 탭이 있으면 클릭해서 본문 펼치기 (있으면 클릭, 없으면 무시)
        for tab_text in ("상세정보", "상세 정보", "스펙", "Specification"):
            try:
                page.click(f"text={tab_text}", timeout=2000)
                page.wait_for_timeout(1500)
                break
            except Exception:
                continue
        html = page.content()
        browser.close()
    return html


CLAUDE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB API 제한
TARGET_LONG_EDGE = 1568  # Claude vision 권장 — 짧은 변 1568px 안쪽이면 추가 다운스케일 없음


def download_as_b64(url: str) -> tuple[str, str]:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = resp.content

    # 5MB 미만이고 지원 포맷이면 그대로 보냄
    media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if media_type in ("image/jpeg", "image/png", "image/gif", "image/webp") and len(raw) <= CLAUDE_MAX_BYTES:
        return base64.standard_b64encode(raw).decode("ascii"), media_type

    # 초과 또는 미지원 포맷 → JPEG로 재인코딩 + 필요시 다운스케일
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    long_edge = max(img.size)
    if long_edge > TARGET_LONG_EDGE:
        scale = TARGET_LONG_EDGE / long_edge
        img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)), Image.LANCZOS)

    quality = 85
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= CLAUDE_MAX_BYTES or quality <= 50:
            break
        quality -= 10
    return base64.standard_b64encode(data).decode("ascii"), "image/jpeg"


def extract_specs(image_urls: list[str]) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    content: list[dict] = []
    for i, url in enumerate(image_urls):
        print(f"  [{i}] downloading {url}", file=sys.stderr)
        b64, media = download_as_b64(url)
        content.append({"type": "text", "text": f"=== 이미지 {i} ==="})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": b64},
        })
    content.append({"type": "text", "text": EXTRACT_PROMPT})

    msg = client.messages.create(
        model=VISION_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": content}],
    )

    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return {
        "specs": json.loads(raw),
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("사용: python prototype_image_specs.py <product_url> [이미지개수제한]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 환경변수가 필요합니다.", file=sys.stderr)
        sys.exit(1)

    print(f"[1/3] 페이지 가져오는 중: {url}", file=sys.stderr)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.encoding = resp.apparent_encoding
    html = resp.text

    print("[2/3] 상세 이미지 URL 추출", file=sys.stderr)
    images = extract_detail_images(html, url)
    print(f"  → requests 단계: {len(images)}장 발견", file=sys.stderr)

    # 상세 이미지가 1장 미만이면 JS 렌더링 페이지로 보고 Playwright 재시도
    if len(images) < 1:
        print("  → Playwright 폴백 시도", file=sys.stderr)
        try:
            html = fetch_with_playwright(url)
            images = extract_detail_images(html, url)
            print(f"  → Playwright 단계: {len(images)}장 발견", file=sys.stderr)
        except Exception as e:
            print(f"  → Playwright 실패: {type(e).__name__}: {e}", file=sys.stderr)

    if limit and limit < len(images):
        images = images[:limit]
        print(f"  → 앞 {len(images)}장만 사용", file=sys.stderr)

    if not images:
        print("상세 이미지를 찾지 못했습니다.", file=sys.stderr)
        sys.exit(1)

    print(f"[3/3] Claude {VISION_MODEL}에 추출 요청", file=sys.stderr)
    result = extract_specs(images)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
