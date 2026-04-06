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
TIMEOUT = 15  # seconds


class ScrapeError(Exception):
    """스크래핑 실패 시 발생 — routes.py에서 케이스 6 처리용"""
    pass


def fetch_html(url: str) -> str:
    """
    URL에서 HTML을 가져와 본문 텍스트만 정제해서 반환.

    Returns:
        str: 정제된 HTML 문자열

    Raises:
        ScrapeError: 네트워크 오류, 봇 차단(403/429), 링크 만료(404) 등
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        raise ScrapeError(f"연결 실패: {url}")
    except requests.exceptions.Timeout:
        raise ScrapeError(f"응답 시간 초과 ({TIMEOUT}s): {url}")
    except requests.exceptions.RequestException as e:
        raise ScrapeError(f"요청 오류: {e}")

    if resp.status_code == 404:
        raise ScrapeError(f"페이지를 찾을 수 없습니다 (404): {url}")
    if resp.status_code in (403, 429):
        raise ScrapeError(f"봇 차단 또는 접근 거부 ({resp.status_code}): {url}")
    if not resp.ok:
        raise ScrapeError(f"HTTP {resp.status_code}: {url}")

    # EUC-KR 등 한국 사이트 인코딩 자동 감지
    resp.encoding = resp.apparent_encoding
    return _clean_html(resp.text)


def _clean_html(raw_html: str) -> str:
    """
    script / style 태그 제거 후 읽기 쉬운 텍스트 블록으로 정제.
    AI에 넘기는 텍스트 크기를 줄이기 위해 공백도 압축.
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)

    # AI 입력 토큰 절감: 최대 8000자로 제한
    return cleaned[:8000]
