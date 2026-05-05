"""결과 페이지 공유용 OG 이미지 생성기.

분석 결과를 1200×630 PNG으로 렌더링한다. 분석 결과는 immutable이므로
analysis_id 단위로 디스크에 영구 캐시해 재요청은 파일 응답으로 처리한다.

한글 폰트는 다음 순서로 탐색한다:
  1) repo에 번들된 static/fonts/*.ttf
  2) fc-match로 시스템 sans-serif:lang=ko 조회 (linux/macOS 공용)
  3) 알려진 절대 경로 (Nix Noto CJK, Nanum, macOS Apple SD Gothic Neo)

운영 환경(nixpacks)은 noto-fonts-cjk-sans 패키지로 한글을 보장하고,
로컬은 macOS 시스템 폰트로 동작한다.
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

logger = logging.getLogger(__name__)

OG_W, OG_H = 1200, 630

# BPA 톤앤매너 (style.css와 동기화)
BG       = (49, 49, 60)      # #31313C
ACCENT   = (179, 205, 255)   # #B3CDFF
WHITE    = (255, 255, 255)
MUTED    = (160, 160, 176)   # #a0a0b0
DIM      = (107, 107, 126)   # #6b6b7e
SURFACE  = (62, 62, 74)      # #3e3e4a (입력박스 배경)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_FONT_DIR = _REPO_ROOT / "static" / "fonts"

_FALLBACK_FONT_PATHS = [
    # Nix store는 경로가 동적이라 fc-match로 우선 탐색하고, 못 찾을 때 후보를 시도.
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/Library/Fonts/AppleGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]


@lru_cache(maxsize=2)
def _resolve_font_path(bold: bool) -> str | None:
    """한글을 렌더할 수 있는 폰트 파일 경로를 반환. 못 찾으면 None.

    bold=True면 굵은 변형을 우선시한다. 캐시는 프로세스 수명 동안 유지.
    """
    # 1) repo 번들 폰트
    bundled_candidates = (
        ["Pretendard-Bold.ttf", "Pretendard-ExtraBold.ttf", "NotoSansKR-Bold.ttf"]
        if bold else
        ["Pretendard-Regular.ttf", "NotoSansKR-Regular.ttf"]
    )
    for name in bundled_candidates:
        p = _BUNDLED_FONT_DIR / name
        if p.exists():
            return str(p)

    # 2) fc-match (linux/macOS 모두 사용 가능). weight 200=bold, 80=regular per fontconfig.
    if shutil.which("fc-match"):
        try:
            spec = f"sans-serif:lang=ko:weight={'200' if bold else '80'}"
            out = subprocess.run(
                ["fc-match", "-f", "%{file}", spec],
                capture_output=True, text=True, timeout=2,
            )
            path = (out.stdout or "").strip()
            if out.returncode == 0 and path and Path(path).exists():
                return path
        except Exception as e:
            logger.warning("fc-match 폰트 조회 실패: %s", e)

    # 3) 정적 후보
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
            logger.warning("폰트 로드 실패 %s: %s", path, e)
    # 한글 미지원 폴백 — 적어도 라틴/숫자는 출력되게.
    logger.error("한글 폰트 미설치: OG 이미지의 한글이 깨져 보일 수 있음. static/fonts/에 Pretendard-Bold.ttf를 두거나 시스템에 noto-fonts-cjk-sans를 설치하세요.")
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
    """한 줄에 들어가도록 말줄임. 한글/영문 혼용 안전."""
    if _text_size(draw, text, font)[0] <= max_w:
        return text
    ellipsis = "…"
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


def render_og_image(
    saving_krw: int,
    saving_pct: float | int | None,
    bike_brand: str | None,
    bike_model: str | None,
    bike_year: int | None,
) -> bytes:
    """1200×630 PNG 바이트를 반환."""
    img = Image.new("RGB", (OG_W, OG_H), BG)
    draw = ImageDraw.Draw(img)

    # 좌측 상단 액센트 바 — BPA 식별 요소
    draw.rectangle([(0, 0), (8, OG_H)], fill=ACCENT)

    # 좌측 상단 BPA 로고
    logo_font = _load_font(56, bold=True)
    draw.text((56, 48), "BPA", font=logo_font, fill=ACCENT)

    # 우측 상단 도메인/태그라인
    tag_font = _load_font(22, bold=False)
    tag_text = "Bike Price Analyst"
    tw, th = _text_size(draw, tag_text, tag_font)
    draw.text((OG_W - 56 - tw, 64), tag_text, font=tag_font, fill=MUTED)

    # 본문 영역 폭/패딩
    pad_x = 80
    inner_w = OG_W - pad_x * 2

    # 1) 프리헤드라인
    pre_font = _load_font(38, bold=False)
    pre_text = "이 자전거, 부품 합산보다"
    pw, ph = _text_size(draw, pre_text, pre_font)
    pre_y = 200
    draw.text(((OG_W - pw) // 2, pre_y), pre_text, font=pre_font, fill=MUTED)

    # 2) 메인 헤드라인 — "₩1,234,000 싸다"
    amount_part = f"₩{saving_krw:,}"
    tail_part = " 싸다"
    head_font = _load_font(116, bold=True)

    # 한 줄에 안 들어가면 폰트 점진 축소
    full = amount_part + tail_part
    head_w, head_h = _text_size(draw, full, head_font)
    size = 116
    while head_w > inner_w and size > 64:
        size -= 6
        head_font = _load_font(size, bold=True)
        head_w, head_h = _text_size(draw, full, head_font)

    head_x = (OG_W - head_w) // 2
    head_y = pre_y + ph + 28
    # amount는 ACCENT, " 싸다"는 WHITE로 분리 렌더 — 가독성·후킹 강화.
    draw.text((head_x, head_y), amount_part, font=head_font, fill=ACCENT)
    amt_w, _ = _text_size(draw, amount_part, head_font)
    draw.text((head_x + amt_w, head_y), tail_part, font=head_font, fill=WHITE)

    # 3) 서브카피 — 비율 백분율
    sub_font = _load_font(28, bold=False)
    if saving_pct is not None:
        try:
            pct_val = float(saving_pct)
            # 정수면 소수점 제거
            pct_str = f"{int(pct_val)}" if pct_val.is_integer() else f"{pct_val:.1f}"
            sub_text = f"부품 개별 구매 대비 {pct_str}% 저렴"
        except Exception:
            sub_text = "부품 개별 구매 대비 더 저렴"
    else:
        sub_text = "부품 개별 구매 대비 더 저렴"
    sw, sh = _text_size(draw, sub_text, sub_font)
    sub_y = head_y + head_h + 20
    draw.text(((OG_W - sw) // 2, sub_y), sub_text, font=sub_font, fill=MUTED)

    # 4) 디바이더
    div_y = sub_y + sh + 44
    div_w = 64
    draw.rectangle(
        [((OG_W - div_w) // 2, div_y), ((OG_W + div_w) // 2, div_y + 3)],
        fill=ACCENT,
    )

    # 5) 자전거 라벨
    bike_label = _format_bike_label(bike_brand, bike_model, bike_year)
    bike_font = _load_font(32, bold=False)
    bike_label = _truncate_to_width(draw, bike_label, bike_font, inner_w)
    bw, bh = _text_size(draw, bike_label, bike_font)
    bike_y = div_y + 24
    draw.text(((OG_W - bw) // 2, bike_y), bike_label, font=bike_font, fill=WHITE)

    # 6) 푸터 카피
    footer_font = _load_font(22, bold=False)
    footer_text = "URL 하나로 자전거 가격을 분석합니다"
    fw, fh = _text_size(draw, footer_text, footer_font)
    draw.text(((OG_W - fw) // 2, OG_H - 56 - fh), footer_text, font=footer_font, fill=DIM)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── 디스크 캐시 ────────────────────────────────────────────────
# Analysis는 immutable한 분석 스냅샷이므로 analysis_id 키로 영구 캐시한다.
# Railway 컨테이너 재시작 시 캐시가 날아가도 다음 요청에서 재생성되므로 무해.

_CACHE_DIR = Path(os.environ.get("BPA_OG_CACHE_DIR", "/tmp/bpa_og_cache"))


def _cache_path(analysis_id) -> Path:
    # analysis.id가 SQLAlchemy UUID 객체로 오는 경로가 있어 str() 강제 변환.
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
    """캐시 적중 시 디스크에서, 미스면 새로 렌더해 캐시에 저장."""
    p = _cache_path(analysis_id)
    if p.exists():
        try:
            return p.read_bytes()
        except Exception as e:
            logger.warning("OG 캐시 읽기 실패 %s: %s", p, e)

    data = render_og_image(saving_krw, saving_pct, bike_brand, bike_model, bike_year)

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".png.tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
    except Exception as e:
        logger.warning("OG 캐시 쓰기 실패 %s: %s", p, e)

    return data
