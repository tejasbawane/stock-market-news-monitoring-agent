# Stock News Agent

A background agent that watches stock news for your watchlist, scores each story, raises alerts for the ones that matter, and shows everything on a dashboard at **http://localhost:5000**.

## How it works

1. Every 2 minutes the agent pulls company news for each ticker (plus general market news) from the **Finnhub API**.
2. New stories are scored for sentiment (bullish, bearish, neutral) and impact (1 to 5). With an Anthropic key, **Claude** does the scoring. Without one, simple keyword rules do it.
3. Stories scoring 4 or 5 become alerts: a toast in the page, and a desktop notification if you allow it.
4. "Write briefing" asks Claude for a short summary of the last 24 hours.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

copy .env.example .env         # Windows
# cp .env.example .env         # macOS / Linux
```

Open `.env` and add your keys:

| Key | Required | Where to get it |
| --- | --- | --- |
| `FINNHUB_API_KEY` | Yes | https://finnhub.io/register (free) |

Then run:

```bash
python app.py
```

Open http://localhost:5000.

## Settings (in `.env`)

- `SCAN_INTERVAL_SECONDS` - time between scans (default 120, minimum 30)
- `ALERT_IMPACT_THRESHOLD` - impact score that triggers an alert (default 4)
- `ANTHROPIC_MODEL` - default `claude-sonnet-5`; `claude-haiku-4-5-20251001` is cheaper
- `PORT` - default 5000

## Notes

- Finnhub's free plan covers US-listed tickers and 60 calls per minute. The watchlist is capped at 15 tickers to stay inside that.
- API keys stay on the server. The browser never sees them.
- The watchlist is saved in `watchlist.json`. Stories live in memory and reload on the next scan after a restart.
- This summarizes news. It is not investment advice.

## Files

- `app.py` - Flask server, the monitoring agent, Finnhub and Claude calls
- `static/index.html` - the dashboard (no build step)
