import os
from datetime import date, timedelta

import requests

# 인메모리 캐시 — 당일 재사용
_cache: dict = {}

# API 조회 실패 시 사용하는 폴백 환율
FALLBACK_RATES = {"USD": 1380, "EUR": 1510}

# 한국은행 ECOS API — 주요국통화의대원화환율 (STAT_CODE: 036Y001)
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
        return _cache["rates"]

    api_key = os.environ.get("BOK_API_KEY")
    if not api_key:
        print("[EXCHANGE] BOK_API_KEY 없음 — 폴백 환율 사용")
        return FALLBACK_RATES

    rates = {}
    # 당일 고시 전일 수 있으므로 최근 3영업일 역순으로 시도
    for days_back in range(4):
        target = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
        try:
            for currency, item_code in _ITEM_CODES.items():
                if currency in rates:
                    continue
                url = (
                    f"https://ecos.bok.or.kr/api/StatisticSearch"
                    f"/{api_key}/json/kr/1/1/036Y001/D/{target}/{target}/{item_code}"
                )
                resp = requests.get(url, timeout=5)
                resp.raise_for_status()
                rows = resp.json().get("StatisticSearch", {}).get("row", [])
                if rows:
                    rates[currency] = round(float(rows[0]["DATA_VALUE"]))
        except Exception as e:
            print(f"[EXCHANGE] 환율 조회 실패 ({target}): {e}")
            continue

        if len(rates) == len(_ITEM_CODES):
            break

    if not rates:
        print("[EXCHANGE] 환율 조회 전체 실패 — 폴백 환율 사용")
        return FALLBACK_RATES

    merged = {**FALLBACK_RATES, **rates}
    _cache = {"date": today, "rates": merged}
    print(f"[EXCHANGE] 환율 조회 완료: {merged}")
    return merged
