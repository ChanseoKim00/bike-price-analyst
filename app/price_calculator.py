import json
import os
import time
from datetime import datetime, timedelta

import anthropic

from .models import db, Part

_client = None

TTL_DAYS = {
    "groupset": 90,
    "wheelset": 60,
    "frameset": 120,
    "saddle": 180,
    "handlebar": 180,
}

SEARCH_SYSTEM = """
당신은 자전거 부품 가격 조사 전문가입니다.
주어진 부품의 한국 판매가를 웹에서 조사한 뒤, 반드시 응답 마지막에 아래 JSON 블록을 출력하세요.

조사 기준 (우선순위 순):
1. 공식 수입사 또는 공식 대리점 판매가
2. 주요 정규 판매처 시중가

반드시 제외: 병행수입 / 중고 / 한시적 특가·할인 행사가 / 해외 직구가

조사 후 응답 맨 마지막에 반드시 이 형식을 출력하세요:
RESULT_JSON:{"price_krw": 정수또는null, "official_url": "URL또는null"}
""".strip()


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _is_fresh(part: Part) -> bool:
    """ttl_days 기준으로 가격 유효 여부 확인"""
    if part.last_verified_at is None:
        return False
    ttl = timedelta(days=part.ttl_days)
    return datetime.utcnow() - part.last_verified_at < ttl


def _search_price_with_ai(part_name: str, part_type: str) -> dict:
    """
    Claude 웹 검색으로 공식 판매가 조회.
    web_search_20250305는 Anthropic 서버가 직접 실행하는 server_tool_use 방식 —
    단일 API 호출로 완료되므로 루프 불필요.

    Returns: {"price_krw": int or None, "official_url": str or None}
    """
    client = _get_client()

    # 웹 검색 — RateLimitError 시 60초 대기 후 1회 재시도, 재시도도 실패하면 null 반환
    for attempt in range(2):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=SEARCH_SYSTEM,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"다음 자전거 부품의 한국 공식 판매가를 조사해주세요.\n"
                            f"부품 종류: {part_type}\n"
                            f"부품명: {part_name}"
                        ),
                    }
                ],
            )
            break
        except (anthropic.RateLimitError, anthropic.OverloadedError):
            if attempt == 0:
                print(f"[RATE LIMIT] 부품 검색 429/529 ({part_name}) — 60초 대기 후 재시도")
                time.sleep(60)
            else:
                print(f"[RATE LIMIT] 부품 검색 재시도도 실패 ({part_name}) — null 반환")
                return {"price_krw": None, "official_url": None}

    # 텍스트 블록만 이어붙여 최종 응답 추출 (text가 None인 블록 제외)
    search_result = "\n".join(
        block.text for block in response.content
        if hasattr(block, "text") and block.text is not None
    ).strip()

    if not search_result:
        return {"price_krw": None, "official_url": None}

    # RESULT_JSON: 태그로 JSON 추출 (별도 API 호출 없이)
    marker = "RESULT_JSON:"
    idx = search_result.rfind(marker)
    if idx == -1:
        return {"price_krw": None, "official_url": None}

    raw = search_result[idx + len(marker):].strip()
    # 줄바꿈 이후 내용 제거
    raw = raw.splitlines()[0].strip()

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {"price_krw": None, "official_url": None}


# frameset은 AI 웹 검색으로 찾기 어려운 경우가 많아 DB 직접 입력 방식으로만 운영
# (완성차 프레임은 공식 대리점 한정 판매가 대부분 → missing_parts로 처리)
SKIP_AI_SEARCH_TYPES = {"frameset"}

# DB에 price_krw=null로 저장돼 있어도 재검색하는 부품 타입
# saddle/handlebar는 못 찾으면 null 그대로 유지
RETRY_ON_NULL_TYPES = {"groupset", "wheelset"}


def get_or_fetch_part(part_name: str, part_name_normalized: str, part_type: str) -> Part:
    """
    parts 테이블 조회 → 없거나 stale이면 AI 웹 검색 후 저장.
    frameset은 DB에 있을 때만 사용, 없으면 null 반환 (missing_parts 처리됨).

    Returns:
        Part: DB에 저장된 Part 객체 (price_krw가 None일 수 있음)
    """
    # 1. DB 조회
    part = Part.query.filter_by(part_name_normalized=part_name_normalized).first()

    if part and _is_fresh(part):
        print(f"[CACHE HIT]  parts — {part_type}: {part_name} ({part.price_krw:,}원)" if part.price_krw else f"[CACHE HIT]  parts — {part_type}: {part_name} (가격 없음)")
        part.last_checked_at = datetime.utcnow()
        db.session.commit()
        return part

    # 2. frameset은 AI 검색 건너뜀 — DB에 없으면 None 반환
    if part_type in SKIP_AI_SEARCH_TYPES:
        print(f"[SKIP]       parts — {part_type}: {part_name} (AI 검색 제외)")
        return None

    # 3. DB에 null로 저장된 부품 중 재검색 안 하는 타입 → 그대로 반환
    if part and part.price_krw is None and part_type not in RETRY_ON_NULL_TYPES:
        print(f"[CACHE HIT]  parts — {part_type}: {part_name} (가격 없음, 재검색 안 함)")
        return part

    # 4. AI 웹 검색으로 가격 조회 (1회만 시도, 실패 시 null 처리)
    print(f"[CACHE MISS] parts — {part_type}: {part_name} (AI 웹 검색 시작)")
    result = _search_price_with_ai(part_name, part_type)
    now = datetime.utcnow()

    if part:
        # stale → 업데이트
        part.price_krw = result["price_krw"]
        part.official_url = result["official_url"]
        part.last_verified_at = now if result["price_krw"] else part.last_verified_at
        part.last_checked_at = now
    else:
        # 신규 저장
        part = Part(
            part_type=part_type,
            part_name=part_name,
            part_name_normalized=part_name_normalized,
            price_krw=result["price_krw"],
            official_url=result["official_url"],
            last_verified_at=now if result["price_krw"] else None,
            last_checked_at=now,
            ttl_days=TTL_DAYS.get(part_type, 90),
        )
        db.session.add(part)

    db.session.commit()
    return part


def calculate_parts_sum(parts: list[Part]) -> tuple[int, list[str]]:
    """
    부품 리스트에서 가격 합산 및 missing_parts 계산.

    Returns:
        (parts_sum_krw, missing_parts)
        missing_parts: 가격을 찾지 못한 부품 타입 목록
    """
    total = 0
    missing = []

    for part in parts:
        if part is None or part.price_krw is None:
            if part is not None:
                missing.append(part.part_type)
        else:
            total += part.price_krw

    return total, missing
