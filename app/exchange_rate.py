import os
from datetime import date, timedelta

import requests

# 인메모리 캐시 — 당일 재사용
_cache: dict = {}

# API 조회 실패 시 사용하는 폴백 환율
FALLBACK_RATES = {"USD": 1470, "EUR": 1740}

# 한국은행 ECOS API — 주요국통화의대원화환율 (STAT_CODE: 731Y001)
_STAT_CODE = "731Y001"
_ITEM_CODES = {
    "USD": "0000001",
    "EUR": "0000003",
}


def get_exchange_rates() -> dict:
    """
    당일 USD/KRW, EUR/KRW 환율을 반환한다.
    당일 고시 전이면 최근 영업일 기준으로 조회하며, 결과는 당일 캐시된다.

    Returns:
        {"USD": int, "EUR": int}  (단위: 원)
    """
    global _cache
    today = date.today().isoformat()

    if _cache.get("date") == today:
        print(f"[EXCHANGE] 캐시 사용: {_cache['rates']}")
        return _cache["rates"]

    api_key = os.environ.get("BOK_API_KEY")
    if not api_key:
        print("[EXCHANGE] BOK_API_KEY 환경변수 없음 — 폴백 환율 사용")
        return FALLBACK_RATES

    print(f"[EXCHANGE] BOK_API_KEY 확인됨 (길이: {len(api_key)}자)")

    rates = {}
    # 오늘부터 최대 7일 전까지 역순으로 가장 최근 영업일 데이터 조회
    for days_back in range(7):
        target = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
        print(f"[EXCHANGE] 조회 시도: {target}")

        day_has_data = True
        for currency, item_code in _ITEM_CODES.items():
            if currency in rates:
                continue
            url = (
                f"https://ecos.bok.or.kr/api/StatisticSearch"
                f"/{api_key}/json/kr/1/1/{_STAT_CODE}/D/{target}/{target}/{item_code}"
            )
            masked_url = url.replace(api_key, "***")
            print(f"[EXCHANGE] 요청 URL: {masked_url}")
            try:
                resp = requests.get(url, timeout=10)
                print(f"[EXCHANGE] HTTP 상태: {resp.status_code}")
                body = resp.json()
                print(f"[EXCHANGE] 응답 JSON: {body}")

                # ECOS는 오류도 HTTP 200으로 반환하므로 RESULT 키로 판별
                if "RESULT" in body:
                    result = body["RESULT"]
                    print(f"[EXCHANGE] API 오류 — CODE: {result.get('CODE')}, MESSAGE: {result.get('MESSAGE')}")
                    day_has_data = False
                    break  # 이 날짜는 데이터 없음 → 하루 더 거슬러 올라감

                rows = body.get("StatisticSearch", {}).get("row", [])
                if rows:
                    value = round(float(rows[0]["DATA_VALUE"]))
                    rates[currency] = value
                    print(f"[EXCHANGE] {currency}: {value:,}원 (날짜: {target})")
                else:
                    print(f"[EXCHANGE] {currency}: 데이터 없음 (날짜: {target})")
                    day_has_data = False
                    break

            except Exception as e:
                print(f"[EXCHANGE] {currency} 요청 예외: {type(e).__name__}: {e}")
                day_has_data = False
                break

        if day_has_data and len(rates) == len(_ITEM_CODES):
            break

    if not rates:
        print("[EXCHANGE] 환율 조회 전체 실패 — 폴백 환율 사용")
        return FALLBACK_RATES

    merged = {**FALLBACK_RATES, **rates}
    _cache = {"date": today, "rates": merged}
    print(f"[EXCHANGE] 환율 조회 완료: {merged}")
    return merged
