"""
Module that extracts bicycle component specs from image-only product pages
(Korean shopping malls where the spec sheet is rendered only as images).

Outputs the 5 component slots (frameset, groupset, wheelset, saddle, handlebar)
plus frame_material and brake_type, using a JSON schema compatible with
ai_analyzer.py.

Usage flow (from routes/worker):
    text, html = scraper.fetch_html_with_raw(url)
    use_image, image_urls = should_use_image_mode(text, html, url)
    if use_image:
        spec = extract_specs_from_images(image_urls)
        # overwrite ai_analyzer result with spec's component slots
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
    # Fallback when imported standalone (outside the Flask context)
    class ServiceBusyError(Exception):
        pass


# Image-mode branching thresholds
# The keyword heuristic produced too many false positives on sites that expose
# component category names ("wheelset", "frame", etc.) in their sidebar/menu,
# so it has been retired. Instead we look at the actual ai_analyzer extraction
# result and switch to image mode for reinforcement when too many slots are empty.
PART_SLOTS = ("frameset", "groupset", "wheelset", "saddle", "handlebar")
NULL_SLOTS_THRESHOLD = 2       # Switch to image mode if N or more component slots are null
IMAGE_COUNT_THRESHOLD = 3      # And at least M /editor/ images present
MAX_IMAGES_TO_SEND = 8         # Token cost control

# Image processing
CLAUDE_MAX_BYTES = 5 * 1024 * 1024
TARGET_LONG_EDGE = 1568

# Confidence cutoff - slots below this are forced to null
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
You are an expert at extracting component specs from bicycle product-page images.
The attached images are detail-page images (including spec sheets) for a single bicycle model.

[Extraction philosophy - very important]
Do not try to fill slots for individual sub-components (shifters, front/rear derailleurs, crankset, cassette, chain, brakes, etc.).
Instead, gather every component clue visible in the spec sheet and merge them into the following 5 slots:
  frameset / groupset / wheelset / saddle / handlebar

In particular, infer groupset as a single drivetrain lineup:
  - Clues "FC-R8100" + "RD-R8150" + "BR-R8170" -> "Shimano Ultegra Di2"
  - Even just "ST-R7170" -> "Shimano 105 Di2"
  - Reduce model numbers (R9200, R9250, R8150, etc.) to the lineup name

[Shimano lineup inference table]
  R9200/R9250 = Dura-Ace Di2
  R9100/R9150 = Dura-Ace (R9100 = mechanical, R9150 = Di2)
  R8100/R8150/R8170 = Ultegra Di2 (R8100 = mechanical, R8150/R8170 = Di2)
  R7100/R7150/R7170 = 105 Di2 (R7100 = mechanical, R7150/R7170 = Di2)
  R7000 = 105 (previous-gen 11-speed)

[SRAM lineups]
  Red eTap AXS, Force eTap AXS, Rival eTap AXS, Apex eTap AXS

[part_name_normalized rules - identical to ai_analyzer.py]
Lowercase English + underscore (_) only. Spaces, hyphens, uppercase strictly forbidden.
  groupset examples:
    "shimano_dura_ace_di2", "shimano_ultegra_di2", "shimano_105_di2",
    "sram_red_etap_axs", "sram_force_etap_axs"
  Include: brand + lineup + electronic/mechanical distinction (di2)
  Exclude: model numbers (R9200, etc.), suffixes such as 'rail', 'system', 'integrated',
           and compatibility labels such as 'Tubeless', 'TLR'

[Fizik saddle normalized rule]
  fizik_(category)_(lineup)_(rail_grade)_(adaptive flag)
  category: vento / tempo / transiro - default to vento
  lineup: argo / aeris / antares - default to argo
  rail grade: 00 / r1 / r3 / r5 - default to r5

Respond using only the JSON schema below. No markdown, no explanation, JSON only.

{
  "frameset": {
    "part_name": null or "original notation",
    "part_name_normalized": null or "normalized English",
    "evidence": "which image, where, and what clue was used"
  },
  "groupset": {
    "part_name": null or "original notation or lineup name",
    "part_name_normalized": null or "e.g. shimano_ultegra_di2",
    "evidence": "list of collected clues"
  },
  "wheelset": { ... same structure ... },
  "saddle": { ... same structure ... },
  "handlebar": { ... same structure ... },
  "frame_material": "carbon" | "alloy" | "steel" | "titanium" | "other" | "unknown",
  "brake_type": "hydraulic_disc" | "mechanical_disc" | "rim" | "unknown",
  "_confidence": {
    "frameset": 0.0~1.0, "groupset": 0.0~1.0, "wheelset": 0.0~1.0,
    "saddle": 0.0~1.0, "handlebar": 0.0~1.0,
    "frame_material": 0.0~1.0, "brake_type": 0.0~1.0
  },
  "_evidence_image_indices": [supporting image indices],
  "_notes": "anything notable (empty string if none)"
}

[Anti-hallucination rules]
1. If unsure, use null. Do not make plausible guesses.
   - If text is blurry, use null
   - If only some characters are visible, use null
   - If Korean OCR is awkward, use null
2. Aggressively cross-reference clues:
   - Reading even one component code clearly is enough to infer a lineup
   - If clues conflict -> null + record in _notes
3. _confidence guidelines:
   - 0.9+: clearly read lineup name or model number
   - 0.7~0.9: lineup inferred from component code clues, inference is unambiguous
   - 0.5~0.7: inference but some clues blurry
   - below 0.5: leave value as null and confidence as 0.0
4. Ignore design/color photos.
""".strip()


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def extract_detail_images(html: str, base_url: str) -> list:
    """Extract /editor/-path images in the body area as spec candidates."""
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
    """Number of empty component slots in the ai_analyzer result."""
    n = 0
    for slot in PART_SLOTS:
        s = ai_result.get(slot) or {}
        if not s.get("part_name"):
            n += 1
    return n


def should_use_image_mode(ai_result: dict, raw_html: str, base_url: str):
    """
    Decide whether to reinforce with image mode based on the ai_analyzer result.

    Args:
        ai_result: result from ai_analyzer.extract_bike_info() (partial result is fine).
                   The caller may pass an empty or partial dict if AnalysisError was raised.
        raw_html: raw HTML received by the scraper (before cleaning).
        base_url: page URL - used to resolve image absolute paths.

    Returns:
        (use_image: bool, image_urls: list, reason: str)
    """
    null_slots = count_null_part_slots(ai_result)
    image_urls = extract_detail_images(raw_html, base_url)
    use = null_slots >= NULL_SLOTS_THRESHOLD and len(image_urls) >= IMAGE_COUNT_THRESHOLD
    reason = (
        f"null_slots={null_slots}/{len(PART_SLOTS)} (threshold>={NULL_SLOTS_THRESHOLD}), "
        f"editor_images={len(image_urls)} (threshold>={IMAGE_COUNT_THRESHOLD})"
    )
    return use, image_urls, reason


def merge_image_specs_into_ai_result(ai_result: dict, image_specs: dict) -> dict:
    """
    Merge image-mode results into the text-mode (ai_analyzer) result.
    Keep already-filled slots; fill only the empty slots with image-mode results.
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
    """SHA256 of a sorted URL list. Used to verify identity on TTL refresh."""
    joined = "\n".join(sorted(image_urls))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _download_as_b64(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = resp.content

    media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if media_type in ("image/jpeg", "image/png", "image/gif", "image/webp") and len(raw) <= CLAUDE_MAX_BYTES:
        return base64.standard_b64encode(raw).decode("ascii"), media_type

    # Larger than 5MB or unsupported format -> re-encode as JPEG and downscale
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
                print(f"[IMAGE_SPEC] AI analysis {e.status_code} - waiting 60s before retry")
                time.sleep(60)
            else:
                raise ServiceBusyError("The service is temporarily busy. Please try again in a moment.")


def _empty_slot():
    return {"part_name": None, "part_name_normalized": None, "evidence": ""}


def extract_specs_from_images(image_urls) -> dict:
    """
    Extract component specs from a list of image URLs. Returns a partial dict
    compatible with ai_analyzer.py.

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
                "filtered_low_confidence": [list of field names],
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
        content.append({"type": "text", "text": f"=== Image {i} ==="})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": b64},
        })

    # prompt caching: cache the system prompt (long extraction guide)
    # Reduces input token cost when several pages are processed in the same window
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
        raise ServiceBusyError(f"Could not parse image spec extraction response as JSON: {raw[:200]}")

    # confidence cutoff - force null on slots below 0.7
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
