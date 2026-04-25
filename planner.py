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
    deadline_hhmm: Optional[str]   # "HH:MM" 24h, or None if not specified
    deadline_date: Optional[str]   # "YYYY-MM-DD" or None (defaults to today)
    error: Optional[str] = None    # user-facing message if parsing failed


PARSE_SYSTEM = """You extract trip planning info from short messages.
The user wants to get between Malta and Gozo (two islands in Malta).
You must respond with ONLY a single JSON object, no preamble, no markdown, no code fences.

Required fields:
- "origin": specific place name as the user wrote it (e.g. "Sliema", "Valletta", "Victoria", "Mġarr", "Xagħra"). NEVER include qualifiers like ", Gozo" or ", Malta". NEVER fabricate. ALWAYS restore Maltese diacritics if you recognise the place: "Xaghra"→"Xagħra", "Mgarr"→"Mġarr", "Zebbug"→"Żebbuġ", "Ghajnsielem"→"Għajnsielem", "Gzira"→"Gżira", "Qrendi"→"Qrendi", etc.
- "destination": same rules as origin.
- "deadline_hhmm": arrival deadline in "HH:MM" 24h format if user gave a time, else null.
- "deadline_date": ISO date "YYYY-MM-DD" the deadline applies to. Resolve relative words ("today", "tomorrow", "next Monday") using the current date provided in CONTEXT. If no date is specified but a time is, default to "today" if that time hasn't passed yet, else "tomorrow". If no deadline at all, null.
- "error": null normally, OR a short human-friendly clarification question if the request is ambiguous or missing info.

CONTEXT HANDLING:
If you see prior conversation in the message history, USE IT. A user saying "yes" or "Malta one" after you asked "did you mean Malta Airport?" should resolve to that destination. Don't ask again — combine all the info you have.

DISAMBIGUATION:
- "Airport" in Malta means Malta International Airport (Luqa) — there is no airport on Gozo. Treat "airport" as that, no need to clarify.
- "Victoria" = the city in central Gozo (also called Rabat locally). Just return "Victoria".
- "Rabat" is ambiguous — there's one on Malta and one on Gozo. ASK which.

Examples (assume CONTEXT date is 2026-04-25, time 14:30):

Input: "from Sliema to Victoria by 14:00"
Output: {"origin": "Sliema", "destination": "Victoria", "deadline_hhmm": "14:00", "deadline_date": "2026-04-26", "error": null}
(reason: 14:00 already passed today, so default to tomorrow)

Input: "from Sliema to Victoria by 18:00"
Output: {"origin": "Sliema", "destination": "Victoria", "deadline_hhmm": "18:00", "deadline_date": "2026-04-25", "error": null}

Input: "I'm in Xaghra need airport by 17:00 tomorrow"
Output: {"origin": "Xagħra", "destination": "Malta International Airport", "deadline_hhmm": "17:00", "deadline_date": "2026-04-26", "error": null}

Input: "how do I get to Gozo"
Output: {"origin": null, "destination": "Gozo", "deadline_hhmm": null, "deadline_date": null, "error": "Where are you starting from?"}

Input: "from Sliema to Rabat"
Output: {"origin": "Sliema", "destination": null, "deadline_hhmm": null, "deadline_date": null, "error": "There are two Rabats — the one on Malta or the one on Gozo?"}
"""


async def parse_request(
    text: str, history: list[dict] | None = None,
) -> ParsedRequest:
    # Inject the current Malta date/time so the LLM can resolve "today",
    # "tomorrow", "by 14:00", etc. against a known reference.
    now = datetime.now(MALTA_TZ)
    contextualised = (
        f"CONTEXT: Current Malta date is {now.strftime('%Y-%m-%d')} "
        f"({now.strftime('%A')}), current time is {now.strftime('%H:%M')}.\n\n"
        f"User message: {text}"
    )
    raw = await _call_claude(
        PARSE_SYSTEM, contextualised, max_tokens=300, history=history
    )
    if raw is None:
        return ParsedRequest("", "", None, None, "AI is unavailable right now. Try again in a moment.")

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
        return ParsedRequest("", "", None, None, "Sorry, I couldn't understand that. Try: 'from Sliema to Victoria by 14:00'.")

    if data.get("error"):
        return ParsedRequest(
            data.get("origin") or "",
            data.get("destination") or "",
            data.get("deadline_hhmm"),
            data.get("deadline_date"),
            data["error"],
        )
    if not data.get("origin") or not data.get("destination"):
        logger.warning("LLM missing origin/destination: %r", data)
        return ParsedRequest("", "", None, None, "I need both origin and destination. Try: 'from Sliema to Victoria by 14:00'.")

    return ParsedRequest(
        origin=data["origin"],
        destination=data["destination"],
        deadline_hhmm=data.get("deadline_hhmm"),
        deadline_date=data.get("deadline_date"),
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

    # Common Maltese letters often typed without diacritics by tourists.
    # We try the user's spelling first, then add a diacritic-restored variant.
    _MALTESE_FIXUPS = [
        ("xaghra", "Xagħra"),
        ("mgarr", "Mġarr"),
        ("zebbug", "Żebbuġ"),
        ("zejtun", "Żejtun"),
        ("zurrieq", "Żurrieq"),
        ("zabbar", "Żabbar"),
        ("birzebbuga", "Birżebbuġa"),
        ("zebbiegh", "Żebbiegħ"),
        ("ghajnsielem", "Għajnsielem"),
        ("ghasri", "Għasri"),
        ("gharb", "Għarb"),
        ("xewkija", "Xewkija"),
        ("rabat", "Rabat"),
        ("sannat", "Sannat"),
        ("qala", "Qala"),
        ("kercem", "Kerċem"),
    ]

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
        # Maltese diacritic restoration — tourists often type ASCII forms
        lower_base = out[-1].lower()
        for ascii_form, real_form in _MALTESE_FIXUPS:
            if ascii_form in lower_base:
                out.append(real_form)
                break
        # Always also try with ", Malta" appended — disambiguates small
        # Maltese places that collide with British/other names
        # (e.g. "St Julians" → otherwise matches a UK village)
        base = out[-1]
        if "malta" not in base.lower() and "gozo" not in base.lower():
            out.append(f"{base}, Malta")
        return list(dict.fromkeys(v for v in out if v))  # dedup, keep order

    # First pass: try every variant, only accept Malta hits
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
        malta_hits = [h for h in results if h.get("country_code") == "MT"]

        if malta_hits:
            chosen = malta_hits[0]
            lat, lon = chosen["latitude"], chosen["longitude"]
            logger.info(
                "Geocoded %r → %s (%.4f, %.4f, MT)",
                place, chosen.get("name"), lat, lon,
            )
            return GeocodedPlace(
                name=chosen.get("name") or place,
                lat=lat, lon=lon,
                on_gozo=is_on_gozo(lat, lon),
            )

    # No Malta match anywhere — refuse rather than plan a trip from a
    # foreign place that happens to share the name.
    logger.warning("No Malta match for %r in any variant", place)
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
        "deadline_time": parsed.deadline_hhmm,
        "deadline_date": parsed.deadline_date,
        "ferry_options": ferry_data,
        "sea_conditions": sea_conditions,
    }


# --- Step 5: LLM writes the human-readable plan ---
PLAN_SYSTEM = """You are a friendly travel assistant helping someone plan a trip between Malta and Gozo (Mediterranean islands).

You will receive a JSON object with:
- The user's origin and destination (with island detection)
- Available ferry departures from each operator (today + tomorrow morning)
- `deadline_date` (YYYY-MM-DD) and `deadline_time` (HH:MM) — TRUST THESE. The parser already resolved "tomorrow"/"today". If `deadline_date` is in the future, do NOT say "you missed it" just because the time has passed today.
- Current sea conditions

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

DEADLINE LOGIC:
- If `deadline_date` is tomorrow or later, you have plenty of options — pick the most convenient morning departure.
- If `deadline_date` is today and there's no ferry that fits, say so honestly.
- If no deadline given, just show the next 1-2 sensible options.

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
