"""
LLM-powered trip planner for Gozo Ferry Bot.

Workflow:
  1. User says "from X to Y by HH:MM" in free text after /plan
  2. LLM extracts structured fields (origin, destination, deadline)
  3. We geocode the places (Open-Meteo, free)
  4. We pick which ferry option(s) make sense based on island geography
  5. We query our existing schedule code for actual departures
  6. LLM formats a human-friendly answer with options and trade-offs

Failure modes are explicit: if the LLM can't parse, we ask for clarification.
If geocoding fails, we say so. If no ferry option fits, we explain why.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # fast and cheap
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

MALTA_TZ = ZoneInfo("Europe/Malta")


# --- Geographic helpers ---
def is_on_gozo(lat: float, lon: float) -> bool | None:
    """True if on Gozo, False if on Malta, None if outside both."""
    if not (35.78 <= lat <= 36.10 and 14.15 <= lon <= 14.58):
        return None
    return lat >= 36.00


# --- LLM call wrapper ---
async def _call_claude(
    system: str,
    user: str,
    max_tokens: int = 1024,
    history: list[dict] | None = None,
) -> str | None:
    """Single Claude API call. Returns text or None on failure.
    history: optional list of prior {"role": "user"|"assistant", "content": str}
    messages — prepended before the new user message."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — LLM disabled")
        return None

    messages = (history or []) + [{"role": "user", "content": user}]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": messages,
                },
            )
            r.raise_for_status()
            data = r.json()
            return data["content"][0]["text"]
    except httpx.HTTPStatusError as e:
        logger.warning(
            "Claude API HTTP %s: %s", e.response.status_code, e.response.text[:300]
        )
        return None
    except Exception as e:
        logger.warning("Claude API call failed: %s", e)
        return None


# --- Step 1: parse user request into structured fields ---
@dataclass
class ParsedRequest:
    origin: str
    destination: str
    deadline_hhmm: Optional[str]  # "HH:MM" 24h, or None if not specified
    error: Optional[str] = None    # user-facing message if parsing failed


PARSE_SYSTEM = """You extract trip planning info from short messages.
The user wants to get between Malta and Gozo (two islands in Malta).
You must respond with ONLY a single JSON object, no preamble, no markdown, no code fences.

Required fields:
- "origin": specific place name as the user wrote it (e.g. "Sliema", "Valletta", "Victoria", "Mġarr", "Xagħra"). NEVER include qualifiers like ", Gozo" or ", Malta". NEVER fabricate.
- "destination": same rules as origin.
- "deadline_hhmm": arrival deadline in "HH:MM" 24h format if user gave a time, else null.
- "error": null normally, OR a short human-friendly clarification question if the request is ambiguous or missing info.

CONTEXT HANDLING:
If you see prior conversation in the message history, USE IT. A user saying "yes" or "Malta one" after you asked "did you mean Malta Airport?" should resolve to that destination. Don't ask again — combine all the info you have.

DISAMBIGUATION:
- "Airport" in Malta means Malta International Airport (Luqa) — there is no airport on Gozo. Treat "airport" as that, no need to clarify.
- "Victoria" = the city in central Gozo (also called Rabat locally). Just return "Victoria".
- "Rabat" is ambiguous — there's one on Malta and one on Gozo. ASK which.

Examples:

Input: "from Sliema to Victoria by 14:00"
Output: {"origin": "Sliema", "destination": "Victoria", "deadline_hhmm": "14:00", "error": null}

Input: "I'm in Xaghra need airport by 17:00 tomorrow"
Output: {"origin": "Xagħra", "destination": "Malta International Airport", "deadline_hhmm": "17:00", "error": null}

Input: "how do I get to Gozo"
Output: {"origin": null, "destination": "Gozo", "deadline_hhmm": null, "error": "Where are you starting from?"}

Input: "from Sliema to Rabat"
Output: {"origin": "Sliema", "destination": null, "deadline_hhmm": null, "error": "There are two Rabats — the one on Malta or the one on Gozo?"}

Input: "хочу до Вікторії"
Output: {"origin": null, "destination": "Victoria", "deadline_hhmm": null, "error": "Where are you starting from?"}
"""


async def parse_request(
    text: str, history: list[dict] | None = None,
) -> ParsedRequest:
    raw = await _call_claude(PARSE_SYSTEM, text, max_tokens=300, history=history)
    if raw is None:
        return ParsedRequest("", "", None, "AI is unavailable right now. Try again in a moment.")

    # Log raw response so we can debug LLM weirdness in Render logs
    logger.info("LLM parse input=%r raw=%r", text, raw[:300])

    # Extract JSON object from response. Claude often wraps JSON in
    # ```json ... ``` fences despite instructions not to. Find the first
    # {...} block and try to parse that.
    cleaned = raw.strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON: %r", raw[:200])
        return ParsedRequest("", "", None, "Sorry, I couldn't understand that. Try: 'from Sliema to Victoria by 14:00'.")

    if data.get("error"):
        return ParsedRequest(
            data.get("origin") or "", data.get("destination") or "",
            data.get("deadline_hhmm"), data["error"],
        )
    if not data.get("origin") or not data.get("destination"):
        logger.warning("LLM missing origin/destination: %r", data)
        return ParsedRequest("", "", None, "I need both origin and destination. Try: 'from Sliema to Victoria by 14:00'.")

    return ParsedRequest(
        origin=data["origin"],
        destination=data["destination"],
        deadline_hhmm=data.get("deadline_hhmm"),
    )


# --- Step 2: geocode ---
@dataclass
class GeocodedPlace:
    name: str
    lat: float
    lon: float
    on_gozo: Optional[bool]  # True/False/None


async def geocode(place: str) -> GeocodedPlace | None:
    """Geocode via Open-Meteo (free, no key). Filters to Malta.
    Tries the full string first, then progressively simpler variants
    (strips trailing qualifiers like ', Gozo' / ', Malta')."""

    def _variants(p: str) -> list[str]:
        out = [p.strip()]
        # Strip trailing ", X" qualifiers since they often confuse geocoder
        if "," in p:
            out.append(p.split(",")[0].strip())
        # Strip leading articles
        first = out[-1]
        for prefix in ("the ", "The "):
            if first.startswith(prefix):
                out.append(first[len(prefix):])
        return list(dict.fromkeys(v for v in out if v))  # dedup, keep order

    for query in _variants(place):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={
                        "name": query,
                        "count": 10,
                        "language": "en",
                        "format": "json",
                    },
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            logger.warning("Geocoding failed for %r: %s", query, e)
            continue

        results = data.get("results") or []
        # Prefer Malta (MT) results above all else
        malta_hits = [h for h in results if h.get("country_code") == "MT"]
        chosen = (malta_hits[0] if malta_hits
                  else (results[0] if results else None))

        if chosen:
            lat, lon = chosen["latitude"], chosen["longitude"]
            logger.info(
                "Geocoded %r → %s (%.4f, %.4f, %s)",
                place, chosen.get("name"), lat, lon, chosen.get("country_code"),
            )
            return GeocodedPlace(
                name=chosen.get("name") or place,
                lat=lat, lon=lon,
                on_gozo=is_on_gozo(lat, lon),
            )

    logger.warning("All geocoding variants failed for %r", place)
    return None


# --- Step 3: figure out which ferry options apply ---
@dataclass
class FerryOption:
    operator: str            # "Gozo Channel" or "Fast Ferry"
    from_terminal: str       # human name
    to_terminal: str
    direction_key: str       # for our schedule code, e.g. "mgarr_to_cirkewwa"
    crossing_minutes: int


def applicable_options(
    origin_on_gozo: Optional[bool], dest_on_gozo: Optional[bool],
) -> list[FerryOption]:
    """
    Both operators run between the islands. We list both — the LLM in step 5
    will reason about which is more convenient given the user's actual coords.
    """
    if origin_on_gozo is None or dest_on_gozo is None:
        return []
    if origin_on_gozo == dest_on_gozo:
        return []  # no ferry needed (same island) — handled separately

    if origin_on_gozo:  # Gozo → Malta
        return [
            FerryOption("Gozo Channel", "Mġarr", "Ċirkewwa",
                        "mgarr_to_cirkewwa", 25),
            FerryOption("Fast Ferry", "Mġarr", "Valletta",
                        "ff_mgarr_to_valletta", 45),
        ]
    else:  # Malta → Gozo
        return [
            FerryOption("Gozo Channel", "Ċirkewwa", "Mġarr",
                        "cirkewwa_to_mgarr", 25),
            FerryOption("Fast Ferry", "Valletta", "Mġarr",
                        "ff_valletta_to_mgarr", 45),
        ]


# --- Step 4: build context for the LLM (departures + meta) ---
def build_planning_context(
    parsed: ParsedRequest,
    origin_geo: GeocodedPlace,
    dest_geo: GeocodedPlace,
    ferry_data: dict,
    sea_conditions: dict | None,
    now: datetime,
) -> dict:
    """
    Bundle everything the LLM needs to write the final plan into a dict.
    `ferry_data`: {operator_label: [{"departing": "HH:MM", "vessel": str|None, "seats": int|None}]}
    """
    return {
        "now_hhmm": now.strftime("%H:%M"),
        "today_date": now.strftime("%A %d %B"),
        "origin": {
            "input": parsed.origin,
            "matched_to": origin_geo.name,
            "on_gozo": origin_geo.on_gozo,
        },
        "destination": {
            "input": parsed.destination,
            "matched_to": dest_geo.name,
            "on_gozo": dest_geo.on_gozo,
        },
        "deadline": parsed.deadline_hhmm,
        "ferry_options": ferry_data,
        "sea_conditions": sea_conditions,
    }


# --- Step 5: LLM writes the human-readable plan ---
PLAN_SYSTEM = """You are a friendly travel assistant helping someone plan a trip between Malta and Gozo (Mediterranean islands).

You will receive a JSON object with:
- The user's origin and destination (with island detection)
- Available ferry departures from each operator
- Current sea conditions
- Optional deadline

Your job: write a SHORT, practical Telegram message (Markdown formatting) that helps them choose.

RULES:
- Use *bold* for emphasis, never headings (#).
- Be honest — if no option meets the deadline, SAY SO clearly. Don't pad.
- Recommend ONE option as primary unless they're truly equivalent.
- Mention sea conditions ONLY if rough/uncomfortable (waves > 1m).
- Don't invent bus times, walking times, or routes — you don't have that data.
  If the user is far from a terminal, just say "you'll need to get to <terminal> first".
- Keep it under 12 lines total. Telegram users skim.
- End with: "_Plus the time to/from the terminal — not included in this estimate._"

PICKING BETWEEN OPERATORS:
- Gozo Channel: car ferry, runs 24/7, Ċirkewwa ↔ Mġarr (~25 min). On Malta side it's at the very north, ~1h by bus from Valletta.
- Fast Ferry: passenger only, Valletta ↔ Mġarr (~45 min). Convenient if user is in/near Valletta.
- If user is near Valletta or Sliema and on foot → Fast Ferry usually wins.
- If user is in central/north Malta or has a car → Gozo Channel usually wins.

OUTPUT FORMAT EXAMPLE:

🗺 *Plan: Sliema → Victoria*

*Recommended: Fast Ferry*
Get to Valletta Ferry Terminal (bus 13 takes ~20 min from Sliema)
➡️ 13:00 Valletta → Mġarr (45 min, arrive 13:45)
Then bus from Mġarr to Victoria (~15 min)
Estimated arrival: ~14:00 — meets your 14:00 deadline tightly.

*Alternative: Gozo Channel*
Longer bus to Ċirkewwa (~75 min) but ferry runs more frequently.
Next sailing 13:30, you'd arrive Victoria ~14:25. Too late for 14:00.

_Plus the time to/from the terminal — not included in this estimate._
"""


async def write_plan(context: dict) -> str | None:
    payload = json.dumps(context, ensure_ascii=False, indent=2)
    return await _call_claude(PLAN_SYSTEM, payload, max_tokens=1200)
