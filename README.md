# Stock News Agent

A background agent that watches stock news for your watchlist, scores each story, raises alerts for the ones that matter, and shows everything on a dashboard. It runs on the Finnhub API alone.

## How it works

1. Every 2 minutes the agent pulls company news for each ticker (plus general market news) from the **Finnhub API**.
2. Each new story is scored with keyword rules: sentiment (bullish, bearish, neutral) and impact (1 to 5). Words in the headline count double.
3. Stories scoring 4 or 5 become alerts: a toast in the page, and a desktop notification if you allow it.

Keyword scoring is a rough guide. It reads words, not meaning, so check the story before acting on a score.

## Run it on your computer

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

copy .env.example .env         # Windows
# cp .env.example .env         # macOS / Linux
```

Open `.env` and add your free key from https://finnhub.io/register:

```
FINNHUB_API_KEY=your_key_here
```

Then run:

```bash
python app.py
```

Open http://localhost:5000.

## Settings

Set these in `.env` on your computer, or as environment variables on Render.

| Setting | Required | What it does |
| --- | --- | --- |
| `FINNHUB_API_KEY` | Yes | Your Finnhub API key |
| `APP_PASSWORD` | No | If set, the site asks for this password (any username). Set it whenever the app is on the public internet |
| `WATCHLIST` | No | Starting tickers, comma separated, e.g. `AAPL,MSFT,NVDA`. Used when no saved watchlist exists |
| `SCAN_INTERVAL_SECONDS` | No | Time between scans (default 120, minimum 30) |
| `ALERT_IMPACT_THRESHOLD` | No | Impact score that triggers an alert (default 4) |
| `PORT` | No | Local port (default 5000). Render sets this itself |

To tune the scoring, edit the word lists (`BULL_RE`, `BEAR_RE`, `MAJOR_RE`) in `app.py`.

## Deploy on Render

1. Push the project to GitHub. Never upload `.env`; `.gitignore` keeps it out.
2. On https://render.com, sign in with GitHub, then choose **New + > Web Service** and select the repo.
3. Use these settings:
   - **Language:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
   - **Instance Type:** Free
4. Add environment variables: `FINNHUB_API_KEY`, `APP_PASSWORD`, and optionally `WATCHLIST`.
5. Click **Create Web Service**. When the deploy finishes, open the `.onrender.com` link and sign in with your `APP_PASSWORD`.

Keep the start command at one worker (`--workers 1`). The story feed and the agent live in that process's memory.

Later pushes to GitHub redeploy automatically.

### Free plan limits

- Free services sleep after 15 minutes without visitors and take about a minute to wake up. The agent does not scan while the service is asleep. After it wakes, the first scan reloads the last two days of stories.
- The disk is temporary. Tickers you add or remove in the page and the alert history reset on every restart, so set your tickers with `WATCHLIST`.
- `/healthz` returns `ok` without a password, for uptime checks.

## Notes

- Finnhub's free plan covers US-listed tickers and 60 calls per minute. The watchlist is capped at 15 tickers to stay inside that.
- The API key stays on the server. The browser never sees it.
- Locally, the watchlist is saved in `watchlist.json`. Stories live in memory and reload on the next scan after a restart.
- This summarizes news. It is not investment advice.

## Files

- `app.py` - Flask server, the monitoring agent, Finnhub calls, keyword scoring
- `static/index.html` - the dashboard (no build step)
- `requirements.txt` - Python packages, including `gunicorn` for Render
- `.env.example` - template for your local `.env`
