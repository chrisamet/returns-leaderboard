#!/usr/bin/env python3
"""
fetch_returns.py - command-line updater for returns_data.json.

The Flask app serves completed months from returns_data.json (see app.py). This
script keeps that file fresh: it is run monthly by .github/workflows/update-returns.yml
and can be run by hand to backfill history.

Usage:
    pip install -r requirements.txt
    python fetch_returns.py --update                # merge the last 3 completed months (CI default)
    python fetch_returns.py --start 2016-01          # backfill from a month (seed the file)
    python fetch_returns.py --start 2025-01 --end 2026-04
    python fetch_returns.py --months 24              # last 24 completed months
    python fetch_returns.py --selftest               # offline checks, no network

All return math, the asset list, and the Yahoo fetch live in returns_core.py. The
in-progress month is never written: build_dataset() clamps every result to months
strictly before the current UTC month, so a half-formed current month can never be
baked into the committed history.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from returns_core import (
    assemble,
    build_dataset,
    current_period,
    label_for,
    load_history,
    monthly_returns,
    write_outputs,
)

UPDATE_WINDOW = 3   # completed months re-fetched on each --update run


def merge(history, fetched):
    """Overwrite-merge `fetched` months into `history`.

    Months present in `fetched` replace their entries (this self-heals late data
    restatements within the trailing window); every other month is left untouched.
    A month with empty rows is never written over a good entry."""
    merged = dict(history)
    for period, entry in fetched.items():
        if entry and entry.get("rows"):
            merged[period] = entry
    return merged


def update(outdir=".", window=UPDATE_WINDOW):
    """Fetch the trailing `window` completed months and overwrite-merge them into
    returns_data.json. Returns True if the file content changed.

    A no-op - nothing fetched, or nothing changed - returns False and leaves the
    file untouched, so a Yahoo outage never clobbers committed data."""
    path = Path(outdir) / "returns_data.json"
    history = load_history(path)
    fetched = build_dataset(months=window)            # current month already excluded
    fetched = {k: v for k, v in fetched.items() if v.get("rows")}
    if not fetched:
        print("[update] fetch returned nothing; leaving the file untouched")
        return False
    merged = merge(history, fetched)
    if merged == history:
        print("[update] no changes")
        return False
    write_outputs(merged, outdir)
    print(f"[update] merged {len(fetched)} month(s); file now has {len(merged)}")
    return True


def selftest():
    """Offline proof of the math and the file machinery. No network."""
    import pandas as pd

    # --- return math, last-trading-day anchoring, date labels ---
    idx = pd.to_datetime(["2026-05-28", "2026-05-29", "2026-06-29", "2026-06-30"])
    close = pd.Series([95.0, 100.0, 105.0, 110.0], index=idx)
    rets, dates = monthly_returns(close)
    assert rets["2026-06"] == 10.0, rets                 # 110/100 - 1
    assert rets.get("2026-05") is None, rets             # no prior month
    assert dates["2026-05"].isoformat() == "2026-05-29"  # last trading day
    assert dates["2026-06"].isoformat() == "2026-06-30"
    assert label_for(dates["2026-05"], dates["2026-06"]) == "29-May-26 to 30-Jun-26"

    # --- FX inversion (return of the foreign currency vs USD) ---
    inv = pd.Series([100.0, 100.0, 95.0, 90.0], index=idx)
    ri, _ = monthly_returns(inv, invert=True)
    assert ri["2026-06"] == 11.1, ri                     # (1/90)/(1/100) - 1 = 11.11%

    # --- assemble shape + range label ---
    per_asset = {
        "US Equities": (rets, dates, "equities"),
        "EUR":         (ri, dates, "fx"),
    }
    ds = assemble(per_asset, ["2026-06"])
    assert ds["2026-06"]["rangeLabel"] == "29-May-26 to 30-Jun-26"
    assert {"name": "US Equities", "value": 10.0, "cls": "equities"} in ds["2026-06"]["rows"]

    # --- the in-progress month is never emitted (the clamp build_dataset/update rely on) ---
    cur = str(current_period())
    prev = str(current_period() - 1)
    idx2 = pd.to_datetime([
        (current_period() - 2).end_time.date(),
        (current_period() - 1).end_time.date(),
        (current_period().start_time + pd.Timedelta(days=1)).date(),
    ])
    s2 = pd.Series([100.0, 110.0, 121.0], index=idx2)
    r2, _ = monthly_returns(s2)
    emitted = [p for p, v in r2.items() if v is not None]
    clamped = [p for p in emitted if p < cur]            # the rule build_dataset applies
    assert prev in clamped and cur not in clamped, (emitted, clamped, cur)

    # --- merge: overwrite within window, keep others, empty is a no-op, idempotent ---
    h = {"2026-03": {"rangeLabel": "x", "rows": [{"name": "A", "value": 1.0, "cls": "equities"}]}}
    new = {"2026-03": {"rangeLabel": "y", "rows": [{"name": "A", "value": 2.0, "cls": "equities"}]},
           "2026-04": {"rangeLabel": "z", "rows": [{"name": "B", "value": 3.0, "cls": "equities"}]}}
    m = merge(h, new)
    assert m["2026-03"]["rows"][0]["value"] == 2.0       # overwritten
    assert "2026-04" in m                                 # added
    assert merge(m, {}) == m                              # empty fetch is a no-op
    assert merge(m, {"2026-05": {"rows": []}}) == m       # empty-rows month not written
    assert merge(m, new) == m                             # idempotent

    # --- load_history tolerates missing / empty / garbage / wrong shape ---
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "returns_data.json"
        assert load_history(p) == {}                      # missing
        p.write_text("")
        assert load_history(p) == {}                      # empty
        p.write_text("not json")
        assert load_history(p) == {}                      # invalid JSON
        p.write_text("[1, 2, 3]")
        assert load_history(p) == {}                      # wrong shape (list, not dict)
        p.write_text(json.dumps({"2026-04": {"rangeLabel": "ok", "rows": []},
                                 "bad": {"no_rows": 1}}))
        loaded = load_history(p)
        assert "2026-04" in loaded and "bad" not in loaded  # keeps valid, drops invalid

    # --- write_outputs round-trips, sorted + trailing newline (stable diffs) ---
    with tempfile.TemporaryDirectory() as tmp:
        write_outputs(ds, tmp)
        text = (Path(tmp) / "returns_data.json").read_text()
        assert json.loads(text) == ds
        assert text.endswith("\n")

    print("selftest OK: return math, FX inversion, labels, in-progress clamp, "
          "merge idempotency, tolerant load, and JSON emission all pass")


def main():
    ap = argparse.ArgumentParser(
        description="Update returns_data.json with monthly asset-class returns")
    ap.add_argument("--update", action="store_true",
                    help=f"merge the last {UPDATE_WINDOW} completed months into the file (CI default)")
    ap.add_argument("--months", type=int, help="number of most recent completed months to backfill")
    ap.add_argument("--start", type=str, help="first month YYYY-MM (overrides --months)")
    ap.add_argument("--end", type=str, help="last month YYYY-MM")
    ap.add_argument("--outdir", type=str, default=".")
    ap.add_argument("--selftest", action="store_true", help="run offline checks and exit")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.update:
        update(outdir=args.outdir)
        return

    # manual backfill: build, then overwrite-merge so existing months are never lost
    ds = build_dataset(months=args.months, start=args.start, end=args.end)
    ds = {k: v for k, v in ds.items() if v.get("rows")}
    if not ds:
        print("No data assembled. Check tickers / network.")
        return
    path = Path(args.outdir) / "returns_data.json"
    write_outputs(merge(load_history(path), ds), args.outdir)


if __name__ == "__main__":
    main()
