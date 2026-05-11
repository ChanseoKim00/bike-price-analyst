"""OG image generator for the result page share card.

Renders the analysis result as a 1200x630 PNG. Analyses are immutable, so
results are cached on disk by analysis_id and re-served from file on repeat
requests.

Latin/sans-serif fonts are looked up in this order:
  1) Bundled fonts in static/fonts/*.ttf
  2) macOS system Helvetica
  3) Linux DejaVu Sans Bold
  4) PIL default font (last resort)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .exchange_rate import get_exchange_rates

logger = logging.getLogger(__name__)

OG_W, OG_H = 1200, 630

# BPA brand palette (kept in sync with style.css)
BG       = (49, 49, 60)      # #31313C
ACCENT   = (179, 205, 255)   # #B3CDFF
WHITE    = (255, 255, 255)
MUTED    = (160, 160, 176)   # #a0a0b0
DIM      = (107, 107, 126)   # #6b6b7e
SURFACE  = (62, 62, 74)      # #3e3e4a (input box background)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_FONT_DIR = _REPO_ROOT / "static" / "fonts"

_FALLBACK_FONT_PATHS = [
    # macOS system fonts
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial.ttf",
    # Linux (Debian/Ubuntu) DejaVu
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # Linux Liberation
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


@lru_cache(maxsize=2)
def _resolve_font_path(bold: bool) -> str | None:
    """Return a path to a font file capable of rendering Latin text. None if not found.

    Prefers the bold variant when bold=True. Cached for the process lifetime.
    """
    # 1) Repo-bundled fonts
    bundled_candidates = (
        ["Inter-Bold.ttf", "Roboto-Bold.ttf", "OpenSans-Bold.ttf"]
        if bold else
        ["Inter-Regular.ttf", "Roboto-Regular.ttf", "OpenSans-Regular.ttf"]
    )
    for name in bundled_candidates:
        p = _BUNDLED_FONT_DIR / name
        if p.exists():
            return str(p)

    # 2) fc-match (available on linux/macOS). weight 200=bold, 80=regular per fontconfig.
    if shutil.which("fc-match"):
        try:
            spec = f"sans-serif:weight={'200' if bold else '80'}"
            out = subprocess.run(
                ["fc-match", "-f", "%{file}", spec],
                capture_output=True, text=True, timeout=2,
            )
            path = (out.stdout or "").strip()
            if out.returncode == 0 and path and Path(path).exists():
                return path
        except Exception as e:
            logger.warning("fc-match font lookup failed: %s", e)

    # 3) Static candidates
    for cand in _FALLBACK_FONT_PATHS:
        if Path(cand).exists():
            return cand

    return None


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    path = _resolve_font_path(bold=bold)
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception as e:
            logger.warning("Font load failed %s: %s", path, e)
    # Last-resort fallback - at least Latin/digits will render.
    logger.error("No suitable sans-serif font installed: OG image text may render at default size. Install DejaVu/Liberation, or drop a TTF into static/fonts/.")
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _format_brand(brand: str | None) -> str:
    if not brand:
        return ""
    return brand.replace("_", " ").title()


def _format_bike_label(brand: str | None, model_name: str | None, model_year: int | None) -> str:
    pieces = []
    b = _format_brand(brand)
    if b:
        pieces.append(b)
    if model_name:
        pieces.append(model_name)
    label = " ".join(pieces).strip()
    if model_year:
        label = f"{label} ({model_year})"
    return label or "Bike"


def _truncate_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_w: int,
) -> str:
    """Truncate to fit a single line. Safe for mixed scripts."""
    if _text_size(draw, text, font)[0] <= max_w:
        return text
    ellipsis = "..."
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if _text_size(draw, candidate, font)[0] <= max_w:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best or ellipsis


def _krw_to_usd(saving_krw: int) -> int:
    """Convert KRW savings to USD using current exchange rate (fallback 1470)."""
    try:
        rate = get_exchange_rates().get("USD", 1470)
    except Exception as e:
        logger.warning("Exchange rate lookup failed, using fallback: %s", e)
        rate = 1470
    if not rate:
        rate = 1470
    return int(round(saving_krw / rate))


def render_og_image(
    saving_krw: int,
    saving_pct: float | int | None,
    bike_brand: str | None,
    bike_model: str | None,
    bike_year: int | None,
) -> bytes:
    """Return a 1200x630 PNG as bytes."""
    img = Image.new("RGB", (OG_W, OG_H), BG)
    draw = ImageDraw.Draw(img)

    # Top-left accent bar - BPA identity element
    draw.rectangle([(0, 0), (8, OG_H)], fill=ACCENT)

    # Top-left BPA logo
    logo_font = _load_font(56, bold=True)
    draw.text((56, 48), "BPA", font=logo_font, fill=ACCENT)

    # Top-right domain/tagline
    tag_font = _load_font(22, bold=False)
    tag_text = "Bike Price Analyst"
    tw, th = _text_size(draw, tag_text, tag_font)
    draw.text((OG_W - 56 - tw, 64), tag_text, font=tag_font, fill=MUTED)

    # Body width / padding
    pad_x = 80
    inner_w = OG_W - pad_x * 2

    # 1) Pre-headline
    pre_font = _load_font(38, bold=False)
    pre_text = "This bike is cheaper than its parts by"
    pw, ph = _text_size(draw, pre_text, pre_font)
    pre_y = 200
    draw.text(((OG_W - pw) // 2, pre_y), pre_text, font=pre_font, fill=MUTED)

    # 2) Main headline - "$1,234 cheaper"
    saving_usd = _krw_to_usd(saving_krw)
    amount_part = f"${saving_usd:,}"
    tail_part = " cheaper"
    head_font = _load_font(116, bold=True)

    # If it doesn't fit on one line, shrink the font progressively
    full = amount_part + tail_part
    head_w, head_h = _text_size(draw, full, head_font)
    size = 116
    while head_w > inner_w and size > 64:
        size -= 6
        head_font = _load_font(size, bold=True)
        head_w, head_h = _text_size(draw, full, head_font)

    head_x = (OG_W - head_w) // 2
    head_y = pre_y + ph + 28
    # Render amount in ACCENT, " cheaper" in WHITE - separate strokes for readability/hook.
    draw.text((head_x, head_y), amount_part, font=head_font, fill=ACCENT)
    amt_w, _ = _text_size(draw, amount_part, head_font)
    draw.text((head_x + amt_w, head_y), tail_part, font=head_font, fill=WHITE)

    # 3) Sub copy - percentage savings
    sub_font = _load_font(28, bold=False)
    if saving_pct is not None:
        try:
            pct_val = float(saving_pct)
            # Drop decimals if integer
            pct_str = f"{int(pct_val)}" if pct_val.is_integer() else f"{pct_val:.1f}"
            sub_text = f"{pct_str}% cheaper than buying parts individually"
        except Exception:
            sub_text = "Cheaper than buying parts individually"
    else:
        sub_text = "Cheaper than buying parts individually"
    sw, sh = _text_size(draw, sub_text, sub_font)
    sub_y = head_y + head_h + 20
    draw.text(((OG_W - sw) // 2, sub_y), sub_text, font=sub_font, fill=MUTED)

    # 4) Divider
    div_y = sub_y + sh + 44
    div_w = 64
    draw.rectangle(
        [((OG_W - div_w) // 2, div_y), ((OG_W + div_w) // 2, div_y + 3)],
        fill=ACCENT,
    )

    # 5) Bike label
    bike_label = _format_bike_label(bike_brand, bike_model, bike_year)
    bike_font = _load_font(32, bold=False)
    bike_label = _truncate_to_width(draw, bike_label, bike_font, inner_w)
    bw, bh = _text_size(draw, bike_label, bike_font)
    bike_y = div_y + 24
    draw.text(((OG_W - bw) // 2, bike_y), bike_label, font=bike_font, fill=WHITE)

    # 6) Footer copy
    footer_font = _load_font(22, bold=False)
    footer_text = "Analyze a bike's price from a single URL"
    fw, fh = _text_size(draw, footer_text, footer_font)
    draw.text(((OG_W - fw) // 2, OG_H - 56 - fh), footer_text, font=footer_font, fill=DIM)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# -- Disk cache --------------------------------------------------
# An Analysis is an immutable snapshot, so we cache permanently keyed by analysis_id.
# Even if the cache is wiped on Railway container restart, the next request regenerates
# it harmlessly.

_CACHE_DIR = Path(os.environ.get("BPA_OG_CACHE_DIR", "/tmp/bpa_og_cache"))


def _cache_path(analysis_id) -> Path:
    # analysis.id can arrive as a SQLAlchemy UUID object on some paths, so force str().
    s = str(analysis_id)
    safe = "".join(c for c in s if c.isalnum() or c in "-_")
    return _CACHE_DIR / f"{safe}.png"


def get_or_render_og(
    analysis_id: str,
    saving_krw: int,
    saving_pct: float | int | None,
    bike_brand: str | None,
    bike_model: str | None,
    bike_year: int | None,
) -> bytes:
    """On cache hit, return from disk; on miss, render and store."""
    p = _cache_path(analysis_id)
    if p.exists():
        try:
            return p.read_bytes()
        except Exception as e:
            logger.warning("OG cache read failed %s: %s", p, e)

    data = render_og_image(saving_krw, saving_pct, bike_brand, bike_model, bike_year)

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".png.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
    except Exception as e:
        logger.warning("OG cache write failed %s: %s", p, e)

    return data
