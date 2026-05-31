#!/usr/bin/env python3
"""
app.py - a tiny Flask server for the returns leaderboard.

The chart asks the backend for one month at a time: when you pick a month and year
in the page, it calls /api/returns?period=YYYY-MM and the server answers it.

Where the data comes from (see get_month):
  1. returns_data.json - completed months are committed to the repo and loaded once
     at startup into HISTORY. They are immutable, so serving them is instant and
     needs no network. This file is kept fresh by a monthly GitHub Action.
  2. in-memory cache  - live fetches are cached for 6 hours.
  3. Yahoo (yfinance) - only the in-progress current month (or a completed month not
     yet baked into the file) is fetched live.

Keep this file in the SAME folder as returns-leaderboard.html and returns_data.json.

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000 (it also opens the browser for you).

Deploy on Render (see README.md). The start command there is:
    gunicorn app:app --bind 0.0.0.0:$PORT

Routes:
    /                          serves the chart
    /api/returns?period=YYYY-MM   returns {"period", "data"} for that one month
                                  data is {"rangeLabel", "rows"} or null
                                  ?refresh=1 forces a fresh pull (bypasses HISTORY)
                                  ?demo=1    returns a tiny canned month (no network)

Edit the ASSETS / return math in returns_core.py.
"""

import datetime as dt
import threading
import webbrowser
from pathlib import Path

from flask import Flask, request, Response, jsonify

from returns_core import build_one_month, last_completed_period, load_history


HTML_PATH = Path(__file__).resolve().parent / "returns-leaderboard.html"
HISTORY_PATH = Path(__file__).resolve().parent / "returns_data.json"

# a tiny canned month so ?demo=1 works offline (used only for testing)
_DEMO = {
    "rangeLabel": "demo window",
    "rows": [
        {"name": "EM Equities", "value": 9.7, "cls": "equities"},
        {"name": "Gold", "value": -1.2, "cls": "commodities"},
        {"name": "WTI Crude", "value": -11.6, "cls": "commodities"},
    ],
}

# ---- committed history: completed months, loaded once at startup ----
# load_history never raises (missing/empty/corrupt -> {}), so a bad data file can
# never take the site down; it just degrades to live-fetching every month.
HISTORY = load_history(HISTORY_PATH)
if HISTORY:
    print(f"[history] loaded {len(HISTORY)} completed month(s) from {HISTORY_PATH.name}")
else:
    print(f"[history] no usable {HISTORY_PATH.name}; every month will be live-fetched")


# ---- per-month cache so re-selecting a live month does not re-hit Yahoo ----
_CACHE = {}                              # period -> {"data": ..., "at": datetime}
_CACHE_TTL = dt.timedelta(hours=6)
_LOCK = threading.Lock()


def get_month(period, force=False):
    # Tier 2: immutable committed history. A hit here needs no lock and no network.
    # ?refresh=1 (force) deliberately bypasses it to allow a manual live re-pull.
    if not force and period in HISTORY:
        return HISTORY[period]
    now = dt.datetime.now()
    with _LOCK:
        ent = _CACHE.get(period)                  # Tier 1: live in-memory cache
        if ent and not force and now - ent["at"] < _CACHE_TTL:
            return ent["data"]
        try:                                      # Tier 3: live fetch from Yahoo
            data = build_one_month(period)
        except Exception as exc:                  # network down, Yahoo hiccup, etc.
            print(f"[fetch] failed: {exc}")
            return ent["data"] if ent else None
        if data is not None:
            _CACHE[period] = {"data": data, "at": now}
        return data


app = Flask(__name__)


def _flag(name):
    return request.args.get(name, "").lower() in ("1", "true", "yes", "on")


def _default_period():
    return last_completed_period()   # most recent completed month


@app.route("/")
def index():
    if not HTML_PATH.exists():
        return Response(
            f"Could not find returns-leaderboard.html next to app.py "
            f"(looked in {HTML_PATH.parent}). Put both files in the same folder.",
            status=500,
            mimetype="text/plain",
        )
    return Response(HTML_PATH.read_text(), mimetype="text/html")


@app.route("/api/returns")
def api_returns():
    period = request.args.get("period") or _default_period()
    if _flag("demo"):
        return jsonify({"period": period, "data": _DEMO})
    data = get_month(period, force=_flag("refresh"))
    return jsonify({"period": period, "data": data})


@app.route("/api/history")
def api_history():
    # The full set of completed months, straight from the validated in-memory dict.
    # Instant and network-free; the in-progress month is intentionally absent (the
    # client fetches it on demand via /api/returns).
    return jsonify({"data": HISTORY})


def _open_browser():
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    threading.Timer(1.2, _open_browser).start()
    print("Serving the leaderboard at http://127.0.0.1:5000   (Ctrl+C to stop)")
    print("Completed months load from returns_data.json; the current month is fetched from Yahoo.")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
