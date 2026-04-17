import re
import requests
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
TIMEOUT = 15      # requests 타임아웃 (초)
PW_TIMEOUT = 30   # Playwright 타임아웃 (초)
JS_THRESHOLD = 500  # 이 글자 수 미만이면 Playwright 폴백


class ScrapeError(Exception):
    """스크래핑 실패 시 발생 — routes.py에서 케이스 6 처리용"""
    def __init__(self, message, code="unknown"):
        super().__init__(message)
        self.code = code


def fetch_html(url: str) -> str:
    """
    URL에서 HTML을 가져와 본문 텍스트만 정제해서 반환.
    requests로 먼저 시도하고, 결과가 JS_THRESHOLD 미만이면 Playwright로 재시도.

    Returns:
        str: 정제된 텍스트

    Raises:
        ScrapeError: 네트워크 오류, 봇 차단(403/429), 링크 만료(404) 등
    """
    # 1차 시도: requests
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        raise ScrapeError(f"연결 실패: {url}", code="connection_error")
    except requests.exceptions.Timeout:
        raise ScrapeError(f"응답 시간 초과 ({TIMEOUT}s): {url}", code="timeout")
    except requests.exceptions.RequestException as e:
        raise ScrapeError(f"요청 오류: {e}", code="http_error")

    if resp.status_code == 404:
        raise ScrapeError(f"페이지를 찾을 수 없습니다 (404): {url}", code="not_found")
    if resp.status_code in (403, 429):
        raise ScrapeError(f"봇 차단 또는 접근 거부 ({resp.status_code}): {url}", code="blocked")
    if not resp.ok:
        raise ScrapeError(f"HTTP {resp.status_code}: {url}", code="http_error")

    decoded = _decode_response(resp)
    text = _clean_html(decoded)

    if len(text) >= JS_THRESHOLD:
        print(f"[SCRAPER] requests 성공 ({len(text)}자)")
        return text

    # 2차 시도: Playwright (JS 렌더링)
    print(f"[SCRAPER] requests 결과 {len(text)}자 — Playwright 폴백 시도")
    pw_text = _fetch_with_playwright(url)
    if pw_text:
        print(f"[SCRAPER] Playwright 성공 ({len(pw_text)}자)")
        return pw_text

    # Playwright도 실패하면 requests 결과 그대로 반환
    print(f"[SCRAPER] Playwright 실패 — requests 결과({len(text)}자) 사용")
    return text


def _fetch_with_playwright(url: str) -> str:
    """
    Playwright headless Chromium으로 JS 렌더링 후 텍스트 추출.

    Returns:
        str: 정제된 텍스트. 실패 시 빈 문자열.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[SCRAPER] playwright 패키지 없음 — 폴백 스킵")
        return ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
            )
            page.goto(url, wait_until="networkidle", timeout=PW_TIMEOUT * 1000)
            html = page.content()
            browser.close()
        return _clean_html(html)
    except Exception as e:
        print(f"[SCRAPER] Playwright 오류: {type(e).__name__}: {e}")
        return ""


def _decode_response(resp) -> str:
    """
    응답 본문을 올바른 인코딩으로 디코딩.
    apparent_encoding 우선, 실패 시 cp949 → utf-8 순으로 시도.
    """
    for enc in [resp.apparent_encoding, "cp949", "utf-8"]:
        if not enc:
            continue
        try:
            return resp.content.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    # 모두 실패 시 utf-8 errors=ignore로 fallback
    return resp.content.decode("utf-8", errors="ignore")


def _clean_html(raw_html: str) -> str:
    """
    AI 토큰 최소화를 위해 불필요한 태그 제거 후 텍스트만 추출.
    - 제거 태그: 레이아웃/UI 요소(nav, header, footer 등) + 비텍스트 요소(script, style 등)
    - 연속 공백·줄바꿈 압축

    주의: 일부 사이트는 <head>/<link> 태그가 body 안에 중복 등장하고,
    BeautifulSoup(html.parser)가 그 이후 내용을 해당 태그의 자식으로 파싱한다.
    → 진짜 <head>(html 직계 자식)만 decompose, 나머지는 unwrap으로 내용 보존.
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # <body> 기준으로 추출 — 일부 사이트는 html.parser가 전체 내용을 <head> 안으로
    # 파싱하는 버그가 있어서 soup 전체 대신 body 태그를 기준으로 사용.
    # body가 없으면 soup 전체 fallback.
    root = soup.find("body") or soup

    REMOVE_TAGS = [
        "script", "style", "noscript", "iframe",  # 비텍스트
        "nav", "header", "footer", "aside",        # 레이아웃
        "form", "button", "input", "select",       # UI 컨트롤
        "svg", "img", "figure", "picture",         # 미디어
        "head",                                    # 잘못된 위치의 head 태그
    ]
    for tag in root(REMOVE_TAGS):
        tag.decompose()

    text = root.get_text(separator="\n")

    # 각 줄 앞뒤 공백 제거 + 빈 줄 제거
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)

    # 연속 줄바꿈 2개로 압축, 연속 공백 1개로 압축
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)

    # AI 입력 토큰 절감: 최대 8000자로 제한
    return cleaned[:8000]
