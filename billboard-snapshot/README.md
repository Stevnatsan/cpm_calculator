# Billboard Calculator (snapshot version)

By default the deployed app never talks to BigQuery.
`generate_snapshot.py` exports the deduped inventory into
`app/data/snapshot.json`, and `app/main.py` reads that file, so no GCP
credentials are needed on the host you deploy to. See "Fresher data" below
for switching to scheduled refreshes or live data.

## 1. Generate the snapshot (local, one-time / whenever you want fresh data)

```bash
gcloud auth application-default login     # once, if you haven't
pip install -r requirements-snapshot.txt
python generate_snapshot.py
```

This writes `app/data/snapshot.json`. Commit it (or re-upload it) whenever
you redeploy.

## 2. Run it locally to check

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
open http://localhost:8000
```

## 3. Deploy

### Vercel
Push this folder to a GitHub repo, import it in Vercel. FastAPI at
`app/main.py` is auto-detected (zero config) as of Vercel's current Python
runtime. Set env vars in the Vercel dashboard:
- `APP_USERNAME`
- `APP_PASSWORD`

### Netlify
Netlify's Python support is less native than Vercel's for ASGI apps --
Vercel is the more direct path for this app as-is. If you specifically
need Netlify, the practical route is wrapping `app.main:app` behind
Netlify's Python function runtime with an ASGI adapter (e.g. Mangum), which
is extra plumbing this repo doesn't include.

## Fresher data

Today the app runs on the committed snapshot and nothing below is switched
on. There are two ways to get fresher numbers, and they can be used
together. Both run the same query (`app/queries.py`) and go through the same
loader (`app/data_source.py`), so every tab (planner, custom plan, swaps,
market, budget curve, saved plans) sees the same data either way.

### Before either option: a service account

1. In Google Cloud (project `stickearn-bi`, or whichever project should pay
   for the queries), create a service account, e.g. `cpm-calculator-reader`.
2. Give it **BigQuery Job User** on the project that runs the queries, and
   **BigQuery Data Viewer** on the `cleanse_playlog` dataset.
3. Create a JSON key for it and download it. Treat it like a password.

### Option A: scheduled snapshot refresh (recommended first)

A GitHub Action (`.github/workflows/refresh-snapshot.yml`) regenerates
`snapshot.json` every Monday at 08:00 WIB and commits it, and Vercel
redeploys on that commit. It does nothing until you add the key:

1. GitHub repo > Settings > Secrets and variables > Actions > New repository
   secret. Name: `GCP_SA_KEY`, value: the whole JSON key file.
2. Optional: a repository *variable* `BQ_PROJECT` if queries should be billed
   to a project other than `stickearn-bi`.
3. Run it once by hand: Actions > Refresh snapshot > Run workflow.

The app stays a fast, credential-free snapshot app; the data is at most a
week old. Change the `cron` line to refresh more or less often. The script
refuses to write a snapshot with less than half the rows of the current one
(usually a broken query or permission); tick "force" when running by hand if
the drop is real.

### Option B: live data in the app

The app queries BigQuery itself and caches the result (1 hour by default).
In Vercel > Project > Settings > Environment Variables, add:

| Variable | Value |
| --- | --- |
| `DATA_SOURCE` | `bigquery` |
| `GOOGLE_APPLICATION_CREDENTIALS_JSON` | the whole JSON key file |
| `DATA_CACHE_SECONDS` | optional, default `3600` |
| `BQ_PROJECT` / `BQ_TABLE` | optional, defaults in `app/queries.py` |

and add `google-cloud-bigquery>=3.17` to `requirements.txt`, then redeploy.

How it behaves:
- Live data is used only when `DATA_SOURCE=bigquery` **and** a credential is
  set. Otherwise the app uses the snapshot, as today.
- If a query fails (permissions, timeout, quota), the app keeps the last
  live data it loaded, or falls back to the snapshot, and retries after 5
  minutes (`DATA_RETRY_SECONDS`). It never goes down because of BigQuery.
- The banner shows "Live data, refreshed ..." or "Snapshot data as of ...",
  plus a note when it had to fall back. `/api/meta` returns `data_source`
  and `data_note` too.
- Each Vercel instance runs the query on its first request and then once
  per cache period, so BigQuery is billed per run. The query reads the whole
  OBT table; check its size in the BigQuery console before choosing a short
  cache time. That first request also waits for the query (Vercel's limit
  here is 30 s, see `vercel.json`).

To go back to the snapshot, remove `DATA_SOURCE` or set it to `snapshot`.

### By hand

Still works as before: `python generate_snapshot.py` with your own
`gcloud auth application-default login`, then commit and push.

### Tests

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests
```

These check that every endpoint answers on the snapshot, that the query
selects every column the app reads, and the live/fallback switching (with
BigQuery faked, no credentials needed).

## Everything else

Dedup logic, CPM calculation, and the greedy solver are unchanged from the
live version -- see the original README's "How the numbers work" section
for the field-by-field explanation of `monthly_price`, `monthly_impression`,
`cpm_calculated`, etc.
