# Gozo Ferry Bot 🛳

Telegram bot for the Mġarr ↔ Ċirkewwa ferry schedule, with live sea-condition comfort assessment.

## Features

- Next ferry lookup (auto-detects direction from your location)
- Full daily schedule
- Live sea conditions (wind + wave height) from Open-Meteo
- Comfort rating: 🟢 smooth / 🟡 moderate / 🟠 rough / 🔴 very rough

## Commands

| Command | What it does |
|---------|--------------|
| `/start` | Welcome and command list |
| `/next` | Next ferry — detects direction from your location 📍 |
| `/mgarr` | Next 3 departures Mġarr → Ċirkewwa |
| `/cirkewwa` | Next 3 departures Ċirkewwa → Mġarr |
| `/today` | Full schedule for today |
| `/sea` | Current sea conditions in the channel |

After `/next` the bot offers to share your location or pick an island manually. Location detection uses latitude: ~36.00°N is the Malta/Gozo boundary.

## Updating the schedule

Edit `schedule.json` when Gozo Channel publishes a new timetable (seasonal changes ~twice a year). Source: [gozochannel.com/ferry/schedule](https://www.gozochannel.com/ferry/schedule/).

```json
{
  "mgarr_to_cirkewwa": {
    "weekday": ["06:00", "07:00", ...],
    "weekend": [...]
  },
  "cirkewwa_to_mgarr": { ... }
}
```

## Deployment (Render Free)

1. Push repo to GitHub
2. Render → New → Blueprint → select this repo (reads `render.yaml`)
3. Add environment variable `TELEGRAM_BOT_TOKEN` in the Render dashboard
4. Optional: set up an UptimeRobot monitor on `https://<your-service>.onrender.com` every 5 min to keep the Free instance awake

## Local development

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export WEBHOOK_URL="https://your-public-ngrok-or-render-url"
python bot.py
```

For local polling instead of webhooks, swap `run_webhook` for `run_polling` in `main()`.

## Tech stack

- Python 3.12
- python-telegram-bot 20+ (webhook mode)
- httpx (already a PTB dependency, used for Open-Meteo)
- Open-Meteo API (free, no key required)

## What could be added next

- Scrape live "Next Departure" + vehicle queue data from gozochannel.com (requires headless browser — data is JS-rendered)
- Reminders ("notify me 30 min before my ferry")
- Remember last chosen island per user
- Swap to Fast Ferry Valletta–Mġarr schedule as alternative
