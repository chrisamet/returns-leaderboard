# Returns Leaderboard

A small Flask app that shows monthly asset-class returns as an interactive bar chart.
Pick a month and year: completed months are served instantly from a committed data file,
and the current in-progress month is fetched live from Yahoo Finance (via yfinance).

## How the data works
Monthly returns for **completed** months never change, so they are computed once and
stored in a version-controlled file, `returns_data.json`, that ships with the app:

- **Completed months** are loaded from `returns_data.json` at startup - instant, no
  network, and durable across restarts/redeploys (the data lives in the repo, not on the
  server's ephemeral disk).
- **The current, in-progress month** (and any completed month not yet baked into the
  file) is fetched live from Yahoo and cached in memory for 6 hours.
- A monthly **GitHub Action** (`.github/workflows/update-returns.yml`) re-runs the fetcher,
  appends newly completed months to `returns_data.json`, commits, and pushes - which
  triggers a Render redeploy. The file stays current with no manual work.

The in-progress month is never written to the file; only completed months are stored.

## Files
- `app.py` - Flask server: serves the chart and `/api/returns`, with the 3-tier
  read-through (committed history -> in-memory cache -> live Yahoo fetch)
- `returns_core.py` - shared asset list, return math, and Yahoo fetchers (one source of truth)
- `fetch_returns.py` - CLI to backfill / refresh `returns_data.json` (used by the Action)
- `returns_data.json` - committed history of completed months (the durable cache)
- `returns-leaderboard.html` - the chart UI (served by `app.py`)
- `.github/workflows/update-returns.yml` - monthly data refresh
- `requirements.txt` - dependencies
- `render.yaml` - Render blueprint (optional)
- `.python-version` - pinned Python for Render and the Action

## Run locally
```
pip install -r requirements.txt
python app.py
```
Then open http://127.0.0.1:5000.

> On macOS, port 5000 is often held by the AirPlay Receiver (ControlCenter). Either turn it
> off under System Settings -> General -> AirDrop & Handoff, or serve on another port, e.g.
> `gunicorn app:app --bind 127.0.0.1:5057`.

## Refresh / backfill the data
```
python fetch_returns.py --update          # merge the last 3 completed months into the file
python fetch_returns.py --start 2016-01    # (re)build history from a start month
python fetch_returns.py --selftest         # offline checks of the math + file logic, no network
```
To change the tracked assets, edit `ASSETS` in `returns_core.py`.

## Deploy to Render

**Option A - dashboard**
1. Push this folder to a GitHub repo.
2. Render dashboard -> New -> Web Service -> connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Plan: Free, then Create. The app goes live at an `onrender.com` URL.

**Option B - blueprint**
Push the repo (including `render.yaml`), then Render dashboard -> New -> Blueprint and
select the repo. It reads `render.yaml` and configures the service.

After pushing, enable the GitHub Action (Actions tab). It runs monthly and can be triggered
manually via "Run workflow"; it already declares the `contents: write` permission it needs
to push the data commit.

## Notes
- Free Render web services sleep after ~15 min idle; the first visit after that has a cold
  start of roughly a minute. Completed months still render instantly from the committed file.
- yfinance pulls from Yahoo. Datacenter IPs (Render, GitHub Actions) are sometimes
  rate-limited; the fetcher retries per ticker, and the Action commits only when it actually
  gets data, so a hiccup is a harmless no-op (past months are already committed).
- If a build complains about the Python version, change `.python-version` to one Render supports.
