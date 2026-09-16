"""
Run this LOCALLY (using your own `gcloud auth application-default login`)
to export a point-in-time snapshot of the billboard inventory.

The deployed app (app/main.py) never talks to BigQuery -- it only reads the
file this script writes: app/data/snapshot.json. Whenever you want fresher
numbers, re-run this script and redeploy.

Usage:
    gcloud auth application-default login     # once, if you haven't already
    pip install -r requirements-snapshot.txt
    python generate_snapshot.py
"""

import datetime
import json
import os
from pathlib import Path

from google.cloud import bigquery

PROJECT_ID = os.environ.get("BQ_PROJECT", "stickearn-bi")
OBT_TABLE = "`stickearn-bi`.`cleanse_playlog`.`obt_survey_ads_billboard_playlog_taxonomy_rev`"
OUT_PATH = Path(__file__).resolve().parent / "app" / "data" / "snapshot.json"

# Identical dedup logic to the live version: the OBT is one row per
# ads_id x year x month, so a physical billboard appears many times. This
# collapses to one row per inventory_id, keeping the most recent PRICED
# survey period -- a row that actually carries a price beats a newer empty
# one, so priced inventory is not silently dropped.
DEDUPED_INVENTORY = f"""(
  WITH inventory_activity AS (
    SELECT
      inventory_id,
      COUNT(DISTINCT ads_id) AS historical_ads_count,
      MAX(CONCAT(CAST(survey_year AS STRING), '-',
                 LPAD(CAST(survey_month AS STRING), 2, '0'))) AS last_active_period
    FROM {OBT_TABLE}
    WHERE inventory_id IS NOT NULL
    GROUP BY inventory_id
  ),
  latest_snapshot AS (
    SELECT
      inventory_id, inventory_name, inventory_address, inventory_image_url,
      SAFE_CAST(inventory_latitude  AS FLOAT64) AS inventory_latitude,
      SAFE_CAST(inventory_longitude AS FLOAT64) AS inventory_longitude,
      venue_type,
      billboard_name, billboard_address, billboard_image_url,
      SAFE_CAST(billboard_latitude  AS FLOAT64) AS billboard_latitude,
      SAFE_CAST(billboard_longitude AS FLOAT64) AS billboard_longitude,
      lighting_type, display_type_name,
      city_name, district_name, sub_district_name,
      SAFE_CAST(estimatedReach AS FLOAT64) AS estimated_reach_daily,
      SAFE_CAST(REGEXP_REPLACE(estimatedImpressionMonth, r'[^0-9.\\-]', '')
                AS FLOAT64) AS estimated_impression_month,
      cpm,
      SAFE_CAST(estimate_ads_price AS FLOAT64) AS estimate_ads_price,
      estimate_ads_price_source,
      survey_year, survey_month
    FROM {OBT_TABLE}
    WHERE inventory_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY inventory_id
      ORDER BY
        CASE WHEN SAFE_CAST(estimate_ads_price AS FLOAT64) > 0 THEN 0 ELSE 1 END,
        survey_year DESC, survey_month DESC
    ) = 1
  )
  SELECT
    ls.*,
    ia.historical_ads_count,
    ia.last_active_period,
    ls.estimate_ads_price AS monthly_price,
    COALESCE(NULLIF(ls.estimated_impression_month, 0),
             NULLIF(ls.estimated_reach_daily, 0) * 30) AS monthly_impression,
    CASE
      WHEN ls.estimated_impression_month > 0 THEN 'estimated_impression_month'
      WHEN ls.estimated_reach_daily      > 0 THEN 'estimated_reach_daily_x30'
    END AS monthly_impression_source,
    SAFE_DIVIDE(
      ls.estimate_ads_price,
      COALESCE(NULLIF(ls.estimated_impression_month, 0),
               NULLIF(ls.estimated_reach_daily, 0) * 30) / 1000
    ) AS cpm_calculated
  FROM latest_snapshot AS ls
  LEFT JOIN inventory_activity AS ia ON ia.inventory_id = ls.inventory_id
)"""

# Same shape as the live app's candidate_sql, minus the city filter --
# this pulls every priced candidate, in every city, in one shot.
SNAPSHOT_SQL = f"""
SELECT
  COALESCE(billboard_name, inventory_name, 'Unnamed')    AS billboard_name,
  COALESCE(inventory_name, '')                           AS inventory_name,
  COALESCE(billboard_address, inventory_address, '')     AS address,
  COALESCE(city_name, '')                                AS city_name,
  COALESCE(district_name, '')                            AS district_name,
  COALESCE(sub_district_name, '')                        AS sub_district_name,
  COALESCE(display_type_name, '')                        AS display_type_name,
  COALESCE(lighting_type, '')                            AS lighting_type,
  COALESCE(venue_type, '')                               AS venue_type,
  COALESCE(billboard_image_url, inventory_image_url, '') AS image_url,
  COALESCE(billboard_latitude,  inventory_latitude)      AS latitude,
  COALESCE(billboard_longitude, inventory_longitude)     AS longitude,
  monthly_price,
  monthly_impression,
  cpm,
  cpm_calculated,
  monthly_impression_source,
  last_active_period
FROM {DEDUPED_INVENTORY}
WHERE monthly_price      > 0
  AND monthly_impression > 0
"""


def main() -> None:
    client = bigquery.Client(project=PROJECT_ID)
    print(f"Querying {OBT_TABLE} ...")
    rows = [dict(r) for r in client.query(SNAPSHOT_SQL).result()]

    payload = {
        "generated_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source_table": OBT_TABLE,
        "row_count": len(rows),
        "rows": rows,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(f"Wrote {len(rows)} rows ({size_mb:.1f} MB) to {OUT_PATH}")
    print("Commit this file and redeploy to publish the new snapshot.")


if __name__ == "__main__":
    main()
