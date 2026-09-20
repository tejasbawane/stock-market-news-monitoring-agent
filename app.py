"""
Stock News Agent - local server.

A background agent watches company and market news through the Finnhub API,
scores every new story with keyword rules, raises alerts for high-impact
stories, and serves the dashboard at http://localhost:5000.
"""
import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

# ---------------------------------------------------------------- config
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
SCAN_INTERVAL = max(30, int(os.getenv("SCAN_INTERVAL_SECONDS", "120")))
ALERT_IMPACT = int(os.getenv("ALERT_IMPACT_THRESHOLD", "4"))
PORT = int(os.getenv("PORT", "5000"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "")   # set this when the app is on the public internet

WATCHLIST_FILE = BASE / "watchlist.json"
MAX_WATCHLIST = 15          # keeps scans inside Finnhub's free-tier rate limit
PER_TICKER = 12             # stories pulled per ticker per scan
MARKET_STORIES = 15         # stories pulled from the general market feed
MAX_ARTICLES = 400          # feed size kept in memory
ALERT_MAX_AGE = 6 * 3600    # only alert on stories published in the last 6 hours
QUOTE_TTL = 25              # seconds a quote stays cached

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Starting watchlist when no watchlist.json exists. On hosts with a temporary disk (like
# Render's free plan) that is after every restart, so set WATCHLIST there, e.g. "AAPL,MSFT,NVDA".
DEFAULT_WATCHLIST = [
    s for s in (x.strip().upper() for x in os.getenv("WATCHLIST", "AAPL,MSFT,NVDA,TSLA,AMZN").split(","))
    if SYMBOL_RE.match(s)
] or ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"]

app = Flask(__name__, static_folder=None)


@app.before_request
def require_password():
    """If APP_PASSWORD is set, ask for it (any username) before serving anything except /healthz."""
    if not APP_PASSWORD or request.path == "/healthz":
        return None
    auth = request.authorization
    if auth and hmac.compare_digest((auth.password or "").encode(), APP_PASSWORD.encode()):
        return None
    return Response("Password required.", 401, {"WWW-Authenticate": 'Basic realm="Stock News Agent"'})


@app.get("/healthz")
def healthz():
    return "ok"

# ---------------------------------------------------------------- state
lock = threading.RLock()
wake = threading.Event()
state = {
    "articles": {},      # url -> article
    "alerts": [],        # newest first
    "version": 0,        # bumps whenever the feed changes
    "last_scan": None,
    "next_scan": None,
    "last_error": None,
    "scanning": False,
    "paused": False,
}
quote_cache = {}


# ---------------------------------------------------------------- watchlist
def load_watchlist():
    if not WATCHLIST_FILE.exists():
        return list(DEFAULT_WATCHLIST)
    try:
        data = json.loads(WATCHLIST_FILE.read_text())
        if isinstance(data, list):
            return [s for s in data if isinstance(s, str) and SYMBOL_RE.match(s)]
    except (OSError, ValueError):
        pass
    return list(DEFAULT_WATCHLIST)


def save_watchlist(symbols):
    WATCHLIST_FILE.write_text(json.dumps(symbols))


# ---------------------------------------------------------------- Finnhub
def finnhub(path, **params):
    if not FINNHUB_KEY:
        raise RuntimeError("FINNHUB_API_KEY is not set.")
    params["token"] = FINNHUB_KEY
    r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=15)
    if r.status_code == 429:
        raise RuntimeError("Finnhub rate limit reached. Raise SCAN_INTERVAL_SECONDS or shorten the watchlist.")
    if r.status_code in (401, 403):
        raise RuntimeError("Finnhub refused the request. Check the API key, and note the free plan covers US-listed tickers.")
    r.raise_for_status()
    return r.json()


def fetch_company_news(symbol):
    today = datetime.now(timezone.utc).date()
    data = finnhub(
        "company-news",
        symbol=symbol,
        **{"from": (today - timedelta(days=2)).isoformat(), "to": today.isoformat()},
    )
    return data[:PER_TICKER] if isinstance(data, list) else []


def fetch_market_news():
    data = finnhub("news", category="general")
    return data[:MARKET_STORIES] if isinstance(data, list) else []


def get_quote(symbol):
    now = time.time()
    with lock:
        cached = quote_cache.get(symbol)
    if cached and now - cached[0] < QUOTE_TTL:
        return cached[1]
    try:
        q = finnhub("quote", symbol=symbol)
        if not q.get("c"):
            data = {"error": "No quote available"}
        else:
            data = {"price": q.get("c"), "change": q.get("d"), "percent": q.get("dp"), "prev_close": q.get("pc")}
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as text
        data = {"error": str(exc)}
    with lock:
        quote_cache[symbol] = (now, data)
    return data


# ---------------------------------------------------------------- scoring (keyword rules)
def _pattern(words):
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b")


BULL_RE = _pattern([
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies", "record high", "record revenue",
    "upgrade", "upgraded", "raises guidance", "raised guidance", "raises forecast", "approval", "approved",
    "buyback", "partnership", "wins", "outperform", "jumps", "climbs", "stock split", "dividend hike",
])
BEAR_RE = _pattern([
    "miss", "misses", "plunge", "plunges", "tumble", "tumbles", "slump", "downgrade", "downgraded",
    "cuts guidance", "lowers forecast", "profit warning", "lawsuit", "sued", "probe", "investigation",
    "recall", "layoffs", "job cuts", "bankruptcy", "chapter 11", "fraud", "fined", "subpoena", "delisting",
    "warning", "sinks", "falls", "drops",
])
MAJOR_RE = _pattern([
    "earnings", "guidance", "acquisition", "acquire", "acquires", "merger", "takeover", "sec", "fda",
    "antitrust", "bankruptcy", "chapter 11", "recall", "ceo", "resigns", "layoffs", "downgrade", "upgrade",
    "tariff", "tariffs", "lawsuit", "investigation", "settlement", "plunge", "surge", "soar",
])


def score_article(article):
    """Rate a story from its text. Words in the headline count double."""
    head, body = article["headline"].lower(), article["summary"].lower()

    def hits(rx):
        return rx.findall(head) * 2 + rx.findall(body)

    bull, bear, major = hits(BULL_RE), hits(BEAR_RE), hits(MAJOR_RE)
    if not (bull or bear or major):
        return {"sentiment": "neutral", "impact": 1, "reason": "No market-moving terms found."}

    if len(bull) > len(bear):
        sentiment = "bullish"
    elif len(bear) > len(bull):
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    lean = abs(len(bull) - len(bear))
    impact = min(5, 2 + (len(major) >= 2) + (len(major) >= 4) + (lean >= 3))
    terms = sorted(set(bull + bear + major))
    return {"sentiment": sentiment, "impact": impact, "reason": "Flagged terms: " + ", ".join(terms[:5]) + "."}


# ---------------------------------------------------------------- the agent
def commit(articles):
    """Store scored stories, raise alerts, trim the feed."""
    now = time.time()
    with lock:
        for art in articles:
            state["articles"][art["url"]] = art
            fresh = now - art["published"] < ALERT_MAX_AGE
            if art["impact"] >= ALERT_IMPACT and fresh:
                state["alerts"].insert(0, {
                    "key": art["url"],
                    "symbols": art["symbols"],
                    "headline": art["headline"],
                    "url": art["url"],
                    "impact": art["impact"],
                    "sentiment": art["sentiment"],
                    "reason": art["reason"],
                    "published": art["published"],
                    "raised": now,
                })
        state["alerts"] = state["alerts"][:50]
        if len(state["articles"]) > MAX_ARTICLES:
            newest = sorted(state["articles"].values(), key=lambda a: a["published"], reverse=True)[:MAX_ARTICLES]
            state["articles"] = {a["url"]: a for a in newest}
        state["version"] += 1


def scan_once():
    with lock:
        if state["scanning"]:
            return
        state["scanning"] = True
    errors = []
    try:
        pending = {}
        for sym in load_watchlist() + ["MARKET"]:
            try:
                raw = fetch_market_news() if sym == "MARKET" else fetch_company_news(sym)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{sym}: {exc}")
                if "rate limit" in str(exc).lower():
                    break
                continue
            for item in raw:
                url, headline = item.get("url") or "", (item.get("headline") or "").strip()
                if not url or not headline:
                    continue
                with lock:
                    known = state["articles"].get(url)
                    if known:
                        if sym not in known["symbols"]:
                            known["symbols"].append(sym)
                            state["version"] += 1
                        continue
                if url in pending:
                    if sym not in pending[url]["symbols"]:
                        pending[url]["symbols"].append(sym)
                    continue
                pending[url] = {
                    "symbols": [sym],
                    "headline": headline,
                    "summary": re.sub(r"\s+", " ", item.get("summary") or "").strip()[:600],
                    "source": item.get("source") or "",
                    "url": url,
                    "published": item.get("datetime") or int(time.time()),
                }
            time.sleep(0.3)

        articles = sorted(pending.values(), key=lambda a: a["published"], reverse=True)
        for art in articles:
            art.update(score_article(art))
        if articles:
            commit(articles)
    finally:
        with lock:
            state["scanning"] = False
            state["last_scan"] = time.time()
            state["last_error"] = "; ".join(errors[:3]) or None
            state["version"] += 1


_agent_started = False


def start_agent():
    global _agent_started
    with lock:
        if _agent_started:
            return
        _agent_started = True
    threading.Thread(target=agent_loop, daemon=True).start()


def agent_loop():
    while True:
        if not state["paused"] and FINNHUB_KEY:
            try:
                scan_once()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    state["last_error"] = f"Scan failed: {exc}"
        with lock:
            state["next_scan"] = None if state["paused"] else time.time() + SCAN_INTERVAL
        wake.wait(SCAN_INTERVAL)
        wake.clear()


# ---------------------------------------------------------------- HTTP API
def status_payload():
    with lock:
        return {
            "has_finnhub": bool(FINNHUB_KEY),
            "scan_interval": SCAN_INTERVAL,
            "alert_threshold": ALERT_IMPACT,
            "scanning": state["scanning"],
            "paused": state["paused"],
            "last_scan": state["last_scan"],
            "next_scan": state["next_scan"],
            "last_error": state["last_error"],
            "watchlist": load_watchlist(),
        }


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/feed")
def api_feed():
    client_version = request.args.get("v", type=int)
    with lock:
        version = state["version"]
        if client_version == version:
            return jsonify(unchanged=True, version=version, status=status_payload())
        articles = sorted(state["articles"].values(), key=lambda a: a["published"], reverse=True)
        return jsonify(
            unchanged=False,
            version=version,
            status=status_payload(),
            articles=articles,
            alerts=list(state["alerts"]),
        )


@app.get("/api/quotes")
def api_quotes():
    return jsonify(quotes={sym: get_quote(sym) for sym in load_watchlist()})


@app.post("/api/scan")
def api_scan():
    if not FINNHUB_KEY:
        return jsonify(error="Add FINNHUB_API_KEY to your .env file and restart the server."), 400
    threading.Thread(target=scan_once, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/agent/toggle")
def api_toggle():
    with lock:
        state["paused"] = not state["paused"]
        paused = state["paused"]
        state["next_scan"] = None if paused else state["next_scan"]
        state["version"] += 1
    if not paused:
        wake.set()
    return jsonify(paused=paused)


@app.post("/api/watchlist")
def api_add_symbol():
    body = request.get_json(silent=True) or {}
    symbol = str(body.get("symbol", "")).strip().upper()
    if not SYMBOL_RE.match(symbol):
        return jsonify(error="Enter a ticker such as AAPL or BRK.B."), 400
    if not FINNHUB_KEY:
        return jsonify(error="Add FINNHUB_API_KEY to your .env file first."), 400
    with lock:
        symbols = load_watchlist()
    if symbol in symbols:
        return jsonify(error=f"{symbol} is already on your watchlist."), 409
    if len(symbols) >= MAX_WATCHLIST:
        return jsonify(error=f"The watchlist is limited to {MAX_WATCHLIST} tickers to stay within API rate limits."), 400
    if "error" in get_quote(symbol):
        return jsonify(error=f"No quote found for {symbol}. Finnhub's free plan covers US-listed tickers."), 400
    with lock:
        symbols = load_watchlist()
        symbols.append(symbol)
        save_watchlist(symbols)
        state["version"] += 1
    wake.set()
    return jsonify(watchlist=symbols)


@app.delete("/api/watchlist/<symbol>")
def api_remove_symbol(symbol):
    symbol = symbol.upper()
    with lock:
        save_watchlist([s for s in load_watchlist() if s != symbol])
        for key in list(state["articles"]):
            art = state["articles"][key]
            if symbol in art["symbols"]:
                art["symbols"].remove(symbol)
                if not art["symbols"]:
                    del state["articles"][key]
        state["alerts"] = [a for a in state["alerts"] if a["key"] in state["articles"]]
        state["version"] += 1
    return jsonify(ok=True)


# ---------------------------------------------------------------- run
# Under gunicorn (Render sets RENDER=true) nothing calls app.run(), so start the agent on import.
if os.getenv("RENDER") or os.getenv("START_AGENT_ON_IMPORT") == "1":
    start_agent()

if __name__ == "__main__":
    if not FINNHUB_KEY:
        print("! FINNHUB_API_KEY is not set - copy .env.example to .env and add your key.")
    start_agent()
    print(f"Stock News Agent running at http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
