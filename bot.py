"""
Gozo Ferry Bot — Telegram-бот для розкладу порому Mgarr ↔ Cirkewwa.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

# --- Конфігурація ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Встановіть змінну середовища TELEGRAM_BOT_TOKEN з токеном від @BotFather"
    )

MALTA_TZ = ZoneInfo("Europe/Malta")
SCHEDULE_FILE = Path(__file__).parent / "schedule.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Логіка розкладу ---
def load_schedule() -> dict:
    with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_day_type(date) -> str:
    """Повертає 'weekend' для Сб/Нд, інакше 'weekday'."""
    return "weekend" if date.weekday() >= 5 else "weekday"


def parse_time(date, time_str: str) -> datetime:
    """Об'єднує дату з 'HH:MM' у datetime з таймзоною Мальти."""
    hour, minute = map(int, time_str.split(":"))
    return datetime(date.year, date.month, date.day, hour, minute, tzinfo=MALTA_TZ)


def next_departures(direction: str, now: datetime, limit: int = 3) -> list[datetime]:
    """
    Наступні `limit` відправлень у заданому напрямку після `now`.
    direction: 'mgarr_to_cirkewwa' або 'cirkewwa_to_mgarr'.
    """
    schedule = load_schedule()
    result: list[datetime] = []

    # Перевіряємо сьогодні та завтра (щоб охопити пізній вечір → раннє утро)
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
        return "зараз"
    if total < 60:
        return f"через {total} хв"
    h, m = divmod(total, 60)
    return f"через {h} год {m} хв" if m else f"через {h} год"


def detect_island(lat: float, lon: float) -> str | None:
    """
    Визначає острів за координатами.
    Повертає 'gozo', 'malta' або None (якщо точка не в Мальтійському архіпелазі).
    Кордон: ~36.00°N розділяє Мальту від Гоцо/Коміно.
    """
    # Груба обмежувальна рамка для Мальти + Гоцо + Коміно
    if not (35.78 <= lat <= 36.10 and 14.15 <= lon <= 14.58):
        return None
    return "gozo" if lat >= 36.00 else "malta"


# Звідки → який напрямок розкладу потрібен
ISLAND_TO_DIRECTION = {
    "gozo": ("mgarr_to_cirkewwa", "Mgarr → Cirkewwa", "Гоцо"),
    "malta": ("cirkewwa_to_mgarr", "Cirkewwa → Mgarr", "Мальті"),
}


# --- Хендлери команд ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🛳 *Розклад порому Gozo ↔ Malta*\n\n"
        "Команди:\n"
        "/next — наступний пором (визначу за локацією)\n"
        "/mgarr — наступні 3 з Mgarr → Cirkewwa\n"
        "/cirkewwa — наступні 3 з Cirkewwa → Mgarr\n"
        "/today — розклад на сьогодні"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def next_both(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує клавіатуру для вибору острова (автоматично чи вручну)."""
    keyboard = [
        [KeyboardButton("📍 Поділитися локацією", request_location=True)],
        [KeyboardButton("🏝 Я на Гоцо"), KeyboardButton("🇲🇹 Я на Мальті")],
    ]
    markup = ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "Звідки пливете? Поділіться локацією — визначу автоматично.",
        reply_markup=markup,
    )


async def _send_next_from_island(update: Update, island: str) -> None:
    """Відправляє найближчі пороми з обраного острова."""
    direction, label, island_name = ISLAND_TO_DIRECTION[island]
    now = datetime.now(MALTA_TZ)
    deps = next_departures(direction, now, limit=3)

    if not deps:
        await update.message.reply_text(
            f"Ви на {island_name}. Найближчих поромів не знайшов.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    lines = [f"📍 Ви на {island_name} → *{label}*\n"]
    for i, d in enumerate(deps):
        prefix = "➡️" if i == 0 else "  •"
        lines.append(f"{prefix} {d.strftime('%H:%M')} ({format_delta(d - now)})")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробляє надіслану геолокацію."""
    loc = update.message.location
    island = detect_island(loc.latitude, loc.longitude)

    if island is None:
        await update.message.reply_text(
            "Здається, ви не на Мальті чи Гоцо 🤔\n"
            "Оберіть острів вручну: /next",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await _send_next_from_island(update, island)


async def handle_island_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробляє натискання кнопок «Я на Гоцо / Мальті»."""
    text = update.message.text.lower()
    if "гоцо" in text:
        await _send_next_from_island(update, "gozo")
    elif "мальт" in text:
        await _send_next_from_island(update, "malta")


async def _next_direction(
    update: Update,
    direction: str,
    label: str,
) -> None:
    now = datetime.now(MALTA_TZ)
    deps = next_departures(direction, now, limit=3)

    if not deps:
        await update.message.reply_text(f"Немає найближчих поромів {label}.")
        return

    lines = [f"🛳 *{label}*\n"]
    for i, d in enumerate(deps):
        prefix = "➡️" if i == 0 else "  •"
        lines.append(f"{prefix} {d.strftime('%H:%M')} ({format_delta(d - now)})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def next_mgarr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _next_direction(update, "mgarr_to_cirkewwa", "Mgarr → Cirkewwa")


async def next_cirkewwa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _next_direction(update, "cirkewwa_to_mgarr", "Cirkewwa → Mgarr")


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(MALTA_TZ)
    schedule = load_schedule()
    day_type = get_day_type(now.date())
    day_label = "вихідний" if day_type == "weekend" else "будній"

    m_times = schedule["mgarr_to_cirkewwa"][day_type]
    c_times = schedule["cirkewwa_to_mgarr"][day_type]

    text = (
        f"📅 *Розклад на сьогодні* ({day_label})\n\n"
        f"🛳 *Mgarr → Cirkewwa:*\n{', '.join(m_times)}\n\n"
        f"🛳 *Cirkewwa → Mgarr:*\n{', '.join(c_times)}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# --- Запуск ---
def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("next", next_both))
    app.add_handler(CommandHandler("mgarr", next_mgarr))
    app.add_handler(CommandHandler("cirkewwa", next_cirkewwa))
    app.add_handler(CommandHandler("today", today))

    # Геолокація і кнопки вибору острова
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"(?i)(гоцо|мальт)"),
            handle_island_text,
        )
    )

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
