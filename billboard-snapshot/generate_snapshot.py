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

Without BigQuery access, run the query in the BigQuery console instead,
save the results as CSV and load that file:
    python generate_snapshot.py --csv bq-results.csv
"""

import csv
import datetime
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent / "app"
sys.path.insert(0, str(APP_DIR))

from data_source import ALL_COLUMNS, DataStore, fetch_bigquery_rows  # noqa: E402
from queries import SOURCE_DESCRIPTION  # noqa: E402

OUT_PATH = APP_DIR / "data" / "snapshot.json"

# Refuse to replace the snapshot with something much smaller, which usually
# means a broken query or permissions problem rather than real inventory loss.
MIN_KEEP_RATIO = 0.5

# CSV cells are all text; these become numbers (empty -> null), the rest stay text.
NUMBER_COLUMNS = {
    "latitude", "longitude", "monthly_price", "monthly_impression", "cpm",
    "cpm_calculated", "size_x", "size_y", "area_m2",
}


def _csv_value(col: str, v: str):
    if col == "inventory_id":
        return int(v) if v.isdigit() else v
    if col in NUMBER_COLUMNS:
        return float(v) if v.strip() else None
    return v


def rows_from_csv(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [
            {c: _csv_value(c, r[c]) for c in ALL_COLUMNS if c in r}
            for r in csv.DictReader(f)
        ]


def main() -> None:
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "--csv" in sys.argv:
        csv_path = sys.argv[sys.argv.index("--csv") + 1]
        print(f"Reading {csv_path} ...")
        rows = rows_from_csv(csv_path)
        source = f"{SOURCE_DESCRIPTION}, exported as {Path(csv_path).name}"
    else:
        print(f"Querying {SOURCE_DESCRIPTION} ...")
        rows = fetch_bigquery_rows(timeout=300)
        source = SOURCE_DESCRIPTION

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
        "source_table": source,
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
