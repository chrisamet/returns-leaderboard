#!/usr/bin/env python3
"""
returns_core.py - shared logic for the returns leaderboard.

Single source of truth for the asset list, the month-over-month return math, the
Yahoo (yfinance) fetchers, and the JSON history-file helpers. Imported by both the
Flask app (app.py) and the CLI updater (fetch_returns.py) so the logic lives in one
place.

yfinance is imported lazily inside the fetchers, so importing this module is cheap
and network-free: the app boot and the offline selftest never touch the network.

Returns are month-end to month-end, anchored to the last available TRADING day of
each calendar month (matches the reference leaderboard's date windows). ETFs are
downloaded dividend/split adjusted (auto_adjust=True), so figures approximate TOTAL
return. FX rows with invert=True read as the foreign currency's return vs USD.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd


# ---- config: (display name, yahoo ticker, css class, invert) ----
#   css class is one of: equities | fixedIncome | commodities | fx | realEstate | crypto
#   invert=True takes 1/price first (used for USDxxx FX pairs).
ASSETS = [
    ("EM Equities",        "EEM",       "equities",    False),
    ("NASDAQ",             "QQQ",       "equities",    False),
    ("Magnificent 7",      "MAGS",      "equities",    False),
    ("US Equities",        "SPY",       "equities",    False),
    ("Global Equities",    "ACWI",      "equities",    False),
    ("Industrial Metals",  "DBB",       "commodities", False),
    ("Japan Equities",     "EWJ",       "equities",    False),
    ("US Small Cap",       "IWM",       "equities",    False),
    ("Euro Area Equities", "EZU",       "equities",    False),
    ("Germany Equities",   "EWG",       "equities",    False),
    ("Silver",             "SLV",       "commodities", False),
    ("Dollar Index",       "DX-Y.NYB",  "fx",          False),
    ("US IG Corporate",    "LQD",       "fixedIncome", False),
    ("US High Yield",      "HYG",       "fixedIncome", False),
    ("US Treasury Bills",  "BIL",       "fixedIncome", False),
    ("US TIPS",            "TIP",       "fixedIncome", False),
    ("US Treasuries",      "GOVT",      "fixedIncome", False),
    ("US REITs",           "VNQ",       "realEstate",  False),
    ("UK Equities",        "EWU",       "equities",    False),
    ("EUR",                "EURUSD=X",  "fx",          False),
    ("GBP",                "GBPUSD=X",  "fx",          False),
    ("Gold",               "GLD",       "commodities", False),
    ("CAD",                "USDCAD=X",  "fx",          True),
    ("JPY",                "USDJPY=X",  "fx",          True),
    ("China Equities",     "MCHI",      "equities",    False),
    ("Bitcoin",            "BTC-USD",   "crypto",      False),
    ("Commodities",        "DBC",       "commodities", False),
    ("Ethereum",           "ETH-USD",   "crypto",      False),
    ("WTI Crude",          "CL=F",      "commodities", False),
]

MONTH_ABBR = {i: calendar.month_abbr[i] for i in range(1, 13)}


# ---- period helpers (UTC-anchored) -----------------------------------------
def current_period():
    """The in-progress calendar month as a pandas Period, anchored to UTC.

    UTC keeps the 'is this month complete?' boundary stable across the timezone
    where the code runs (GitHub Actions runners and Render are UTC)."""
    today_utc = dt.datetime.now(dt.timezone.utc).date()
    return pd.Period(today_utc, "M")


def last_completed_period():
    """The most recent completed month as a 'YYYY-MM' string."""
    return str(current_period() - 1)


# ---- pure helpers (no network) ---------------------------------------------
def monthly_returns(daily_close, invert=False):
    """Month-over-month return from a daily close Series. Returns two dicts keyed
    by 'YYYY-MM': percent returns (None for the first month) and the last trading
    date used for each month."""
    s = daily_close.dropna().astype(float)
    if invert:
        s = 1.0 / s
    s.index = pd.to_datetime(s.index)
    s = s.sort_index()

    periods = s.index.to_period("M")
    last_close = s.groupby(periods).last()
    last_date = pd.Series(s.index, index=s.index).groupby(periods).max()
    ret = last_close.pct_change(fill_method=None) * 100.0

    out_ret, out_date = {}, {}
    for p in ret.index:
        key = str(p)
        out_ret[key] = None if pd.isna(ret[p]) else round(float(ret[p]), 1)
        d = last_date[p]
        out_date[key] = None if pd.isna(d) else pd.Timestamp(d).date()
    return out_ret, out_date


def label_for(prev_date, this_date):
    """'30-Apr-26 to 29-May-26' style window label from two date objects."""
    if prev_date is None or this_date is None:
        return None
    fmt = lambda d: f"{d.day:02d}-{MONTH_ABBR[d.month]}-{str(d.year)[2:]}"
    return f"{fmt(prev_date)} to {fmt(this_date)}"


def assemble(per_asset, periods_wanted):
    """Build {period: {rangeLabel, rows}} from per-asset {name: (returns, dates, cls)}."""
    dataset = {}
    for p in periods_wanted:
        rows = []
        for name, (rets, _dates, cls) in per_asset.items():
            v = rets.get(p)
            if v is not None:
                rows.append({"name": name, "value": v, "cls": cls})
        if not rows:
            continue
        prev_p = str(pd.Period(p, "M") - 1)
        label = None
        for name, (_r, dates, _c) in per_asset.items():
            label = label_for(dates.get(prev_p), dates.get(p))
            if label:
                break
        dataset[p] = {"rangeLabel": label, "rows": rows}
    return dataset


# ---- yfinance fetch (lazy import; resilient to datacenter rate-limiting) ----
def _fetch_per_asset(dl_start, dl_end):
    """Download adjusted daily closes for every asset over [dl_start, dl_end] and
    return {name: (returns, dates, cls)}.

    Yahoo rate-limits datacenter IPs (CI / Render), so a single download can drop
    tickers. We download the full list up to 3 times, accumulating any tickers that
    came back with data, and back off between tries. Always requesting the full list
    keeps the response a (ticker, field) multiindex, so extraction stays uniform."""
    import yfinance as yf

    tickers = [a[1] for a in ASSETS]
    start = dl_start.strftime("%Y-%m-%d")
    end = (dl_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    closes = {}
    for attempt in range(3):
        raw = yf.download(
            tickers, start=start, end=end, auto_adjust=True,
            progress=False, group_by="ticker", threads=True,
        )
        level0 = set(raw.columns.get_level_values(0)) if hasattr(raw.columns, "get_level_values") else set()
        for _name, tkr, _cls, _inv in ASSETS:
            if tkr in closes:
                continue
            close = raw[tkr]["Close"] if tkr in level0 else None
            if close is not None and not close.dropna().empty:
                closes[tkr] = close
        got, total = len(closes), len(tickers)
        print(f"[fetch] {start} -> {end}: {got}/{total} tickers (attempt {attempt + 1})")
        if got == total:
            break
        if attempt < 2:
            time.sleep(2 * (attempt + 1))

    per_asset = {}
    for name, tkr, cls, invert in ASSETS:
        close = closes.get(tkr)
        if close is None or close.dropna().empty:
            continue
        rets, dates = monthly_returns(close, invert=invert)
        per_asset[name] = (rets, dates, cls)
    return per_asset


def build_one_month(period):
    """Fetch and return {rangeLabel, rows} for one month, or None.

    Uses a narrow window (the prior two months + the target) so pct_change has a
    predecessor for the target month."""
    p = pd.Period(period, "M")
    per_asset = _fetch_per_asset((p - 2).start_time, p.end_time)
    return assemble(per_asset, [period]).get(period)


def build_dataset(months=None, start=None, end=None):
    """Fetch many months and return {period: {rangeLabel, rows}}.

    The in-progress (and any future) month is ALWAYS excluded: every period emitted
    is strictly before current_period(). This is the single chokepoint that makes it
    impossible to bake a half-formed current month into the committed history,
    regardless of the start/end/months arguments."""
    if start:
        dl_start = (pd.Period(start, "M") - 2).start_time
    else:
        n = months or 12
        dl_start = (current_period() - (n + 2)).start_time
    dl_end = pd.Period(end, "M").end_time if end else pd.Timestamp.today()

    per_asset = _fetch_per_asset(dl_start, dl_end)

    all_periods = sorted({p for (r, _d, _c) in per_asset.values()
                          for p, v in r.items() if v is not None})

    cur = str(current_period())
    all_periods = [p for p in all_periods if p < cur]   # never the in-progress month
    if start:
        all_periods = [p for p in all_periods if p >= start]
    if end:
        all_periods = [p for p in all_periods if p <= end]
    if months and not start:
        all_periods = all_periods[-months:]

    return assemble(per_asset, all_periods)


# ---- JSON history file -----------------------------------------------------
def load_history(path):
    """Load the committed history file as {period: {rangeLabel, rows}}.

    Returns {} on any problem (missing, empty, invalid JSON, wrong shape) without
    raising, so a bad or absent data file never breaks app boot or the updater."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(v, dict) and isinstance(v.get("rows"), list)}


def write_outputs(dataset, outdir="."):
    """Write the dataset to returns_data.json with stable, minimal git diffs
    (sorted keys, 2-space indent, trailing newline)."""
    out = Path(outdir) / "returns_data.json"
    out.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(dataset)} month(s) -> {out}")
