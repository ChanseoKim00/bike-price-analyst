import json
import os
import time
from datetime import datetime
import anthropic

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


class AnalysisError(Exception):
    """Raised when required fields (brand / model_name / groupset) cannot be extracted -> case 6 handling"""
    pass


class ServiceBusyError(Exception):
    """Raised when RateLimitError still fails after retry -> error page handling"""
    pass


SYSTEM_PROMPT = """
You are an expert at extracting information from bicycle product pages.
Respond using only the JSON format below. Output JSON only, with no explanation or markdown.

Extraction rules:
- brand: lowercase English, underscores instead of spaces (e.g. "specialized", "fantasia", "elfama")
- model_name: keep the original notation (e.g. "Radar 9 ARC Gen.3")
- model_year: integer. Search in the following order:
  1) Year explicitly stated on the page (e.g. "2025 model year", "2026 model")
  2) Release year/month info (e.g. "released 2025-04")
  3) Earliest year inferred from review/comment/inquiry dates (e.g. comment from 2025 -> 2025)
  4) null if none of the above
- price_krw: integer, based on the discounted price (null if absent). If the price is shown in foreign currency (USD, EUR, etc.), apply the exchange rate provided below to convert into KRW and return as an integer.
- frame_material: "carbon" | "alloy" | "steel" | "titanium" | "other" | "unknown"
- frame_material_confidence: 0.0~1.0 (1.0 if explicitly stated, 0.7 if inferred from model name, 0.4 if guessed)
- frame_material_source: "page_text" | "model_knowledge" | "unknown"
- brake_type: "hydraulic_disc" | "mechanical_disc" | "rim" | "unknown"

Component fields (each with part_name + part_name_normalized):
- part_name: the original notation as written on the page
- part_name_normalized: must use only lowercase English letters and underscores (_). Spaces, hyphens (-), and uppercase are strictly forbidden. Self-verify there are no spaces before output.

  [Include]
  - Brand name
  - Tier (s-works / pro / expert / comp etc. — within-brand tier)
  - Product line name
  - Electronic/mechanical distinction (di2, etc.)
  - Material (manganese / carbon, etc. — material that determines spec)
  - Rim depth (45 / 55 / 62, etc. — numeric size)

  [Exclude]
  - Generic suffixes such as 'rail', 'system', 'integrated'
  - Full-name-only modifiers such as 'DICUT', 'DB'
  - Model numbers (R9200, R9250, R8150, R7100, etc.)
  - Derived options (power meter, crank set, etc.)
  - Tubeless-related notations: 'Tubeless', 'Tubeless Ready', 'TL', 'TLR', etc. (these are tire compatibility markers, unnecessary for component model identification)

  Drivetrain examples:
    "Shimano Dura-Ace Di2 R9250" -> "shimano_dura_ace_di2"
    "SRAM Red eTap AXS" -> "sram_red_etap_axs"
    Note: Both R9200 and R9250 normalize to "shimano_dura_ace_di2".
    Note: If bike brand/model/edition names are mixed in, extract only the drivetrain brand and lineup.
    "RCR Pro Dura-Ace Di2 Team Edition" -> "shimano_dura_ace_di2"
    "Canyon Ultimate CFR Dura-Ace Di2" -> "shimano_dura_ace_di2"
    "Specialized Tarmac SL8 Red AXS"   -> "sram_red_etap_axs"

  Wheelset examples:
    "CADEX Max 50 WheelSystem" -> "cadex_max_50"
    "DT Swiss ARC 1100 DICUT DB 55" -> "dt_swiss_arc_1100_55"
    "DT Swiss ARC 1400 DICUT DB 62" -> "dt_swiss_arc_1400_62"
    "Roval Rapide CLX II Tubeless" -> "roval_rapide_clx_ii"

  Saddle examples:
    "Selle Italia Novus Boost EVO Superflow Manganese rail" -> "selle_italia_novus_boost_evo_superflow_manganese"
    "Selle Italia Novus Boost EVO Superflow Carbon rail" -> "selle_italia_novus_boost_evo_superflow_carbon"
    "Specialized S-Works Power Mirror" -> "specialized_s_works_power_mirror"
    "Specialized Pro Power Mirror" -> "specialized_pro_power_mirror"
    "Specialized Expert Power Mirror" -> "specialized_expert_power_mirror"
    "Specialized Comp Power Mirror" -> "specialized_comp_power_mirror"

  Fizik saddle normalized rule: fizik_(category)_(lineup)_(rail_grade)_(adaptive flag)
    category: vento / tempo / transiro - default to vento if unspecified
    lineup: argo / aeris / antares etc. - default to argo if unspecified
    rail grade: 00 / r1 / r3 / r5 etc. - default to r5 if unspecified
    adaptive: include only if explicitly stated, otherwise omit
    "Fizik Vento Argo R1 Adaptive" -> "fizik_vento_argo_r1_adaptive"
    "Fizik Vento Argo R1"          -> "fizik_vento_argo_r1"
    "Fizik Vento Argo 00"          -> "fizik_vento_argo_00"
    "Fizik Tempo Argo R1"          -> "fizik_tempo_argo_r1"
    "Fizik Transiro Antares R3"    -> "fizik_transiro_antares_r3"
    "Fizik Vento Aeris R3 Adaptive" -> "fizik_vento_aeris_r3_adaptive"
    "Fizik" (insufficient info)     -> "fizik_vento_argo_r5"

  Handlebar examples:
    "Controltech Sirocco FL4" -> "controltech_sirocco_fl4"
    "Giant Contact SLR 0 Aero Integrated" -> "giant_contact_slr_0_aero"

- Use null for any component not stated on the page

{
  "brand": "string",
  "model_name": "string",
  "model_year": integer or null,
  "price_krw": integer or null,
  "frame_material": "string",
  "frame_material_confidence": float,
  "frame_material_source": "string",
  "brake_type": "string",
  "groupset": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  },
  "wheelset": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  },
  "frameset": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  },
  "saddle": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  },
  "handlebar": {
    "part_name": "string or null",
    "part_name_normalized": "string or null"
  }
}
""".strip()


YEAR_RETRY_PROMPT = """
The previous analysis could not find model_year.
Please find the model year again from the page text below.

Search order:
1. Explicit year notation such as "2025 model year", "2026 model"
2. Release year/month (e.g. "released 2025-04" -> 2025)
3. Earliest year inferred from review/comment/inquiry dates

Output only one integer for the year you found. Output null if not found.
Do not output any text other than the number or null.
""".strip()


def _call_api(client, system: str, user: str) -> str:
    # The system prompt is over 4,000 characters, so to avoid being charged the full cost on every call,
    # we apply ephemeral prompt caching - hits across the year-retry call and concurrent analyses.
    system_blocks = [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    for attempt in range(2):
        try:
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_blocks,
                messages=[{"role": "user", "content": user}],
            )
            raw = message.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return raw
        except anthropic.APIStatusError as e:
            if e.status_code not in (429, 529):
                raise
            if attempt == 0:
                print(f"[RATE LIMIT] AI analysis {e.status_code} - waiting 60s before retry")
                time.sleep(60)
            else:
                raise ServiceBusyError("The service is temporarily busy. Please try again in a moment.")


def extract_bike_info(page_text: str, exchange_rates: dict = None) -> dict:
    """
    Extract bicycle information from scraped page text.
    If model_year is null, retry once; if still missing, default to the current year.

    Args:
        page_text: scraped page text
        exchange_rates: exchange rate info shaped like {"USD": int, "EUR": int} (omit if absent)

    Returns:
        dict: extracted bicycle info (model_year is always an integer)

    Raises:
        AnalysisError: if any of brand / model_name / groupset cannot be extracted
    """
    client = _get_client()

    rate_note = ""
    if exchange_rates:
        lines = ", ".join(f"1 {k} = {v:,} KRW" for k, v in exchange_rates.items())
        rate_note = f"\n\n[Current exchange rates] {lines}"

    raw = _call_api(
        client,
        system=SYSTEM_PROMPT,
        user=f"Please extract information from the bicycle product page text below.{rate_note}\n\n{page_text}",
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise AnalysisError(f"Could not parse AI response as JSON: {raw[:200]}")

    # Validate required fields
    missing = []
    if not data.get("brand"):
        missing.append("brand")
    if not data.get("model_name"):
        missing.append("model_name")
    if not data.get("groupset", {}).get("part_name"):
        missing.append("groupset")

    if missing:
        raise AnalysisError(f"Required field extraction failed: {', '.join(missing)}")

    # Handle model_year: retry if null -> default to current year if still missing
    if not data.get("model_year"):
        retry_raw = _call_api(
            client,
            system=YEAR_RETRY_PROMPT,
            user=page_text,
        )
        try:
            year = int(retry_raw)
            data["model_year"] = year
        except (ValueError, TypeError):
            data["model_year"] = datetime.utcnow().year

    return data
