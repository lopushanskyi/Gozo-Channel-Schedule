# Gozo Ferry Bot 🛳

Telegram bot for the Mġarr ↔ Ċirkewwa ferry schedule, with live sea-condition comfort assessment.

## Features

- **Live daily schedule** from Gozo Channel's own CDN (`static.gozochannel.com`)
- Holiday-schedule detection (🎉 banner when `is_holiday: true`)
- Next ferry lookup with location auto-detection
- Live sea conditions (wind + wave height) from Open-Meteo
- Sea comfort rating: 🟢 smooth / 🟡 moderate / 🟠 rough / 🔴 very rough
- Falls back to bundled schedule.json if the live source is unavailable

## Commands

| Command | What it does |
|---------|--------------|
| `/start` | Welcome and command list |
| `/next` | Next ferry — detects direction from your location 📍 |
| `/mgarr` | Next 3 departures Mġarr → Ċirkewwa |
| `/cirkewwa` | Next 3 departures Ċirkewwa → Mġarr |
| `/today` | Full schedule for today |
| `/sea` | Current sea conditions in the channel |

## Data sources

### Primary: Gozo Channel static JSON

URL pattern:
```
https://static.gozochannel.com/schedules/YYYY/MM/DD/passenger.json
```

Schema:
```json
{
  "date": "2026-04-20",
  "is_holiday": false,
  "times": {
    "mgarr":    [{"name": "00:00", ...}, {"name": "00:45", ...}, ...],
    "cirkewwa": [{"name": "00:00", ...}, ...]
  }
}
```

Each array contains the full operational day ending in the next day's early-morning runs (00:00, 00:45, 01:30). The bot handles the rollover automatically.

### Fallback: bundled `schedule.json`

Used only when the live source is unreachable. Weekday/weekend approximation — less accurate than live data. Update it occasionally or remove if you're confident in the live source.

### Weather: Open-Meteo

- Marine API for wave height (`wave_height` current)
- Forecast API for wind speed/direction at 10m altitude, in knots
- Coordinates: 36.015°N, 14.296°E (mid-channel)
- No API key, cached for 10 minutes

## Deployment (Render Free Web Service)

1. Push repo to GitHub
2. Render → New → Blueprint → select this repo (reads `render.yaml`)
3. Add env variable `TELEGRAM_BOT_TOKEN` in the Render dashboard
4. Optional: UptimeRobot monitor on `https://<service>.onrender.com` every 5 min to prevent spin-down

## Local development

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export WEBHOOK_URL="https://your-ngrok-or-render-url"
python bot.py
```

## Tech stack

- Python 3.12
- python-telegram-bot 20+ with `[webhooks]` extras
- httpx (async HTTP for API calls)

## Architecture notes

- **Caching**: live schedule is cached per calendar date (infinite TTL within the day, the day changes so a new fetch happens); weather cached 10 min
- **Timezone**: all datetimes are `Europe/Malta` — DST handled automatically by `zoneinfo`
- **Rollover**: when a schedule entry's time is less than the previous (e.g. "00:00" after "23:15"), we treat it as the next calendar day

## What could be added next

- Live vehicle queue counts (from the homepage "cars waiting to board" widget — would need to scrape or find that API)
- Reminders ("notify me 30 min before next ferry")
- Remember last chosen island per user
- Fast Ferry Valletta–Mġarr alternative route
- Tomorrow's schedule command
