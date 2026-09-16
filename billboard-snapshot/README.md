# Billboard Calculator (snapshot version)

Same calculator as before, but the deployed app never talks to BigQuery.
Instead, `generate_snapshot.py` (run locally, with your own `gcloud` login)
exports the deduped inventory once into `app/data/snapshot.json`, and
`app/main.py` just reads that file. No GCP credentials needed on the host
you deploy to.

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

## Refreshing the data

There is no scheduled refresh -- re-run `generate_snapshot.py` and push/
redeploy whenever you want newer numbers. The UI shows the snapshot
timestamp so nobody mistakes it for live data.

## Everything else

Dedup logic, CPM calculation, and the greedy solver are unchanged from the
live version -- see the original README's "How the numbers work" section
for the field-by-field explanation of `monthly_price`, `monthly_impression`,
`cpm_calculated`, etc.
