import ipaddress
import re
import socket

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
TIMEOUT = 15      # requests timeout (seconds)
PW_TIMEOUT = 30   # Playwright timeout (seconds)
JS_THRESHOLD = 500  # Fall back to Playwright if text length is below this


class ScrapeError(Exception):
    """Raised when scraping fails - used for case 6 handling in routes.py"""
    def __init__(self, message, code="unknown"):
        super().__init__(message)
        self.code = code


def assert_safe_url(url: str) -> None:
    """
    SSRF guard - raise ScrapeError if a user-supplied URL points at private,
    loopback, link-local, or other internal-resource hosts.

    This does not fully prevent DNS rebinding, but invoking it at both the
    input and request stages blocks typical internal-network and metadata
    endpoints (e.g. 169.254.169.254).
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ScrapeError("Unsupported URL scheme", code="blocked")

    host = parsed.hostname
    if not host:
        raise ScrapeError("URL has no host.", code="blocked")

    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ScrapeError(f"Host lookup failed: {host}", code="connection_error")

    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr[4][0])
        except (ValueError, IndexError):
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ScrapeError("Requests to internal network addresses are not allowed.", code="blocked")


def fetch_html(url: str) -> str:
    """
    Fetch HTML from the URL and return only the cleaned body text.
    Try requests first; if the result is below JS_THRESHOLD, retry with Playwright.

    Returns:
        str: cleaned text

    Raises:
        ScrapeError: network errors, bot blocking (403/429), broken link (404), etc.
    """
    # SSRF guard - block immediately if host is private/loopback
    assert_safe_url(url)

    # First attempt: requests
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        raise ScrapeError(f"Connection failed: {url}", code="connection_error")
    except requests.exceptions.Timeout:
        raise ScrapeError(f"Response timed out ({TIMEOUT}s): {url}", code="timeout")
    except requests.exceptions.RequestException as e:
        raise ScrapeError(f"Request error: {e}", code="http_error")

    if resp.status_code == 404:
        raise ScrapeError(f"Page not found (404): {url}", code="not_found")
    if resp.status_code in (403, 429):
        raise ScrapeError(f"Bot blocking or access denied ({resp.status_code}): {url}", code="blocked")
    if not resp.ok:
        raise ScrapeError(f"HTTP {resp.status_code}: {url}", code="http_error")

    decoded = _decode_response(resp)
    text = _clean_html(decoded)

    if len(text) >= JS_THRESHOLD:
        print(f"[SCRAPER] requests success ({len(text)} chars)")
        return text

    # Second attempt: Playwright (JS rendering)
    print(f"[SCRAPER] requests yielded {len(text)} chars - trying Playwright fallback")
    pw_text = _fetch_with_playwright(url)
    if pw_text:
        print(f"[SCRAPER] Playwright success ({len(pw_text)} chars)")
        return pw_text

    # If Playwright also fails, return the requests result as-is
    print(f"[SCRAPER] Playwright failed - using requests result ({len(text)} chars)")
    return text


def _fetch_with_playwright(url: str) -> str:
    """
    Render via Playwright headless Chromium (JS rendering) and extract text.

    Returns:
        str: cleaned text. Empty string on failure.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("[SCRAPER] playwright package not installed - skipping fallback")
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
        print(f"[SCRAPER] Playwright error: {type(e).__name__}: {e}")
        return ""


def _decode_response(resp) -> str:
    """
    Decode the response body using the correct encoding.
    Try apparent_encoding first, then fall back to cp949 -> utf-8.
    """
    for enc in [resp.apparent_encoding, "cp949", "utf-8"]:
        if not enc:
            continue
        try:
            return resp.content.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    # Final fallback: utf-8 with errors=ignore
    return resp.content.decode("utf-8", errors="ignore")


def _clean_html(raw_html: str) -> str:
    """
    Strip unnecessary tags and extract only text to minimize AI tokens.
    - Tags removed: layout/UI elements (nav, header, footer, etc.) + non-text elements (script, style, etc.)
    - Collapse runs of whitespace and line breaks

    Note: some sites have <head>/<link> tags duplicated inside <body>, and
    BeautifulSoup(html.parser) parses everything after that as children of those tags.
    -> Decompose only the real <head> (a direct child of <html>); unwrap the rest to preserve content.
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # Extract relative to <body> - on some sites html.parser has a bug
    # where it parses the entire document inside <head>, so use the body tag
    # instead of the whole soup. Fall back to whole soup if body is missing.
    root = soup.find("body") or soup

    REMOVE_TAGS = [
        "script", "style", "noscript", "iframe",  # non-text
        "nav", "header", "footer", "aside",        # layout
        "form", "button", "input", "select",       # UI controls
        "svg", "img", "figure", "picture",         # media
        "head",                                    # misplaced head tag
    ]
    for tag in root(REMOVE_TAGS):
        tag.decompose()

    text = root.get_text(separator="\n")

    # Trim leading/trailing whitespace per line and remove blank lines
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)

    # Collapse runs of newlines to 2 and runs of spaces to 1
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)

    # Reduce AI input tokens: cap at 8000 chars
    return cleaned[:8000]
