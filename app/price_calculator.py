import json
import os
import time
from datetime import datetime, timedelta

import anthropic
from sqlalchemy import func

from .models import db, Part, PartPriceHistory

_client = None

TTL_DAYS = {
    "groupset": 90,
    "wheelset": 60,
    "frameset": 120,
    "saddle": 180,
    "handlebar": 180,
}

SEARCH_SYSTEM = """
You are an expert at researching bicycle component prices.
Look up the Korean retail price for the given component on the web, then output only the single line shown below.

Research criteria (in priority order):
1. Official importer or official dealer retail price
2. Standard retail price at major authorized retailers

Strictly excluded: parallel imports / used / temporary special-event or sale prices / overseas direct purchase

[Output rules - very important]
- Do not output reasoning, research summaries, or explanations. Use the search tool, then output the single result line directly.
- The final output must be exactly the following single line:
RESULT_JSON:{"price_krw": integer or null, "official_url": "URL or null"}
- Never add any other text (preface/markdown/comments).
""".strip()


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def _is_fresh(part: Part) -> bool:
    """Check whether the price is still valid based on ttl_days"""
    if part.last_verified_at is None:
        return False
    ttl = timedelta(days=part.ttl_days)
    return datetime.utcnow() - part.last_verified_at < ttl


def _search_price_with_ai(part_name: str, part_type: str) -> dict:
    """
    Look up the official retail price using Claude web search.
    web_search_20250305 is a server_tool_use mechanism that the Anthropic server runs directly,
    so it completes in a single API call and does not need a loop.

    Returns: {"price_krw": int or None, "official_url": str or None}
    """
    client = _get_client()

    # Web search - on RateLimitError, wait 60s and retry once; if the retry also fails, return null.
    # Cost controls:
    #   - max_uses=2: cap on search calls per component (default 5 -> avoids exploding tool_result accumulated input cost)
    #   - cache_control: cache SEARCH_SYSTEM (hits across the 4 component calls per analysis)
    #   - max_tokens=1024: combined with the no-reasoning prompt to cut output cost
    for attempt in range(2):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": SEARCH_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 2,
                }],
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Please research the Korean official retail price for the following bicycle component.\n"
                            f"Component type: {part_type}\n"
                            f"Component name: {part_name}"
                        ),
                    }
                ],
            )
            break
        except anthropic.APIStatusError as e:
            if e.status_code not in (429, 529):
                raise
            if attempt == 0:
                print(f"[RATE LIMIT] component search {e.status_code} ({part_name}) - waiting 60s before retry")
                time.sleep(60)
            else:
                print(f"[RATE LIMIT] component search retry also failed ({part_name}) - returning null")
                return {"price_krw": None, "official_url": None}

    # Concatenate text blocks only to extract the final response (skip blocks with text=None)
    search_result = "\n".join(
        block.text for block in response.content
        if hasattr(block, "text") and block.text is not None
    ).strip()

    if not search_result:
        return {"price_krw": None, "official_url": None}

    # Pull JSON via the RESULT_JSON: tag (no separate API call needed)
    marker = "RESULT_JSON:"
    idx = search_result.rfind(marker)
    if idx == -1:
        return {"price_krw": None, "official_url": None}

    raw = search_result[idx + len(marker):].strip()
    # Strip everything after the first newline
    raw = raw.splitlines()[0].strip()

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {"price_krw": None, "official_url": None}


# frameset is hard for AI web search to find, so we operate frameset entries via direct DB entry only
# (complete-bike frames are mostly sold only through official dealers -> handle via missing_parts)
SKIP_AI_SEARCH_TYPES = {"frameset"}

# Component types that should be re-searched even when stored as price_krw=null in the DB.
# saddle/handlebar stay null if not found.
RETRY_ON_NULL_TYPES = {"groupset", "wheelset"}


def record_part_price_history(
    part: Part,
    new_price: int | None,
    recorded_at: datetime | None = None,
    force: bool = False,
) -> bool:
    """
    Append a row to part_price_history when a component price is newly stored or changed.
    Skip if the price is the same. None prices are not recorded.
    The caller is responsible for session flush/commit.

    Args:
        force: if True, append a new row even when the price equals the previous row.
               Used by the worker to leave a "no change" stamp on the chart during natural ticks.

    Returns:
        bool: True if a history row was actually appended
    """
    if new_price is None:
        return False

    if not force:
        last = (
            PartPriceHistory.query
            .filter_by(part_id=part.id)
            .order_by(PartPriceHistory.recorded_at.desc())
            .first()
        )
        if last and last.price_krw == new_price:
            return False

    db.session.add(PartPriceHistory(
        part_id=part.id,
        price_krw=new_price,
        recorded_at=recorded_at or datetime.utcnow(),
    ))
    return True


def _normalize_part_name(raw: str) -> str:
    """
    Force-normalize the part_name_normalized produced by the AI.
    - Hyphens (-) -> underscores (_)
    - Lowercase everything
    - Trim leading/trailing whitespace
    - Strip tubeless-related markers (_tubeless_ready, _tubeless, _tlr, _tl)
    - Collapse runs of underscores into a single underscore
    """
    if raw is None:
        return None
    import re
    normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
    # Strip tubeless-related markers (order matters: longer patterns first)
    normalized = re.sub(r"_tubeless_ready", "", normalized)
    normalized = re.sub(r"_tubeless", "", normalized)
    normalized = re.sub(r"_tlr", "", normalized)
    normalized = re.sub(r"_tl\b", "", normalized)
    normalized = re.sub(r"_+", "_", normalized)
    normalized = normalized.strip("_")
    return normalized


def get_or_fetch_part(part_name: str, part_name_normalized: str, part_type: str) -> Part:
    """
    Look up the parts table - if missing or stale, run AI web search and store the result.
    framesets are always stored in the parts DB without AI search (price_krw=None allowed).

    Returns:
        Part: the Part object stored in the DB (price_krw may be None)
    """
    part_name_normalized = _normalize_part_name(part_name_normalized)

    # 1-a. Exact match lookup
    part = Part.query.filter_by(
        part_name_normalized=part_name_normalized,
        part_type=part_type,
    ).first()

    prefix_matched = False

    if part is None:
        # 1-b. Case where AI value is a prefix of the DB normalized value
        #      e.g. AI: dt_swiss_arc_1100_dicut  DB: dt_swiss_arc_1100_dicut_db_55
        # Use substr for exact comparison to avoid LIKE's `_` wildcard mis-match.
        ai_prefix = part_name_normalized + "_"
        ai_prefix_len = len(ai_prefix)
        part = Part.query.filter(
            Part.part_type == part_type,
            func.length(Part.part_name_normalized) > ai_prefix_len,
            func.substr(Part.part_name_normalized, 1, ai_prefix_len) == ai_prefix,
        ).first()
        if part:
            prefix_matched = True

    if part is None:
        # 1-c. Case where DB normalized is a prefix of the AI value
        #      e.g. AI: dt_swiss_arc_1100_dicut_db_55  DB: dt_swiss_arc_1100_dicut
        ai_total_len = len(part_name_normalized)
        part = Part.query.filter(
            Part.part_type == part_type,
            func.length(Part.part_name_normalized) < ai_total_len,
            func.substr(
                db.literal(part_name_normalized),
                1,
                func.length(Part.part_name_normalized) + 1,
            ) == func.concat(Part.part_name_normalized, "_"),
        ).first()
        if part:
            prefix_matched = True

    if part and prefix_matched:
        print(f"[PREFIX HIT] parts - {part_type}: {part_name} -> matched: {part.part_name_normalized}")

    if part and _is_fresh(part):
        print(f"[CACHE HIT]  parts - {part_type}: {part_name} ({part.price_krw:,} KRW)" if part.price_krw else f"[CACHE HIT]  parts - {part_type}: {part_name} (no price)")
        part.last_checked_at = datetime.utcnow()
        db.session.commit()
        return part

    # 2. frameset skips AI search - store with null price if missing; if stale, just refresh last_checked_at
    if part_type in SKIP_AI_SEARCH_TYPES:
        print(f"[SKIP]       parts - {part_type}: {part_name} (excluded from AI search, storing null price)")
        now = datetime.utcnow()
        if part:
            part.last_checked_at = now
            db.session.commit()
            return part
        part = Part(
            part_type=part_type,
            part_name=part_name,
            part_name_normalized=part_name_normalized,
            price_krw=None,
            last_checked_at=now,
            ttl_days=TTL_DAYS.get(part_type, 90),
        )
        db.session.add(part)
        db.session.commit()
        return part

    # 3. Components stored as null in DB whose type does not retry -> return as-is
    if part and part.price_krw is None and part_type not in RETRY_ON_NULL_TYPES:
        print(f"[CACHE HIT]  parts - {part_type}: {part_name} (no price, will not re-search)")
        return part

    # 4. AI web search for price (single attempt; on failure, treat as null)
    print(f"[CACHE MISS] parts - {part_type}: {part_name} (starting AI web search)")
    result = _search_price_with_ai(part_name, part_type)
    now = datetime.utcnow()

    if part:
        # stale -> only update price on successful re-search; keep existing price on failure
        if result["price_krw"]:
            price_changed = part.price_krw != result["price_krw"]
            part.price_krw = result["price_krw"]
            part.official_url = result["official_url"]
            part.last_verified_at = now
            if price_changed:
                db.session.flush()  # ensure part.id is set
                record_part_price_history(part, result["price_krw"], recorded_at=now)
        part.last_checked_at = now
    else:
        # New record
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
        if result["price_krw"]:
            db.session.flush()  # ensure part.id is set
            record_part_price_history(part, result["price_krw"], recorded_at=now)

    db.session.commit()
    return part


def calculate_parts_sum(parts: list[Part]) -> tuple[int, list[str]]:
    """
    Sum prices and compute missing_parts from a list of components.

    Returns:
        (parts_sum_krw, missing_parts)
        missing_parts: list of component types whose price could not be found
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
