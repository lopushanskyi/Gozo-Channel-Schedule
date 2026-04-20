"""
Gozo Ferry Bot — Telegram bot for the Mġarr ↔ Ċirkewwa ferry schedule.
Webhook mode (for Render / any Web Service).
Includes sea-comfort assessment via Open-Meteo Marine API.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
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

# Midpoint of the Malta–Gozo channel (between Ċirkewwa and Mġarr)
CHANNEL_LAT = 36.015
CHANNEL_LON = 14.296

# Cache weather for 10 minutes (API is free but let's be polite)
_weather_cache: dict = {"data": None, "ts": 0.0}
WEATHER_TTL = 600  # seconds

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Schedule logic ---
def load_schedule() -> dict:
    with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_day_type(date) -> str:
    """Returns 'weekend' for Sat/Sun, 'weekday' otherwise."""
    return "weekend" if date.weekday() >= 5 else "weekday"


def parse_time(date, time_str: str) -> datetime:
    """Combines date with 'HH:MM' into a Malta-tz datetime."""
    hour, minute = map(int, time_str.split(":"))
    return datetime(date.year, date.month, date.day, hour, minute, tzinfo=MALTA_TZ)


def next_departures(direction: str, now: datetime, limit: int = 3) -> list[datetime]:
    """Next `limit` departures in a given direction after `now`."""
    schedule = load_schedule()
    result: list[datetime] = []

    for day_offset in range(2):
        day = (now + timedelta(days=day_offset)).date()
        times = schedule[direction][get_day_type(day)]
        for t in times:
            departure = parse_time(day, t)
            if departure > now:
                result.append(departure)
                if len(result) >= limit:
                    return result
    return result


def format_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds() // 60)
    if total < 1:
        return "now"
    if total < 60:
        return f"in {total} min"
    h, m = divmod(total, 60)
    return f"in {h}h {m}m" if m else f"in {h}h"


def detect_island(lat: float, lon: float) -> str | None:
    """Returns 'gozo', 'malta' or None. ~36.00°N is the boundary."""
    if not (35.78 <= lat <= 36.10 and 14.15 <= lon <= 14.58):
        return None
    return "gozo" if lat >= 36.00 else "malta"


ISLAND_TO_DIRECTION = {
    "gozo": ("mgarr_to_cirkewwa", "Mġarr → Ċirkewwa", "Gozo"),
    "malta": ("cirkewwa_to_mgarr", "Ċirkewwa → Mġarr", "Malta"),
}


# --- Weather / sea-comfort ---
async def fetch_sea_conditions() -> dict | None:
    """
    Fetches current wind + wave data from Open-Meteo.
    Returns dict with wind_kts, wind_dir, wave_height_m — or None on failure.
    Cached for WEATHER_TTL seconds.
    """
    now_ts = time.time()
    if _weather_cache["data"] and now_ts - _weather_cache["ts"] < WEATHER_TTL:
        return _weather_cache["data"]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Marine API — wave height
            marine = await client.get(
                "https://marine-api.open-meteo.com/v1/marine",
                params={
                    "latitude": CHANNEL_LAT,
                    "longitude": CHANNEL_LON,
                    "current": "wave_height",
                },
            )
            # Forecast API — wind speed (in knots!) and direction
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
    """
    Returns a short human-friendly comfort line.
    Uses wave height primarily (better predictor), wind as fallback/secondary.
    """
    # Wave-based thresholds (empirical for short channel crossings)
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
        # Fallback — just wind
        if wind_kts < 10:
            mood = "🟢 Calm conditions"
        elif wind_kts < 17:
            mood = "🟡 Moderate breeze"
        elif wind_kts < 25:
            mood = "🟠 Fresh wind — expect motion"
        else:
            mood = "🔴 Strong wind — rough crossing"

    # Extra flag for very strong wind regardless of waves (gusts affect boarding)
    if wind_kts >= 28:
        mood += " ⚠️ high wind"

    return mood


def format_conditions(data: dict | None) -> str:
    if not data:
        return ""  # silently skip if weather fetch failed

    wind_kts = data["wind_kts"]
    wind_dir = degrees_to_compass(data["wind_dir"])
    wave_m = data.get("wave_height_m")

    line_weather = f"🌬 {wind_kts:.1f} kts {wind_dir}"
    if wave_m is not None:
        line_weather += f"  •  🌊 {wave_m:.1f} m waves"

    return f"\n{line_weather}\n{comfort_assessment(wind_kts, wave_m)}"


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
    deps = next_departures(direction, now, limit=3)

    if not deps:
        await update.message.reply_text(
            f"You're on {island_name}. No upcoming ferries found.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    lines = [f"📍 You're on {island_name} → *{label}*\n"]
    for i, d in enumerate(deps):
        prefix = "➡️" if i == 0 else "  •"
        lines.append(f"{prefix} {d.strftime('%H:%M')} ({format_delta(d - now)})")

    # Append live sea conditions
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
    deps = next_departures(direction, now, limit=3)

    if not deps:
        await update.message.reply_text(f"No upcoming ferries {label}.")
        return

    lines = [f"🛳 *{label}*\n"]
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
    schedule = load_schedule()
    day_type = get_day_type(now.date())
    day_label = "weekend" if day_type == "weekend" else "weekday"

    m_times = schedule["mgarr_to_cirkewwa"][day_type]
    c_times = schedule["cirkewwa_to_mgarr"][day_type]

    text = (
        f"📅 *Today's schedule* ({day_label})\n\n"
        f"🛳 *Mġarr → Ċirkewwa:*\n{', '.join(m_times)}\n\n"
        f"🛳 *Ċirkewwa → Mġarr:*\n{', '.join(c_times)}"
    )
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
