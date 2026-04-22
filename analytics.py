"""
In-memory analytics for Gozo Ferry Bot.

Tracks aggregate counters for the process lifetime (resets on restart).
Also emits structured JSON log lines so Render Logs can serve as a
longer-term record (typically 7 days on the Free tier).

Deliberately does NOT store user IDs, names, locations, or message text.
Only: event counts, time-of-day bucketing, mode/island choices, errors.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MALTA_TZ = ZoneInfo("Europe/Malta")
logger = logging.getLogger("analytics")

_started_at = datetime.now(MALTA_TZ)
_counters: dict = {
    "total": 0,
    "by_command": defaultdict(int),    # command name → count
    "by_hour": defaultdict(int),       # 0..23 → count
    "by_date": defaultdict(int),       # "YYYY-MM-DD" → count (last 30 days)
    "by_mode": defaultdict(int),       # "car"/"foot" → count
    "by_island": defaultdict(int),     # "gozo"/"malta" → count
    "location_shared": 0,              # how many selections came from real GPS
    "errors": defaultdict(int),        # error kind → count
}


def _emit_log(event: str, metadata: dict) -> None:
    """Emit a structured JSON line for log-based analysis."""
    line = {
        "ts": datetime.now(MALTA_TZ).isoformat(timespec="seconds"),
        "event": event,
        **metadata,
    }
    logger.info("ANALYTICS %s", json.dumps(line, separators=(",", ":")))


def _trim_old_dates() -> None:
    """Keep only the last 30 days in by_date to cap memory."""
    cutoff = (datetime.now(MALTA_TZ) - timedelta(days=30)).date().isoformat()
    for date_str in list(_counters["by_date"].keys()):
        if date_str < cutoff:
            del _counters["by_date"][date_str]


def log_command(command: str) -> None:
    """Record a command invocation."""
    now = datetime.now(MALTA_TZ)
    _counters["total"] += 1
    _counters["by_command"][command] += 1
    _counters["by_hour"][now.hour] += 1
    _counters["by_date"][now.date().isoformat()] += 1
    _trim_old_dates()
    _emit_log("command", {"command": command})


def log_mode_choice(mode: str) -> None:
    """Record the car/foot choice after /next."""
    _counters["by_mode"][mode] += 1
    _emit_log("mode_choice", {"mode": mode})


def log_island_pick(island: str, from_location: bool) -> None:
    """Record which island was picked and whether via GPS or manual button."""
    _counters["by_island"][island] += 1
    if from_location:
        _counters["location_shared"] += 1
    _emit_log(
        "island_pick",
        {"island": island, "via": "location" if from_location else "manual"},
    )


def log_error(kind: str) -> None:
    """Record an error (external API failure, etc)."""
    _counters["errors"][kind] += 1
    _emit_log("error", {"kind": kind})


def _format_uptime() -> str:
    delta = datetime.now(MALTA_TZ) - _started_at
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_summary() -> str:
    """Formatted stats for /stats command (Telegram Markdown)."""
    now = datetime.now(MALTA_TZ)
    today_key = now.date().isoformat()
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    last_7_keys = [
        (now - timedelta(days=i)).date().isoformat() for i in range(7)
    ]

    total = _counters["total"]
    today_count = _counters["by_date"].get(today_key, 0)
    yesterday_count = _counters["by_date"].get(yesterday_key, 0)
    last_7_count = sum(_counters["by_date"].get(d, 0) for d in last_7_keys)

    lines = [
        "📊 *Bot stats* _(since last restart)_\n",
        f"⏱ Uptime: {_format_uptime()}",
        f"📈 Total events: {total}",
        "",
    ]

    # Top commands
    if _counters["by_command"]:
        sorted_cmds = sorted(
            _counters["by_command"].items(), key=lambda x: -x[1]
        )
        lines.append("*Top commands:*")
        for cmd, count in sorted_cmds[:6]:
            pct = 100 * count / total if total else 0
            lines.append(f"  /{cmd}: {count} ({pct:.0f}%)")
        lines.append("")

    # Peak hour
    if _counters["by_hour"]:
        peak_hour, peak_count = max(
            _counters["by_hour"].items(), key=lambda x: x[1]
        )
        lines.append(f"⏰ Peak hour: {peak_hour:02d}:00 ({peak_count} events)")

    # Daily
    lines.append(f"📅 Today: {today_count}")
    lines.append(f"📅 Yesterday: {yesterday_count}")
    lines.append(f"📅 Last 7 days: {last_7_count}")
    lines.append("")

    # Mode & island
    if _counters["by_mode"] or _counters["by_island"]:
        lines.append("*Travel breakdown:*")
        car = _counters["by_mode"].get("car", 0)
        foot = _counters["by_mode"].get("foot", 0)
        if car or foot:
            lines.append(f"  🚗 With car: {car}   🚶 On foot: {foot}")
        gozo = _counters["by_island"].get("gozo", 0)
        malta = _counters["by_island"].get("malta", 0)
        total_island = gozo + malta
        if total_island:
            loc = _counters["location_shared"]
            share_pct = 100 * loc / total_island
            lines.append(f"  📍 From Gozo: {gozo}   From Malta: {malta}")
            lines.append(
                f"  📍 Location shared: {loc}/{total_island} ({share_pct:.0f}%)"
            )
        lines.append("")

    # Errors
    errors = dict(_counters["errors"])
    if errors:
        lines.append("*Errors:*")
        for kind, count in sorted(errors.items(), key=lambda x: -x[1]):
            lines.append(f"  ⚠️ {kind}: {count}")
    else:
        lines.append("✅ No errors recorded")

    return "\n".join(lines)
