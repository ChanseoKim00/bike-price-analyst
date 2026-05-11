"""
Prototype that uses Claude vision to extract part specs from image-only product pages (e.g. fifty2nd).

Usage:
    export ANTHROPIC_API_KEY=...
    python prototype_image_specs.py <product_url> [image_count_limit]

Example:
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
You are an expert at extracting part specifications from bicycle product detail page images.
The attached images are detail-page images (including a spec table) for a single bicycle model.

[Extraction philosophy — very important]
Do not try to fill per-component slots (shifters, front/rear derailleurs, crankset, cassette, chain, brakes, etc.) individually.
Instead, gather every part-related clue visible in the spec table and consolidate the inference into the following 5 slots:
  frameset / groupset / wheelset / saddle / handlebar

In particular, infer the groupset as a single drivetrain lineup:
  - Clues "FC-R8100" + "RD-R8150" + "BR-R8170" -> consolidate into "Shimano Ultegra Di2" (electronic)
  - Even a single "ST-R7170" clue -> "Shimano 105 Di2"
  - Reduce model numbers (R9200, R9250, R8150, etc.) to the lineup name

[Shimano lineup inference table]
  R9200/R9250 = Dura-Ace Di2
  R9100/R9150 = Dura-Ace (R9100=mechanical, R9150=Di2)
  R8100/R8150/R8170 = Ultegra Di2 (R8100=mechanical, R8150/R8170=Di2)
  R7100/R7150/R7170 = 105 Di2 (R7100=mechanical, R7150/R7170=Di2)
  R7000 = 105 (previous-generation 11-speed)
  Tiagra = 4700 series

[SRAM lineup inference table]
  Red eTap AXS, Force eTap AXS, Rival eTap AXS, Apex eTap AXS

[part_name_normalized rules — must match ai_analyzer.py]
Lowercase letters and underscores (_) only. No spaces, hyphens, or uppercase letters allowed.
  groupset: "shimano_dura_ace_di2", "shimano_ultegra_di2", "shimano_105_di2",
            "sram_red_etap_axs", "sram_force_etap_axs"
  Include: brand + lineup + electronic/mechanical distinction (di2)
  Exclude: model numbers (R9200, etc.), suffixes like 'rail', 'system', 'integrated'

Respond using only the JSON schema below. No markdown or explanation, JSON only.

{
  "frameset": {
    "part_name": null or "original wording",
    "part_name_normalized": null or "normalized English",
    "evidence": "brief note on where in the image and what clue was used"
  },
  "groupset": {
    "part_name": null or "original wording or lineup name",
    "part_name_normalized": null or "e.g. shimano_ultegra_di2",
    "evidence": "list the gathered clues (e.g. 'FC-R8100, RD-R8150, BR-R8170 -> inferred Ultegra Di2')"
  },
  "wheelset": {
    "part_name": null or "original wording",
    "part_name_normalized": null or "normalized English",
    "evidence": "..."
  },
  "saddle": {
    "part_name": null or "original wording",
    "part_name_normalized": null or "normalized English",
    "evidence": "..."
  },
  "handlebar": {
    "part_name": null or "original wording",
    "part_name_normalized": null or "normalized English",
    "evidence": "..."
  },
  "frame_material": "carbon" | "alloy" | "steel" | "titanium" | "other" | "unknown",
  "brake_type": "hydraulic_disc" | "mechanical_disc" | "rim" | "unknown",
  "_confidence": {
    "frameset": 0.0~1.0, "groupset": 0.0~1.0, "wheelset": 0.0~1.0,
    "saddle": 0.0~1.0, "handlebar": 0.0~1.0,
    "frame_material": 0.0~1.0, "brake_type": 0.0~1.0
  },
  "_evidence_image_indices": [indices of supporting images],
  "_notes": "anything noteworthy (empty string if nothing)"
}

[Hallucination prevention rules]
1. If unsure, return null. Do not make plausible guesses.
   - If text is blurry, null
   - If only some characters are visible, null
   - If the Korean OCR reading looks awkward, null
2. Actively cross-reference clues:
   - Even a single clearly read part code is enough to infer the lineup
   - Cross-check blurry text against other clearly visible clues
   - If clues contradict each other (e.g. clues for both Ultegra and 105) -> null + note it in _notes
3. _confidence guidelines:
   - 0.9+: clearly read lineup name or model number directly
   - 0.7~0.9: lineup inferred from part-code clues, inference is clear
   - 0.5~0.7: inference, but some clues are blurry
   - below 0.5: leave value as null and set confidence to 0.0
4. Ignore design/color/lifestyle photos.
""".strip()


def extract_detail_images(html: str, base_url: str) -> list[str]:
    """Extract detail-image URLs from a Korean shopping mall page.

    Youngcart/Gnuboard-based stores place the body images for the detail
    description under paths like data/editor or upload/editor. Filter by
    that keyword.
    """
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        # Body detail images are typically located under /editor/
        if "/editor/" not in src:
            continue
        full = urljoin(base_url, src)
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)
    return urls


def fetch_with_playwright(url: str) -> str:
    """For pages that require JS rendering. Call this when the requests result is insufficient."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        page.goto(url, wait_until="networkidle", timeout=30000)
        # If a detail/spec tab exists, click it to expand the body (click if present, ignore otherwise)
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


CLAUDE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB API limit
TARGET_LONG_EDGE = 1568  # Claude vision recommendation — no further downscale needed if short edge is within 1568px


def download_as_b64(url: str) -> tuple[str, str]:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = resp.content

    # If under 5MB and a supported format, send as-is
    media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if media_type in ("image/jpeg", "image/png", "image/gif", "image/webp") and len(raw) <= CLAUDE_MAX_BYTES:
        return base64.standard_b64encode(raw).decode("ascii"), media_type

    # Over the limit or unsupported format -> re-encode as JPEG + downscale if needed
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
        content.append({"type": "text", "text": f"=== image {i} ==="})
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
        print("usage: python prototype_image_specs.py <product_url> [image_count_limit]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("The ANTHROPIC_API_KEY environment variable is required.", file=sys.stderr)
        sys.exit(1)

    print(f"[1/3] fetching page: {url}", file=sys.stderr)
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.encoding = resp.apparent_encoding
    html = resp.text

    print("[2/3] extracting detail image URLs", file=sys.stderr)
    images = extract_detail_images(html, url)
    print(f"  -> requests stage: found {len(images)} images", file=sys.stderr)

    # If fewer than one detail image, treat it as a JS-rendered page and retry with Playwright
    if len(images) < 1:
        print("  -> trying Playwright fallback", file=sys.stderr)
        try:
            html = fetch_with_playwright(url)
            images = extract_detail_images(html, url)
            print(f"  -> Playwright stage: found {len(images)} images", file=sys.stderr)
        except Exception as e:
            print(f"  -> Playwright failed: {type(e).__name__}: {e}", file=sys.stderr)

    if limit and limit < len(images):
        images = images[:limit]
        print(f"  -> using only the first {len(images)} images", file=sys.stderr)

    if not images:
        print("Could not find any detail images.", file=sys.stderr)
        sys.exit(1)

    print(f"[3/3] requesting extraction from Claude {VISION_MODEL}", file=sys.stderr)
    result = extract_specs(images)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
