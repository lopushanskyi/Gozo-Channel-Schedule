"""
Gozo Ferry Bot — Telegram bot combining two ferry operators:
  • Gozo Channel (car ferry, Ċirkewwa ↔ Mġarr)    — static.gozochannel.com JSON
  • Gozo Fast Ferry (passenger, Valletta ↔ Mġarr) — gozohighspeed.com REST API

Sea conditions from Open-Meteo (Marine + Forecast APIs).
Webhook mode (for Render / any Web Service).
"""

import json
import logging
import os
import time
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import analytics
import planner

# --- Config ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Set the TELEGRAM_BOT_TOKEN environment variable (token from @BotFather)"
    )

WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
if not WEBHOOK_URL:
    raise RuntimeError("Set WEBHOOK_URL (full https://... URL of your service)")

PORT = int(os.environ.get("PORT", "8080"))

# Admin user (for /stats). 0 = disabled.
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))

MALTA_TZ = ZoneInfo("Europe/Malta")
SCHEDULE_FILE = Path(__file__).parent / "schedule.json"

CHANNEL_LAT = 36.015
CHANNEL_LON = 14.296

# Fast Ferry seat thresholds
FEW_SEATS_THRESHOLD = 30

# Caches
_live_schedule_cache: dict[str, dict] = {}            # Gozo Channel, key: date iso
_fast_ferry_cache: dict[tuple, tuple[list, float]] = {}  # (dep, arr, date_iso) → (trips, ts)
FAST_FERRY_TTL = 300  # 5 min (seats change as people book)

_weather_cache: dict = {"data": None, "ts": 0.0}
WEATHER_TTL = 600

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Gozo Channel schedule (live JSON) ---
async def fetch_live_schedule(date_obj: date_cls) -> dict | None:
    cache_key = date_obj.isoformat()
    if cache_key in _live_schedule_cache:
        return _live_schedule_cache[cache_key]

    url = (
        f"https://static.gozochannel.com/schedules/"
        f"{date_obj.year}/{date_obj.month:02d}/{date_obj.day:02d}/passenger.json"
    )
    # Some CDNs (Cloudflare-fronted ones in particular) block default
    # python-httpx User-Agent strings. Pretend to be a normal browser.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.gozochannel.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
            _live_schedule_cache[cache_key] = data
            logger.info("Fetched Gozo Channel schedule for %s", cache_key)
            return data
    except Exception as e:
        logger.warning("Live schedule fetch failed for %s: %s", cache_key, e)
        analytics.log_error("gozo_channel_fetch")
        return None


def _parse_times_with_rollover(
    times_list: list[dict], base_date: date_cls
) -> list[datetime]:
    """
    Convert [{name: 'HH:MM', ...}, ...] into tz-aware datetimes.
    When a time is less than the previous one, we've crossed midnight.
    """
    result: list[datetime] = []
    current_date = base_date
    prev_minutes = -1

    for entry in times_list:
        hh, mm = map(int, entry["name"].split(":"))
        total = hh * 60 + mm
        if total < prev_minutes:
            current_date = current_date + timedelta(days=1)
        result.append(
            datetime(
                current_date.year, current_date.month, current_date.day,
                hh, mm, tzinfo=MALTA_TZ,
            )
        )
        prev_minutes = total
    return result


def _load_fallback_schedule() -> dict:
    with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _fallback_departures(direction: str, for_date: date_cls) -> list[datetime]:
    schedule = _load_fallback_schedule()
    day_type = "weekend" if for_date.weekday() >= 5 else "weekday"
    times_strs = schedule[direction][day_type]
    return [
        datetime(
            for_date.year, for_date.month, for_date.day,
            int(t.split(":")[0]), int(t.split(":")[1]),
            tzinfo=MALTA_TZ,
        )
        for t in times_strs
    ]


async def get_gc_departures(
    direction: str, for_date: date_cls
) -> tuple[list[datetime], dict]:
    """Gozo Channel departures for a date. Metadata: source, is_holiday."""
    live = await fetch_live_schedule(for_date)
    if live is not None:
        key = "mgarr" if direction == "mgarr_to_cirkewwa" else "cirkewwa"
        departures = _parse_times_with_rollover(live["times"][key], for_date)
        return departures, {
            "source": "live",
            "is_holiday": live.get("is_holiday", False),
        }
    return _fallback_departures(direction, for_date), {
        "source": "fallback",
        "is_holiday": False,
    }


async def next_gc_departures(
    direction: str, now: datetime, limit: int = 3
) -> tuple[list[datetime], dict]:
    """
    Returns next `limit` Gozo Channel departures after `now`.
    Always queries both today's AND tomorrow's schedule JSON to handle:
      - late-night requests where rollover entries in today's file are insufficient
      - early-morning requests (e.g. 02:00) that need tomorrow's morning runs
      - cases where Gozo Channel doesn't include rollover entries in a given file
    Deduplicates by exact datetime in case rollover and tomorrow's file overlap.
    """
    today_deps, today_meta = await get_gc_departures(direction, now.date())
    tomorrow_deps, _ = await get_gc_departures(
        direction, now.date() + timedelta(days=1)
    )

    # Merge and dedupe (rollover entries in today's file may duplicate
    # the early entries in tomorrow's file)
    seen: set = set()
    merged: list[datetime] = []
    for d in today_deps + tomorrow_deps:
        if d not in seen:
            seen.add(d)
            merged.append(d)
    merged.sort()

    future = [d for d in merged if d > now][:limit]
    return future, today_meta


# --- Gozo Fast Ferry (REST API) ---
FF_VALLETTA = "Valletta"
FF_MGARR = "Imgarr (Gozo)"


async def fetch_fast_ferry(
    departing: str, arriving: str, for_date: date_cls
) -> list[dict] | None:
    """
    Fetches Fast Ferry trips for a date. Returns list of
    {departing: datetime, vessel: str, seats: int}.
    """
    cache_key = (departing, arriving, for_date.isoformat())
    now_ts = time.time()
    cached = _fast_ferry_cache.get(cache_key)
    if cached and now_ts - cached[1] < FAST_FERRY_TTL:
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://gozohighspeed.com/api/Trip",
                params={
                    "departingHarbor": departing,
                    "arrivingHarbor": arriving,
                    "date": for_date.isoformat(),
                },
            )
            r.raise_for_status()
            raw = r.json()

            trips = []
            for item in raw:
                voyages = item.get("voyages") or []
                v = voyages[0] if voyages else {}
                # API returns ISO timestamps. They appear to be in Malta local
                # time (no tz suffix). If the API ever changes to UTC, parse
                # below will detect the suffix.
                ts_str = item["departingTime"]
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                parsed = datetime.fromisoformat(ts_str)
                if parsed.tzinfo is None:
                    # Naive timestamp — interpret as Malta local time
                    parsed = parsed.replace(tzinfo=MALTA_TZ)
                else:
                    parsed = parsed.astimezone(MALTA_TZ)
                trips.append({
                    "departing": parsed,
                    "vessel": v.get("vesselName") or "",
                    "seats": v.get("seatsEconomy", -1),
                })
            _fast_ferry_cache[cache_key] = (trips, now_ts)
            sample = ", ".join(t["departing"].strftime("%H:%M") for t in trips[:5])
            logger.info(
                "Fetched Fast Ferry %s → %s for %s (%d trips: %s%s)",
                departing, arriving, for_date, len(trips),
                sample, "..." if len(trips) > 5 else "",
            )
            return trips
    except Exception as e:
        logger.warning(
            "Fast Ferry fetch failed for %s → %s on %s: %s",
            departing, arriving, for_date, e,
        )
        analytics.log_error("fast_ferry_fetch")
        return None


async def next_fast_ferry(
    departing: str, arriving: str, now: datetime, limit: int = 3
) -> list[dict]:
    today_trips = await fetch_fast_ferry(departing, arriving, now.date()) or []
    future = [t for t in today_trips if t["departing"] > now]

    if len(future) < limit:
        tomorrow_trips = await fetch_fast_ferry(
            departing, arriving, now.date() + timedelta(days=1)
        ) or []
        future.extend(tomorrow_trips[: limit - len(future)])

    return future[:limit]


# Days to sample when establishing the "normal" trip count baseline.
FF_BASELINE_DAYS = (2, 3, 4, 7)
# If today's count is below this fraction of the baseline, flag as restricted.
FF_RESTRICTED_THRESHOLD = 0.6


async def is_fast_ferry_restricted(
    departing: str, arriving: str, for_date: date_cls
) -> bool:
    """
    Detects whether `for_date` has an unusually small Fast Ferry schedule
    compared to nearby reference days. Used to warn users when the operator
    has cancelled trips (e.g. fireworks festival, weather, maintenance).

    Returns True only if we have a clear baseline AND today's count is well
    below it. False otherwise (don't warn on uncertainty).
    """
    today_trips = await fetch_fast_ferry(departing, arriving, for_date)
    if today_trips is None:
        return False
    today_count = len(today_trips)
    if today_count == 0:
        return True  # no service at all today is definitely abnormal

    # Sample baseline from nearby future dates (same weekday-ish, fewer
    # surprises than past dates whose data may already be archived).
    baseline_counts: list[int] = []
    for offset in FF_BASELINE_DAYS:
        ref = await fetch_fast_ferry(
            departing, arriving, for_date + timedelta(days=offset)
        )
        if ref is not None and len(ref) > 0:
            baseline_counts.append(len(ref))

    if len(baseline_counts) < 2:
        return False  # not enough data to judge

    baseline = sum(baseline_counts) / len(baseline_counts)
    return today_count < baseline * FF_RESTRICTED_THRESHOLD


def seat_warning(seats: int) -> str:
    if seats < 0:
        return ""  # unknown
    if seats == 0:
        return "  ❌ Fully booked"
    if seats < FEW_SEATS_THRESHOLD:
        return f"  ⚠️ {seats} seats left"
    return ""


def format_fast_ferry_line(trip: dict, now: datetime, prefix: str) -> str:
    dt = trip["departing"]
    line = f"{prefix} {dt.strftime('%H:%M')} ({format_delta(dt - now)})"
    if trip["vessel"]:
        line += f" — {trip['vessel']}"
    line += seat_warning(trip["seats"])
    return line


# --- Helpers ---
def format_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds() // 60)
    if total < 1:
        return "now"
    if total < 60:
        return f"in {total} min"
    h, m = divmod(total, 60)
    return f"in {h}h {m}m" if m else f"in {h}h"


def detect_island(lat: float, lon: float) -> str | None:
    if not (35.78 <= lat <= 36.10 and 14.15 <= lon <= 14.58):
        return None
    return "gozo" if lat >= 36.00 else "malta"


def holiday_banner(meta: dict) -> str:
    return "🎉 _Public holiday schedule_\n\n" if meta.get("is_holiday") else ""


# --- Ferry terminal coordinates & Google Maps links ---
TERMINALS = {
    # (lat, lon, display_name)
    "cirkewwa": (35.987681, 14.329097, "Ċirkewwa Ferry Terminal"),
    "mgarr":    (36.024068, 14.298150, "Mġarr Ferry Terminal"),
    "valletta": (35.894754, 14.513739, "Valletta Ferry Terminal"),
}


def maps_url(
    from_lat: float, from_lon: float,
    to_lat: float, to_lon: float,
    mode: str,
) -> str:
    """Build a Google Maps directions URL. mode: 'car' → driving, 'foot' → transit."""
    travelmode = "driving" if mode == "car" else "transit"
    return (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={from_lat},{from_lon}"
        f"&destination={to_lat},{to_lon}"
        f"&travelmode={travelmode}"
    )


def build_directions_markup(
    user_lat: float, user_lon: float, island: str, mode: str,
) -> InlineKeyboardMarkup | None:
    """
    Build inline keyboard with Google Maps directions to the relevant terminal(s).
    - Gozo + any mode → one button to Mġarr
    - Malta + car → one button to Ċirkewwa
    - Malta + foot → two buttons (Ċirkewwa and Valletta, since both are usable)
    """
    buttons: list[list[InlineKeyboardButton]] = []

    def btn(terminal_key: str, label_override: str | None = None):
        t_lat, t_lon, t_name = TERMINALS[terminal_key]
        label = label_override or f"🗺 Directions to {t_name}"
        return [InlineKeyboardButton(
            label, url=maps_url(user_lat, user_lon, t_lat, t_lon, mode),
        )]

    if island == "gozo":
        buttons.append(btn("mgarr"))
    elif mode == "car":
        buttons.append(btn("cirkewwa"))
    else:  # malta, foot
        buttons.append(btn("cirkewwa", "🗺 To Ċirkewwa (Gozo Channel)"))
        buttons.append(btn("valletta", "🗺 To Valletta (Fast Ferry)"))

    return InlineKeyboardMarkup(buttons) if buttons else None


# --- Weather ---
async def fetch_sea_conditions() -> dict | None:
    now_ts = time.time()
    if _weather_cache["data"] and now_ts - _weather_cache["ts"] < WEATHER_TTL:
        return _weather_cache["data"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            marine = await client.get(
                "https://marine-api.open-meteo.com/v1/marine",
                params={
                    "latitude": CHANNEL_LAT,
                    "longitude": CHANNEL_LON,
                    "current": "wave_height",
                },
            )
            forecast = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": CHANNEL_LAT,
                    "longitude": CHANNEL_LON,
                    "current": "wind_speed_10m,wind_direction_10m",
                    "wind_speed_unit": "kn",
                },
            )
            marine.raise_for_status()
            forecast.raise_for_status()
            data = {
                "wave_height_m": marine.json()["current"].get("wave_height"),
                "wind_kts": forecast.json()["current"]["wind_speed_10m"],
                "wind_dir": forecast.json()["current"]["wind_direction_10m"],
            }
            _weather_cache["data"] = data
            _weather_cache["ts"] = now_ts
            return data
    except Exception as e:
        logger.warning("Weather fetch failed: %s", e)
        analytics.log_error("weather_fetch")
        return None


def degrees_to_compass(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg / 22.5) + 0.5) % 16]


def comfort_assessment(wind_kts: float, wave_m: float | None) -> str:
    if wave_m is not None:
        if wave_m < 0.4:
            mood = "🟢 Smooth crossing — calm sea"
        elif wave_m < 0.8:
            mood = "🟢 Comfortable — light chop"
        elif wave_m < 1.3:
            mood = "🟡 Some motion — you'll feel it a bit"
        elif wave_m < 2.0:
            mood = "🟠 Rough — sensitive passengers may feel queasy"
        else:
            mood = "🔴 Very rough — possible delays or cancellations"
    else:
        if wind_kts < 10:
            mood = "🟢 Calm conditions"
        elif wind_kts < 17:
            mood = "🟡 Moderate breeze"
        elif wind_kts < 25:
            mood = "🟠 Fresh wind — expect motion"
        else:
            mood = "🔴 Strong wind — rough crossing"
    if wind_kts >= 28:
        mood += " ⚠️ high wind"
    return mood


def format_conditions(data: dict | None) -> str:
    if not data:
        return ""
    wind_kts = data["wind_kts"]
    wind_dir = degrees_to_compass(data["wind_dir"])
    wave_m = data.get("wave_height_m")
    line = f"🌬 {wind_kts:.1f} kts {wind_dir}"
    if wave_m is not None:
        line += f"  •  🌊 {wave_m:.1f} m waves"
    return f"\n{line}\n{comfort_assessment(wind_kts, wave_m)}"


# --- Conversation flow: /next → location → car/foot → result ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    analytics.log_command("start")
    text = (
        "🛳 *Gozo ↔ Malta Ferries*\n\n"
        "I track two operators:\n"
        "  • *Gozo Channel* — car ferry, Ċirkewwa ↔ Mġarr (~25 min)\n"
        "  • *Fast Ferry* — passenger only, Valletta ↔ Mġarr (~45 min)\n\n"
        "Commands:\n"
        "/next — next ferry from your location\n"
        "/plan — AI route planner (e.g. _from Sliema to Victoria by 14:00_)\n"
        "/mgarr — next 3 Gozo Channel from Mġarr\n"
        "/cirkewwa — next 3 Gozo Channel from Ċirkewwa\n"
        "/fastferry — next Fast Ferry in both directions\n"
        "/today — full schedule for today\n"
        "/sea — current sea conditions"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def next_both(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    analytics.log_command("next")
    context.user_data.pop("pending_island", None)
    context.user_data.pop("pending_location", None)
    keyboard = [
        [KeyboardButton("📍 Share location", request_location=True)],
        [KeyboardButton("🏝 I'm on Gozo"), KeyboardButton("🇲🇹 I'm on Malta")],
    ]
    markup = ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "Where are you sailing from?\n\n"
        "_Tip: location sharing works only in the mobile app — "
        "on desktop, pick your island manually._",
        parse_mode="Markdown",
        reply_markup=markup,
    )


async def _ask_mode(update: Update) -> None:
    keyboard = [
        [KeyboardButton("🚗 With a car"), KeyboardButton("🚶 On foot")],
    ]
    markup = ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "Got it. Travelling with a car or on foot?",
        reply_markup=markup,
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loc = update.message.location
    island = detect_island(loc.latitude, loc.longitude)
    if island is None:
        await update.message.reply_text(
            "You don't seem to be on Malta or Gozo 🤔\nPick manually: /next",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    context.user_data["pending_island"] = island
    context.user_data["pending_location"] = (loc.latitude, loc.longitude)
    analytics.log_island_pick(island, from_location=True)
    await _ask_mode(update)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    text_lower = text.lower()
    pending = context.user_data.get("pending_island")
    chat_id = update.effective_chat.id

    # Conversational follow-up for /plan (initial entry)
    if context.user_data.pop("awaiting_plan", False):
        await _run_plan(update, text)
        return

    # Clarification follow-up: bot previously asked a question for /plan
    # and the user is responding. Detect by presence of stored history.
    if chat_id in _plan_history:
        await _run_plan(update, text)
        return

    # Step 2: mode selection (island already known)
    if pending and ("car" in text_lower or "foot" in text_lower):
        mode = "car" if "car" in text_lower else "foot"
        analytics.log_mode_choice(mode)
        location = context.user_data.get("pending_location")  # may be None
        context.user_data.pop("pending_island", None)
        context.user_data.pop("pending_location", None)
        await _send_results(update, pending, mode, location)
        return

    # Step 1: island selection (manual, no location)
    if "gozo" in text_lower:
        context.user_data["pending_island"] = "gozo"
        context.user_data.pop("pending_location", None)
        analytics.log_island_pick("gozo", from_location=False)
        await _ask_mode(update)
    elif "malta" in text_lower:
        context.user_data["pending_island"] = "malta"
        context.user_data.pop("pending_location", None)
        analytics.log_island_pick("malta", from_location=False)
        await _ask_mode(update)


async def _send_results(
    update: Update,
    island: str,
    mode: str,
    location: tuple[float, float] | None,
) -> None:
    now = datetime.now(MALTA_TZ)

    if island == "gozo":
        gc_deps, gc_meta = await next_gc_departures("mgarr_to_cirkewwa", now, limit=3)
        ff_deps = (
            await next_fast_ferry(FF_MGARR, FF_VALLETTA, now, limit=3)
            if mode == "foot" else []
        )
        island_label = "Gozo"
        gc_label = "Gozo Channel — Mġarr → Ċirkewwa"
        ff_label = "Fast Ferry — Mġarr → Valletta"
    else:  # malta
        gc_deps, gc_meta = await next_gc_departures("cirkewwa_to_mgarr", now, limit=3)
        ff_deps = (
            await next_fast_ferry(FF_VALLETTA, FF_MGARR, now, limit=3)
            if mode == "foot" else []
        )
        island_label = "Malta"
        gc_label = "Gozo Channel — Ċirkewwa → Mġarr"
        ff_label = "Fast Ferry — Valletta → Mġarr"

    mode_label = "with a car" if mode == "car" else "on foot"
    lines = [
        holiday_banner(gc_meta)
        + f"📍 You're on {island_label} ({mode_label})\n"
    ]

    # Gozo Channel
    lines.append(f"🛳 *{gc_label}* (~25 min)")
    if gc_deps:
        for i, d in enumerate(gc_deps):
            prefix = "➡️" if i == 0 else "  •"
            lines.append(f"{prefix} {d.strftime('%H:%M')} ({format_delta(d - now)})")
    else:
        lines.append("  No more today")

    # Fast Ferry (only for "on foot")
    if mode == "foot":
        lines.append(f"\n⚡️ *{ff_label}* (~45 min)")
        if ff_deps:
            for i, t in enumerate(ff_deps):
                prefix = "➡️" if i == 0 else "  •"
                lines.append(format_fast_ferry_line(t, now, prefix))
        else:
            lines.append("  No more today")

        # Warn if today's Fast Ferry schedule is unusually short
        ff_dep, ff_arr = (
            (FF_MGARR, FF_VALLETTA) if island == "gozo"
            else (FF_VALLETTA, FF_MGARR)
        )
        if await is_fast_ferry_restricted(ff_dep, ff_arr, now.date()):
            lines.append(
                "  _⚠️ Fast Ferry running a restricted schedule today — "
                "check gozohighspeed.com_"
            )

    conditions = await fetch_sea_conditions()
    lines.append(format_conditions(conditions))

    # First message: schedule + sea conditions, also dismisses the reply keyboard
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    # Second message: Google Maps directions buttons — only if real location shared
    if location is not None:
        lat, lon = location
        directions_markup = build_directions_markup(lat, lon, island, mode)
        if directions_markup:
            await update.message.reply_text(
                "🗺 _Get directions to the terminal:_",
                parse_mode="Markdown",
                reply_markup=directions_markup,
            )


# --- Gozo Channel direction-specific commands ---
async def _next_gc_direction(update: Update, direction: str, label: str) -> None:
    now = datetime.now(MALTA_TZ)
    deps, meta = await next_gc_departures(direction, now, limit=3)

    if not deps:
        await update.message.reply_text(f"No upcoming Gozo Channel ferries {label}.")
        return

    lines = [holiday_banner(meta) + f"🛳 *Gozo Channel — {label}*\n"]
    for i, d in enumerate(deps):
        prefix = "➡️" if i == 0 else "  •"
        lines.append(f"{prefix} {d.strftime('%H:%M')} ({format_delta(d - now)})")

    conditions = await fetch_sea_conditions()
    lines.append(format_conditions(conditions))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def next_mgarr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    analytics.log_command("mgarr")
    await _next_gc_direction(update, "mgarr_to_cirkewwa", "Mġarr → Ċirkewwa")


async def next_cirkewwa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    analytics.log_command("cirkewwa")
    await _next_gc_direction(update, "cirkewwa_to_mgarr", "Ċirkewwa → Mġarr")


# --- Fast Ferry command ---
async def fastferry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    analytics.log_command("fastferry")
    now = datetime.now(MALTA_TZ)
    to_gozo = await next_fast_ferry(FF_VALLETTA, FF_MGARR, now, limit=3)
    to_valletta = await next_fast_ferry(FF_MGARR, FF_VALLETTA, now, limit=3)

    lines = ["⚡️ *Gozo Fast Ferry* — passenger only (~45 min)\n"]

    lines.append("*Valletta → Mġarr*")
    if to_gozo:
        for i, t in enumerate(to_gozo):
            prefix = "➡️" if i == 0 else "  •"
            lines.append(format_fast_ferry_line(t, now, prefix))
    else:
        lines.append("  No upcoming trips")

    lines.append("\n*Mġarr → Valletta*")
    if to_valletta:
        for i, t in enumerate(to_valletta):
            prefix = "➡️" if i == 0 else "  •"
            lines.append(format_fast_ferry_line(t, now, prefix))
    else:
        lines.append("  No upcoming trips")

    lines.append("\n_Bookings may be required — check gozohighspeed.com_")

    # Restricted-schedule warnings (check both directions independently)
    today = now.date()
    if (
        await is_fast_ferry_restricted(FF_VALLETTA, FF_MGARR, today)
        or await is_fast_ferry_restricted(FF_MGARR, FF_VALLETTA, today)
    ):
        lines.insert(
            1,
            "_⚠️ Restricted schedule today — fewer trips than usual._\n",
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Today and Sea ---
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    analytics.log_command("today")
    now = datetime.now(MALTA_TZ)
    today_date = now.date()

    m_deps, meta = await get_gc_departures("mgarr_to_cirkewwa", today_date)
    c_deps, _ = await get_gc_departures("cirkewwa_to_mgarr", today_date)
    m_today = [d.strftime("%H:%M") for d in m_deps if d.date() == today_date]
    c_today = [d.strftime("%H:%M") for d in c_deps if d.date() == today_date]

    ff_to_gozo = await fetch_fast_ferry(FF_VALLETTA, FF_MGARR, today_date) or []
    ff_to_valletta = await fetch_fast_ferry(FF_MGARR, FF_VALLETTA, today_date) or []
    ff_to_gozo_times = [t["departing"].strftime("%H:%M") for t in ff_to_gozo]
    ff_to_valletta_times = [t["departing"].strftime("%H:%M") for t in ff_to_valletta]

    weekday_name = now.strftime("%A")
    parts = [
        holiday_banner(meta) + f"📅 *Today's schedule* ({weekday_name})\n",
        f"🛳 *Gozo Channel — Mġarr → Ċirkewwa* ({len(m_today)} trips)",
        ", ".join(m_today) or "—",
        "",
        f"🛳 *Gozo Channel — Ċirkewwa → Mġarr* ({len(c_today)} trips)",
        ", ".join(c_today) or "—",
        "",
        f"⚡️ *Fast Ferry — Valletta → Mġarr* ({len(ff_to_gozo_times)} trips)",
        ", ".join(ff_to_gozo_times) or "No service today",
        "",
        f"⚡️ *Fast Ferry — Mġarr → Valletta* ({len(ff_to_valletta_times)} trips)",
        ", ".join(ff_to_valletta_times) or "No service today",
    ]
    text = "\n".join(parts)
    if meta.get("source") == "fallback":
        text += "\n\n_⚠️ Gozo Channel: using fallback schedule._"

    await update.message.reply_text(text, parse_mode="Markdown")


async def sea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    analytics.log_command("sea")
    data = await fetch_sea_conditions()
    if not data:
        await update.message.reply_text(
            "Couldn't fetch sea conditions right now. Try again in a moment."
        )
        return

    wind_kts = data["wind_kts"]
    wind_dir = degrees_to_compass(data["wind_dir"])
    wave_m = data.get("wave_height_m")

    lines = [
        "🌊 *Current conditions in the channel*\n",
        f"🌬 Wind: {wind_kts:.1f} kts from {wind_dir}",
    ]
    if wave_m is not None:
        lines.append(f"🌊 Wave height: {wave_m:.1f} m")
    lines.append("")
    lines.append(comfort_assessment(wind_kts, wave_m))
    lines.append("\n_Source: Open-Meteo Marine_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- /plan: LLM-powered route planner ---
async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    analytics.log_command("plan")
    # Clear unrelated conversation state
    context.user_data.pop("pending_island", None)
    context.user_data.pop("pending_location", None)

    # Did the user include the request inline? "/plan from X to Y by 14:00"
    args_text = " ".join(context.args).strip() if context.args else ""

    if not args_text:
        # Ask conversationally — set a flag so next text message is treated as the plan input
        context.user_data["awaiting_plan"] = True
        await update.message.reply_text(
            "🗺 *Trip planner*\n\n"
            "Tell me where you are, where you're going, and (optionally) when "
            "you need to be there.\n\n"
            "Example: _from Sliema to Victoria by 14:00_",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await _run_plan(update, args_text)


async def _run_plan(update: Update, request_text: str) -> None:
    """Heavy lifting for the planner — geocode, fetch, LLM, format."""
    progress_msg = await update.message.reply_text("🤔 Planning…")

    try:
        chat_id = update.effective_chat.id
        history_for_llm = _plan_history.get(chat_id, [])

        # Step 1: parse with LLM (passing history for clarifications)
        parsed = await planner.parse_request(request_text, history=history_for_llm)

        # Save this turn so a follow-up like "yes" can be linked
        new_history = history_for_llm + [
            {"role": "user", "content": request_text},
        ]

        if parsed.error:
            # Save the bot's clarifying question into history.
            # User's next message will land in handle_text, which checks
            # _plan_history and routes back here if a clarification is pending.
            new_history.append({"role": "assistant", "content": parsed.error})
            _plan_history[chat_id] = new_history[-6:]  # cap to last 3 exchanges
            await progress_msg.edit_text(parsed.error)
            return

        # We got a complete request — clear history
        _plan_history.pop(chat_id, None)

        # Step 2: geocode both ends
        origin_geo = await planner.geocode(parsed.origin)
        dest_geo = await planner.geocode(parsed.destination)
        if origin_geo is None or dest_geo is None:
            missing = parsed.origin if origin_geo is None else parsed.destination
            await progress_msg.edit_text(
                f"I couldn't find *{missing}* on the map. "
                "Try a more specific name (e.g. 'Sliema, Malta').",
                parse_mode="Markdown",
            )
            return

        # Step 3: same-island check
        if origin_geo.on_gozo == dest_geo.on_gozo and origin_geo.on_gozo is not None:
            await progress_msg.edit_text(
                f"Both *{origin_geo.name}* and *{dest_geo.name}* are on the same "
                "island — no ferry needed. I only handle Malta ↔ Gozo trips.",
                parse_mode="Markdown",
            )
            return

        if origin_geo.on_gozo is None or dest_geo.on_gozo is None:
            await progress_msg.edit_text(
                "One of those places doesn't seem to be in Malta. "
                "I only handle trips between Malta and Gozo.",
            )
            return

        # Step 4: fetch ferry departures for relevant options.
        # If the deadline is in the future (tomorrow+), fetch from the START
        # of that day so morning options are visible — otherwise we'd only
        # see departures after right-now.
        now = datetime.now(MALTA_TZ)
        ref_time = now
        if parsed.deadline_date:
            try:
                deadline_d = datetime.strptime(
                    parsed.deadline_date, "%Y-%m-%d"
                ).date()
                if deadline_d > now.date():
                    ref_time = datetime.combine(
                        deadline_d, datetime.min.time(), tzinfo=MALTA_TZ
                    )
            except ValueError:
                pass  # bad date format from LLM, fall back to now

        options = planner.applicable_options(origin_geo.on_gozo, dest_geo.on_gozo)

        def _fmt_dep(dt: datetime) -> str:
            # Mark dates clearly when they differ from today so the LLM
            # doesn't confuse "06:45" tomorrow with "06:45" today.
            if dt.date() != now.date():
                return dt.strftime("%a %H:%M")  # e.g. "Sun 06:45"
            return dt.strftime("%H:%M")

        ferry_data: dict = {}
        for opt in options:
            if opt.operator == "Gozo Channel":
                deps, _meta = await next_gc_departures(
                    opt.direction_key, ref_time, limit=8
                )
                ferry_data[f"{opt.operator} ({opt.from_terminal} → {opt.to_terminal})"] = [
                    {
                        "departing": _fmt_dep(d),
                        "crossing_minutes": opt.crossing_minutes,
                    }
                    for d in deps
                ]
            else:  # Fast Ferry
                if origin_geo.on_gozo:
                    ff_deps = await next_fast_ferry(FF_MGARR, FF_VALLETTA, ref_time, limit=8)
                else:
                    ff_deps = await next_fast_ferry(FF_VALLETTA, FF_MGARR, ref_time, limit=8)
                ferry_data[f"{opt.operator} ({opt.from_terminal} → {opt.to_terminal})"] = [
                    {
                        "departing": _fmt_dep(t["departing"]),
                        "vessel": t["vessel"] or None,
                        "seats_economy": t["seats"] if t["seats"] >= 0 else None,
                        "crossing_minutes": opt.crossing_minutes,
                    }
                    for t in ff_deps
                ]

        # Step 5: sea conditions for the LLM to optionally mention
        sea_data = await fetch_sea_conditions()

        # Step 6: LLM writes the plan
        ctx = planner.build_planning_context(
            parsed, origin_geo, dest_geo, ferry_data, sea_data, now,
        )
        plan_text = await planner.write_plan(ctx)

        if not plan_text:
            await progress_msg.edit_text(
                "AI couldn't generate a plan right now. "
                "Try /next or /fastferry directly."
            )
            return

        await progress_msg.edit_text(plan_text, parse_mode="Markdown")

    except Exception as e:
        logger.exception("Plan handler failed: %s", e)
        analytics.log_error("plan_handler")
        try:
            await progress_msg.edit_text(
                "Something went wrong while planning. Try /next or /fastferry."
            )
        except Exception:
            pass


# Per-chat in-memory history for /plan multi-turn clarifications.
# Resets on bot restart (acceptable for a clarification flow).
_plan_history: dict[int, list[dict]] = {}


# --- Admin: stats ---
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    if ADMIN_USER_ID == 0:
        await update.message.reply_text(
            "Stats are only available to the bot admin. "
            "The ADMIN_USER_ID env var is not set on the server."
        )
        return
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text("Only the bot admin can see stats.")
        return
    # Don't count /stats itself in the command stats (it's admin-only)
    await update.message.reply_text(analytics.get_summary(), parse_mode="Markdown")


# --- Launch ---
def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("next", next_both))
    app.add_handler(CommandHandler("mgarr", next_mgarr))
    app.add_handler(CommandHandler("cirkewwa", next_cirkewwa))
    app.add_handler(CommandHandler("fastferry", fastferry))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("sea", sea))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_path = TOKEN
    full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{webhook_path}"

    logger.info("Starting webhook on port %s", PORT)
    logger.info("Webhook URL: %s", full_webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
    )


if __name__ == "__main__":
    main()
