"""
Export a point-in-time snapshot of the billboard inventory from BigQuery
into app/data/snapshot.json, which the deployed app reads by default.

The query lives in app/queries.py and is shared with the app's live
BigQuery data source, so the snapshot and live data always have the same
columns. Before writing, the rows are checked with the app's own loader, so
a query change that would break the app fails here instead of in production.

Usage (locally, with your own Google login):
    gcloud auth application-default login     # once, if you haven't already
    pip install -r requirements-snapshot.txt
    python generate_snapshot.py

In CI it uses GOOGLE_APPLICATION_CREDENTIALS (a service-account key file),
see .github/workflows/refresh-snapshot.yml.
"""

import datetime
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent / "app"
sys.path.insert(0, str(APP_DIR))

from data_source import DataStore, fetch_bigquery_rows  # noqa: E402
from queries import OBT_TABLE  # noqa: E402

OUT_PATH = APP_DIR / "data" / "snapshot.json"

# Refuse to replace the snapshot with something much smaller, which usually
# means a broken query or permissions problem rather than real inventory loss.
MIN_KEEP_RATIO = 0.5


def main() -> None:
    print(f"Querying {OBT_TABLE} ...")
    rows = fetch_bigquery_rows(timeout=300)
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Same loader the app uses: raises if a column the app reads is missing.
    store = DataStore(rows, generated_at, "snapshot")
    print(f"{len(rows)} rows, {len(store.billboards)} usable billboards")

    if OUT_PATH.exists() and "--force" not in sys.argv:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            previous = json.load(f).get("row_count") or 0
        if previous and len(rows) < previous * MIN_KEEP_RATIO:
            sys.exit(
                f"Only {len(rows)} rows vs {previous} in the current snapshot; not overwriting. "
                "Re-run with --force if this drop is real."
            )

    payload = {
        "generated_at": generated_at,
        "source_table": OBT_TABLE,
        "row_count": len(rows),
        "rows": rows,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)

    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(f"Wrote {len(rows)} rows ({size_mb:.1f} MB) to {OUT_PATH}")
    print("Commit this file and redeploy to publish the new snapshot.")


if __name__ == "__main__":
    main()
