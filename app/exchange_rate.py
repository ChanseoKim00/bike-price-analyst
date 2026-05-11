import os
from datetime import date, timedelta

import requests

# In-memory cache - reused for the same day
_cache: dict = {}

# Fallback exchange rates used when the API lookup fails
FALLBACK_RATES = {"USD": 1470, "EUR": 1740}

# Bank of Korea ECOS API - major-currency to KRW exchange rates (STAT_CODE: 731Y001)
_STAT_CODE = "731Y001"
_ITEM_CODES = {
    "USD": "0000001",
    "EUR": "0000003",
}


def get_exchange_rates() -> dict:
    """
    Return today's USD/KRW and EUR/KRW exchange rates.
    Before today's rates have been published, look up the most recent business day.
    Results are cached for the day.

    Returns:
        {"USD": int, "EUR": int}  (unit: KRW)
    """
    global _cache
    today = date.today().isoformat()

    if _cache.get("date") == today:
        print(f"[EXCHANGE] using cache: {_cache['rates']}")
        return _cache["rates"]

    api_key = os.environ.get("BOK_API_KEY")
    if not api_key:
        print("[EXCHANGE] BOK_API_KEY env var missing - using fallback rates")
        return FALLBACK_RATES

    print(f"[EXCHANGE] BOK_API_KEY confirmed (length: {len(api_key)} chars)")

    rates = {}
    # Walk back up to 7 days from today to find the most recent business day with data
    for days_back in range(7):
        target = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
        print(f"[EXCHANGE] attempting lookup: {target}")

        day_has_data = True
        for currency, item_code in _ITEM_CODES.items():
            if currency in rates:
                continue
            url = (
                f"https://ecos.bok.or.kr/api/StatisticSearch"
                f"/{api_key}/json/kr/1/1/{_STAT_CODE}/D/{target}/{target}/{item_code}"
            )
            masked_url = url.replace(api_key, "***")
            print(f"[EXCHANGE] request URL: {masked_url}")
            try:
                resp = requests.get(url, timeout=10)
                print(f"[EXCHANGE] HTTP status: {resp.status_code}")
                body = resp.json()
                print(f"[EXCHANGE] response JSON: {body}")

                # ECOS returns errors with HTTP 200 too, so detect via the RESULT key
                if "RESULT" in body:
                    result = body["RESULT"]
                    print(f"[EXCHANGE] API error - CODE: {result.get('CODE')}, MESSAGE: {result.get('MESSAGE')}")
                    day_has_data = False
                    break  # No data for this date -> step back another day

                rows = body.get("StatisticSearch", {}).get("row", [])
                if rows:
                    value = round(float(rows[0]["DATA_VALUE"]))
                    rates[currency] = value
                    print(f"[EXCHANGE] {currency}: {value:,} KRW (date: {target})")
                else:
                    print(f"[EXCHANGE] {currency}: no data (date: {target})")
                    day_has_data = False
                    break

            except Exception as e:
                print(f"[EXCHANGE] {currency} request exception: {type(e).__name__}: {e}")
                day_has_data = False
                break

        if day_has_data and len(rates) == len(_ITEM_CODES):
            break

    if not rates:
        print("[EXCHANGE] all exchange rate lookups failed - using fallback rates")
        return FALLBACK_RATES

    merged = {**FALLBACK_RATES, **rates}
    _cache = {"date": today, "rates": merged}
    print(f"[EXCHANGE] exchange rate lookup complete: {merged}")
    return merged
