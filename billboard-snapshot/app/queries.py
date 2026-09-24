"""
The BigQuery query behind the billboard data.

Shared by generate_snapshot.py (writes app/data/snapshot.json) and the live
BigQuery data source in app/data_source.py, so both always produce the same
columns. This module has no dependencies, so importing it is free.
"""

import os

PROJECT_ID = os.environ.get("BQ_PROJECT", "stickearn-bi")
OBT_TABLE = os.environ.get(
    "BQ_TABLE",
    "`stickearn-bi`.`cleanse_playlog`.`obt_survey_ads_billboard_playlog_taxonomy_rev`",
)

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

# One row per priced billboard, in every city. The column names here are the
# contract with the app: they must match the Billboard fields in
# app/data_source.py (REQUIRED_COLUMNS), because both snapshot.json and the
# live BigQuery source are read through the same loader.
SNAPSHOT_SQL = f"""
SELECT
  inventory_id,
  COALESCE(inventory_name, billboard_name, 'Unnamed')    AS inventory_name,
  COALESCE(inventory_address, billboard_address, '')     AS inventory_address,
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
  SAFE_CAST(cpm AS FLOAT64)                              AS cpm,
  cpm_calculated,
  monthly_impression_source,
  last_active_period
FROM {DEDUPED_INVENTORY}
WHERE monthly_price      > 0
  AND monthly_impression > 0
"""
