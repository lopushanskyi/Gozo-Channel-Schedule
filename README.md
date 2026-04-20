# Gozo Ferry Bot 🛳

Telegram bot covering **both** Malta–Gozo ferry operators, with live sea-condition comfort assessment.

## Operators

| | Gozo Channel | Gozo Fast Ferry |
|---|---|---|
| Route | Ċirkewwa ↔ Mġarr | Valletta ↔ Mġarr |
| Vehicles | ✅ | ❌ passenger only |
| Crossing | ~25 min | ~45 min |
| Booking | not required | may be required |
| Hours | 24/7 | ~06:45 – 20:45 |

## Features

- Next-ferry lookup with auto-direction from your location
- Picks the right operator based on travel mode (with car / on foot)
- Live daily schedules from each operator's own data source
- Fast Ferry **seat warnings** when availability drops below 30
- Holiday-schedule detection 🎉 (Gozo Channel)
- Sea conditions (wind + wave height) from Open-Meteo
- Comfort rating: 🟢 smooth / 🟡 moderate / 🟠 rough / 🔴 very rough
- Falls back to bundled `schedule.json` if Gozo Channel CDN is down

## Commands

| Command | What it does |
|---------|--------------|
| `/start` | Welcome and command list |
| `/next` | Next ferry — location + car/foot flow 📍 |
| `/mgarr` | Gozo Channel: next 3 from Mġarr → Ċirkewwa |
| `/cirkewwa` | Gozo Channel: next 3 from Ċirkewwa → Mġarr |
| `/fastferry` | Fast Ferry: next in both directions |
| `/today` | Full schedule for today (both operators) |
| `/sea` | Current sea conditions in the channel |

### `/next` conversation flow

```
/next
 → Where are you sailing from?
    [📍 Share location] [🏝 I'm on Gozo] [🇲🇹 I'm on Malta]
 → Travelling with a car or on foot?
    [🚗 With a car] [🚶 On foot]
 → Results
```

- **With a car** → only Gozo Channel (Fast Ferry doesn't take vehicles)
- **On foot** → both operators, so the user can pick based on where they actually are (Ċirkewwa is on Malta's north coast, Valletta is central)

## Data sources

### Gozo Channel (static JSON, per date)
```
https://static.gozochannel.com/schedules/YYYY/MM/DD/passenger.json
```
Schema: `{ date, is_holiday, times: { mgarr: [...], cirkewwa: [...] } }`

Each array ends with the next day's early-morning runs; the bot handles the midnight rollover automatically.

### Gozo Fast Ferry (REST)
```
https://gozohighspeed.com/api/Trip
  ?departingHarbor=Valletta
  &arrivingHarbor=Imgarr%20(Gozo)
  &date=YYYY-MM-DD
```
Returns trips with `departingTime`, `vesselName`, `seatsEconomy`. Cached 5 min (seats change as bookings happen).

### Weather — Open-Meteo
- Marine API for wave height
- Forecast API for wind speed (knots) and direction
- Channel midpoint: 36.015°N, 14.296°E
- No API key, cached 10 min

## Deployment (Render Free Web Service)

1. Push to GitHub
2. Render → New → Blueprint → select repo (reads `render.yaml`)
3. Set env variable `TELEGRAM_BOT_TOKEN` in Render dashboard
4. Optional: UptimeRobot on `https://<service>.onrender.com` every 5 min to prevent spin-down

## Local development

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="..."
export WEBHOOK_URL="https://your-tunnel-url"
python bot.py
```

## Tech stack

- Python 3.12
- python-telegram-bot 20+ `[webhooks]`
- httpx (async HTTP)

## Architecture notes

- **State management**: `context.user_data['pending_island']` carries the island between location and mode messages; cleared after results sent. Ephemeral — resets on process restart (fine).
- **Caching**: Gozo Channel per-date indefinitely; Fast Ferry per (direction, date) for 5 min; weather 10 min.
- **Timezone**: all datetimes in `Europe/Malta` — DST automatic via `zoneinfo`.
- **Day rollover**: Gozo Channel JSON uses time-went-backwards detection; Fast Ferry has explicit ISO timestamps so it's trivial.

## What could be added next

- Vehicle queue count (source not yet identified — Gozo Channel uses a live feed that's hard to find)
- "Smart pick" — automatically recommend the operator based on distance to Ċirkewwa vs Valletta
- Reminders ("notify me 30 min before next ferry")
- Remember user's default mode so `/next` skips the question next time
- Price comparison
