"""
Gozo Ferry Bot — Telegram bot for the Mġarr ↔ Ċirkewwa ferry schedule.

Sources:
- Primary: live daily schedule from static.gozochannel.com (per-date JSON)
- Fallback: bundled schedule.json (weekday/weekend approximation)
- Sea conditions: Open-Meteo Marine + Forecast API (free, no key)

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

MALTA_TZ = ZoneInfo("Europe/Malta")
SCHEDULE_FILE = Path(__file__).parent / "schedule.json"

# Midpoint of the Malta–Gozo channel
CHANNEL_LAT = 36.015
CHANNEL_LON = 14.296

# Caches
_live_schedule_cache: dict[str, dict] = {}  # key: "YYYY-MM-DD"
_weather_cache: dict = {"data": None, "ts": 0.0}
WEATHER_TTL = 600  # seconds

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Live schedule fetching ---
async def fetch_live_schedule(date_obj: date_cls) -> dict | None:
    """Fetch passenger.json from Gozo Channel CDN for a given date.
    Cached per-date in memory. Returns None on failure."""
    cache_key = date_obj.isoformat()
    if cache_key in _live_schedule_cache:
        return _live_schedule_cache[cache_key]

    url = (
        f"https://static.gozochannel.com/schedules/"
        f"{date_obj.year}/{date_obj.month:02d}/{date_obj.day:02d}/passenger.json"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            _live_schedule_cache[cache_key] = data
            logger.info("Fetched live schedule for %s", cache_key)
            return data
    except Exception as e:
        logger.warning("Live schedule fetch failed for %s: %s", cache_key, e)
        return None


def _parse_times_with_rollover(
    times_list: list[dict], base_date: date_cls
) -> list[datetime]:
    """
    Convert [{name: 'HH:MM', ...}, ...] into list of timezone-aware datetimes.
    Handles the rollover: when a time is less than the previous one,
    we've crossed midnight into the next calendar day.
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


# --- Fallback (bundled) schedule ---
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


# --- Unified schedule interface ---
async def get_departures(
    direction: str, for_date: date_cls
) -> tuple[list[datetime], dict]:
    """
    Returns (all departures for given date, metadata).
    metadata = {"source": "live"|"fallback", "is_holiday": bool}
    """
    live = await fetch_live_schedule(for_date)
    if live is not None:
        key = "mgarr" if direction == "mgarr_to_cirkewwa" else "cirkewwa"
        departures = _parse_times_with_rollover(live["times"][key], for_date)
        return departures, {
            "source": "live",
            "is_holiday": live.get("is_holiday", False),
        }

    # Fallback
    departures = _fallback_departures(direction, for_date)
    return departures, {"source": "fallback", "is_holiday": False}


async def next_departures(
    direction: str, now: datetime, limit: int = 3
) -> tuple[list[datetime], dict]:
    """Next `limit` departures after `now`, plus schedule metadata."""
    departures, meta = await get_departures(direction, now.date())
    future = [d for d in departures if d > now][:limit]
    return future, meta


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


ISLAND_TO_DIRECTION = {
    "gozo": ("mgarr_to_cirkewwa", "Mġarr → Ċirkewwa", "Gozo"),
    "malta": ("cirkewwa_to_mgarr", "Ċirkewwa → Mġarr", "Malta"),
}


# --- Weather / sea-comfort ---
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
        return None


def degrees_to_compass(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    ix = int((deg / 22.5) + 0.5) % 16
    return dirs[ix]


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


def holiday_banner(meta: dict) -> str:
    return "🎉 _Public holiday schedule_\n\n" if meta.get("is_holiday") else ""


# --- Command handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🛳 *Gozo ↔ Malta Ferry Schedule*\n\n"
        "Commands:\n"
        "/next — next ferry (I'll figure out the direction from your location)\n"
        "/mgarr — next 3 departures from Mġarr → Ċirkewwa\n"
        "/cirkewwa — next 3 departures from Ċirkewwa → Mġarr\n"
        "/today — full schedule for today\n"
        "/sea — current sea conditions"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def next_both(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [KeyboardButton("📍 Share location", request_location=True)],
        [KeyboardButton("🏝 I'm on Gozo"), KeyboardButton("🇲🇹 I'm on Malta")],
    ]
    markup = ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "Where are you sailing from? Share your location and I'll figure it out.",
        reply_markup=markup,
    )


async def _send_next_from_island(update: Update, island: str) -> None:
    direction, label, island_name = ISLAND_TO_DIRECTION[island]
    now = datetime.now(MALTA_TZ)
    deps, meta = await next_departures(direction, now, limit=3)

    if not deps:
        await update.message.reply_text(
            f"You're on {island_name}. No upcoming ferries found.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    lines = [
        holiday_banner(meta) + f"📍 You're on {island_name} → *{label}*\n"
    ]
    for i, d in enumerate(deps):
        prefix = "➡️" if i == 0 else "  •"
        lines.append(f"{prefix} {d.strftime('%H:%M')} ({format_delta(d - now)})")

    conditions = await fetch_sea_conditions()
    lines.append(format_conditions(conditions))

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
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
    await _send_next_from_island(update, island)


async def handle_island_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.lower()
    if "gozo" in text:
        await _send_next_from_island(update, "gozo")
    elif "malta" in text:
        await _send_next_from_island(update, "malta")


async def _next_direction(update: Update, direction: str, label: str) -> None:
    now = datetime.now(MALTA_TZ)
    deps, meta = await next_departures(direction, now, limit=3)

    if not deps:
        await update.message.reply_text(f"No upcoming ferries {label}.")
        return

    lines = [holiday_banner(meta) + f"🛳 *{label}*\n"]
    for i, d in enumerate(deps):
        prefix = "➡️" if i == 0 else "  •"
        lines.append(f"{prefix} {d.strftime('%H:%M')} ({format_delta(d - now)})")

    conditions = await fetch_sea_conditions()
    lines.append(format_conditions(conditions))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def next_mgarr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _next_direction(update, "mgarr_to_cirkewwa", "Mġarr → Ċirkewwa")


async def next_cirkewwa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _next_direction(update, "cirkewwa_to_mgarr", "Ċirkewwa → Mġarr")


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(MALTA_TZ)
    today_date = now.date()

    m_deps, meta = await get_departures("mgarr_to_cirkewwa", today_date)
    c_deps, _ = await get_departures("cirkewwa_to_mgarr", today_date)

    # For /today, show only times that fall on today's calendar date
    # (drop the next-day rollover entries at the tail of the list)
    m_today = [d.strftime("%H:%M") for d in m_deps if d.date() == today_date]
    c_today = [d.strftime("%H:%M") for d in c_deps if d.date() == today_date]

    weekday_name = now.strftime("%A")
    text = (
        f"{holiday_banner(meta)}"
        f"📅 *Today's schedule* ({weekday_name})\n\n"
        f"🛳 *Mġarr → Ċirkewwa* ({len(m_today)} trips):\n"
        f"{', '.join(m_today)}\n\n"
        f"🛳 *Ċirkewwa → Mġarr* ({len(c_today)} trips):\n"
        f"{', '.join(c_today)}"
    )
    if meta.get("source") == "fallback":
        text += "\n\n_⚠️ Using fallback schedule — live source unavailable._"

    await update.message.reply_text(text, parse_mode="Markdown")


async def sea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


# --- Launch (webhook) ---
def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("next", next_both))
    app.add_handler(CommandHandler("mgarr", next_mgarr))
    app.add_handler(CommandHandler("cirkewwa", next_cirkewwa))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("sea", sea))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"(?i)(gozo|malta)"),
            handle_island_text,
        )
    )

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
