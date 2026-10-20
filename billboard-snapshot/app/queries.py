"""
The BigQuery query behind the billboard data.

Shared by generate_snapshot.py (writes app/data/snapshot.json) and the live
BigQuery data source in app/data_source.py, so both always produce the same
columns. This module has no dependencies, so importing it is free.

It reads the raw core tables (raw_core3_admin) directly. Part A rebuilds the
survey/ads table (one row per ads_id x year x month); part B is the snapshot
logic: one row per inventory_id, the latest PRICED survey period wins (a row
that carries a price beats a newer empty one, so priced inventory is not
silently dropped), plus monthly impressions, calculated CPM and size.

The output column names are the contract with the app: they must match the
Billboard fields in app/data_source.py, because both snapshot.json and the
live BigQuery source are read through the same loader.
"""

import os

PROJECT_ID = os.environ.get("BQ_PROJECT", "stickearn-bi")
DATASET = os.environ.get("BQ_DATASET", "stickearn-bi.raw_core3_admin")

# Shown as the snapshot's source in snapshot.json.
SOURCE_DESCRIPTION = f"`{DATASET}` (survey, pricing and inventory tables)"

SNAPSHOT_SQL = rf"""
-- ============================================================
-- PART A: source (was `select * from final`)
-- ============================================================

-- CTE 1: Validation gate. Only ads present in the daily fact table are valid
WITH valid_ads AS (
  SELECT DISTINCT
    svyAds_id,
    svyBillboard_id,
    scrInventory_id,
    scrScreen_uuid,

    -- Geography (pre-resolved)
    masCity_id,
    masCity_name,
    bdt.masDistrict_id,
    masDistrict_name,
    sd.name AS sub_district,
    masProvince_id,
    masProvince_name,

    -- Display type (pre-resolved)
    masDisplayType_id,
    masDisplayType_name,

    -- Brand / industry (pre-resolved)
    masIndustry_id,
    masIndustry_name,
    masSubIndustry_id,
    masSubIndustry_name,
    masSurveyBrand_id,
    masSurveyBrand_name,
    masSurveySubBrand_id,
    masSurveySubBrand_name,

    -- Inventory (pre-resolved)
    scrInventory_name
  FROM `{DATASET}.insightAdsBillboardDailyTaxonomy` AS bdt
  LEFT JOIN `{DATASET}.masSubDistrict` AS sd
    ON bdt.masDistrict_id = sd.masDistrict_id
  WHERE svyAds_id IS NOT NULL
),

-- CTE 2: Pricing & period enrichment (source of truth for price, cpm, period)
pricing_monthly AS (
  SELECT
    svyAds_id,
    scrInventory_id,
    scrInventory_name,

    year       AS survey_year,
    month      AS survey_month,
    month_name AS survey_month_name,
    period_key,
    svyAds_created_at,

    CAST(NULLIF(price, '')          AS FLOAT64) AS screen_rate_price,
    cpm,
    CAST(NULLIF(avg_price_ooh,  '') AS FLOAT64) AS district_avg_price_ooh,
    CAST(NULLIF(avg_price_dooh, '') AS FLOAT64) AS district_avg_price_dooh,
    estimatedImpressionMonth
  FROM `{DATASET}.insightSummaryAdsPricingMonthlyTaxonomy`
  WHERE svyAds_id IS NOT NULL
),

-- CTE 3: Fact hub. valid_ads wins for dimensions, pricing_monthly for price/period
fact_hub AS (
  SELECT
    va.svyAds_id,
    va.svyBillboard_id,
    va.scrInventory_id,
    va.scrScreen_uuid,
    va.scrInventory_name,

    pm.survey_year,
    pm.survey_month,
    pm.survey_month_name,
    pm.period_key,
    pm.svyAds_created_at,

    pm.screen_rate_price,
    pm.cpm,
    pm.district_avg_price_ooh,
    pm.district_avg_price_dooh,
    pm.estimatedImpressionMonth,

    va.masCity_id,
    va.masCity_name,
    va.masDistrict_id,
    va.masDistrict_name,
    va.sub_district,
    va.masProvince_id,
    va.masProvince_name,

    va.masDisplayType_id,
    va.masDisplayType_name,

    va.masIndustry_id,
    va.masIndustry_name,
    va.masSubIndustry_id,
    va.masSubIndustry_name,
    va.masSurveyBrand_id,
    va.masSurveyBrand_name,
    va.masSurveySubBrand_id,
    va.masSurveySubBrand_name
  FROM valid_ads       AS va
  JOIN pricing_monthly AS pm ON pm.svyAds_id = va.svyAds_id
),

-- CTE 4: Price estimation. Screen rate first, district average by display type as fallback
price_logic AS (
  SELECT
    svyAds_id AS ads_id,
    survey_year,
    survey_month,
    cpm,
    CASE
      WHEN (screen_rate_price IS NULL OR screen_rate_price = 0) THEN
        CASE
          WHEN masDisplayType_id = 16 THEN district_avg_price_ooh
          WHEN masDisplayType_id = 15 THEN district_avg_price_dooh
        END
      ELSE screen_rate_price
    END AS estimate_ads_price,
    CASE
      WHEN (screen_rate_price IS NULL OR screen_rate_price = 0) THEN 'avg_district'
      ELSE 'scr_screen_list'
    END AS estimate_ads_price_source
  FROM fact_hub
),

-- CTE 5: Inventory extras, including size
inventory_extras AS (
  SELECT
    inv.id      AS inventory_id,
    inv.address AS inventory_address,
    inv.sizeX   AS inventory_sizeX,
    inv.sizeY   AS inventory_sizeY,

    -- size: normalized label (3x4 = 4x3), area and class
    IF(ROUND(LEAST(SAFE_CAST(inv.sizeX AS FLOAT64), SAFE_CAST(inv.sizeY AS FLOAT64))) = 0,
       'Unknown',
       FORMAT('%d x %d',
         CAST(ROUND(LEAST(SAFE_CAST(inv.sizeX AS FLOAT64),    SAFE_CAST(inv.sizeY AS FLOAT64))) AS INT64),
         CAST(ROUND(GREATEST(SAFE_CAST(inv.sizeX AS FLOAT64), SAFE_CAST(inv.sizeY AS FLOAT64))) AS INT64)))
      AS inventory_size_label,
    ROUND(SAFE_CAST(inv.sizeX AS FLOAT64)) * ROUND(SAFE_CAST(inv.sizeY AS FLOAT64))
      AS inventory_area_m2,
    CASE
      WHEN ROUND(SAFE_CAST(inv.sizeX AS FLOAT64)) * ROUND(SAFE_CAST(inv.sizeY AS FLOAT64)) IS NULL
        OR ROUND(SAFE_CAST(inv.sizeX AS FLOAT64)) * ROUND(SAFE_CAST(inv.sizeY AS FLOAT64)) = 0 THEN 'Unknown'
      WHEN ROUND(SAFE_CAST(inv.sizeX AS FLOAT64)) * ROUND(SAFE_CAST(inv.sizeY AS FLOAT64)) <  15  THEN '<15m2'
      WHEN ROUND(SAFE_CAST(inv.sizeX AS FLOAT64)) * ROUND(SAFE_CAST(inv.sizeY AS FLOAT64)) <  30  THEN '15-30m2'
      WHEN ROUND(SAFE_CAST(inv.sizeX AS FLOAT64)) * ROUND(SAFE_CAST(inv.sizeY AS FLOAT64)) <  60  THEN '30-60m2'
      WHEN ROUND(SAFE_CAST(inv.sizeX AS FLOAT64)) * ROUND(SAFE_CAST(inv.sizeY AS FLOAT64)) <  90  THEN '60-90m2'
      WHEN ROUND(SAFE_CAST(inv.sizeX AS FLOAT64)) * ROUND(SAFE_CAST(inv.sizeY AS FLOAT64)) <= 120 THEN '90-120m2'
      ELSE '>120m2'
    END AS inventory_size_class,

    inv.imageUrl            AS inventory_image_url,
    inv.latitude            AS inventory_latitude,
    inv.longitude           AS inventory_longitude,
    inv.elevation           AS inventory_elevation,
    inv.latitudeFacingFrom  AS inventory_latitudeFacingFrom,
    inv.longitudeFacingFrom AS inventory_longitudeFacingFrom
  FROM `{DATASET}.scrInventory` AS inv
  WHERE inv.deleted_at IS NULL
),

-- CTE 6: Screen extras (impression / reach, venue type)
screen_extras AS (
  SELECT
    scr.scrinventory_id AS inventory_id,
    scr.id              AS screen_id,
    scr.uuid            AS screen_uuid,
    scr.estimatedImpression,
    scr.estimatedReach,
    mvt.name            AS venue_type
  FROM `{DATASET}.scrScreen` AS scr
  LEFT JOIN `{DATASET}.masVenueType` AS mvt
    ON mvt.id = scr.masVenueType_id
),

-- CTE 7: Billboard extras
billboard_extras AS (
  SELECT
    bb.id                 AS billboard_id,
    bb.scrInventory_id,
    bb.name               AS billboard_name,
    bb.imageUrl           AS billboard_image_url,
    bb.latitude           AS billboard_latitude,
    bb.longitude          AS billboard_longitude,
    bb.description        AS billboard_description,
    bb.stringOwner        AS billboard_owner,
    bb.address            AS billboard_address,
    mdpt.name             AS lighting_type,

    dc.id                 AS display_category_id,
    dc.name               AS display_category_name,

    city.geojson          AS city_geojson,
    city.area             AS city_area,
    city.alternativeNames AS city_alternative_names,
    prov.alternativeNames AS province_alternative_names,
    dist.alternativeNames AS district_alternative_names
  FROM `{DATASET}.svyBillboard` AS bb
  LEFT JOIN `{DATASET}.masDisplayCategory` AS dc
    ON dc.id = bb.masDisplayCategory_id
  LEFT JOIN `{DATASET}.masCity` AS city
    ON city.id = bb.mascity_id
  LEFT JOIN `{DATASET}.masProvince` AS prov
    ON prov.id = city.masprovince_id
  LEFT JOIN `{DATASET}.masDistrict` AS dist
    ON dist.id = bb.masDistrict_id
  LEFT JOIN `{DATASET}.masDisplayLightingType` AS mdpt
    ON mdpt.id = bb.masDisplayLightingType_id
  WHERE bb.name NOT LIKE '%PRO99%'
),

-- CTE 8: Ads core (approved, not deleted)
ads_core AS (
  SELECT
    ads.id                         AS ads_id,
    ads.svySurvey_id               AS survey_id,
    ads.latitude                   AS ads_latitude,
    ads.longitude                  AS ads_longitude,
    ads.updated_at                 AS ads_updated_at,
    ads.deleted_at                 AS ads_deleted_at,
    ads.rejectedAt                 AS ads_rejected_at,
    ads.masSurveyCampaignStatus_id AS ads_survey_campaign_status,
    ads.statusReason               AS ads_status_reason,
    ads.stringSubBrand             AS ads_sub_brand,
    mss.name                       AS status,
    campaign_status.name           AS ads_survey_campaign_status_name,
    campaign_type.name             AS ads_campaign_type_name,

    svy.svyBillboard_id,
    svy.address                    AS survey_address,
    svy.latitude                   AS survey_latitude,
    svy.longitude                  AS survey_longitude,
    svy.created_at                 AS survey_created_at,
    svy.updated_at                 AS survey_updated_at,
    svy.deleted_at                 AS survey_deleted_at,
    svy.approved_at                AS survey_approved_at
  FROM `{DATASET}.svyAdsTaxonomy` AS ads
  JOIN `{DATASET}.svySurveyTaxonomy` AS svy
    ON svy.id = ads.svySurvey_id
  LEFT JOIN `{DATASET}.masSurveyCampaignStatus` AS campaign_status
    ON campaign_status.id = ads.masSurveyCampaignStatus_id
  LEFT JOIN `{DATASET}.masSurveyCampaignType` AS campaign_type
    ON campaign_type.id = ads.masSurveyCampaignType_id
  LEFT JOIN `{DATASET}.masSurveyStatus` AS mss
    ON ads.masSurveyStatus_id = mss.id
  WHERE ads.massurveystatus_id = 2
    AND ads.deleted_at IS NULL
),

-- CTE 9: Photos per ad
ads_photo AS (
  SELECT
    svyAds_id AS ads_id,
    COALESCE(
      MAX(CASE WHEN isDefaultPhoto = 0 THEN imageUrl END),
      MAX(CASE WHEN isDefaultPhoto = 1 THEN imageUrl END)
    ) AS ads_photo_url,
    MAX(CASE WHEN isDefaultPhoto = 1 THEN imageUrl END) AS ads_default_photo_url
  FROM `{DATASET}.svyPhotoTaxonomy`
  WHERE svyAds_id IS NOT NULL
  GROUP BY svyAds_id
),

-- FINAL: one row per ads_id x year x month (same shape as the old OBT table)
final AS (
  SELECT
    -- Inventory
    fh.scrInventory_id AS inventory_id,
    fh.scrInventory_name AS inventory_name,
    ie.inventory_address,
    ie.inventory_sizeX,
    ie.inventory_sizeY,
    ie.inventory_image_url,
    ie.inventory_latitude,
    ie.inventory_longitude,
    ie.inventory_elevation,
    ie.inventory_latitudeFacingFrom,
    ie.inventory_longitudeFacingFrom,
    ie.inventory_size_label,
    ie.inventory_area_m2,
    ie.inventory_size_class,
    -- Screen
    se.screen_id,
    se.screen_uuid,
    se.venue_type,
    fh.cpm,
    se.estimatedImpression,
    se.estimatedReach,
    fh.estimatedImpressionMonth,
    -- Billboard
    fh.svyBillboard_id AS billboard_id,
    be.billboard_name,
    be.billboard_address,
    be.billboard_image_url,
    be.billboard_latitude,
    be.billboard_longitude,
    be.billboard_description,
    be.billboard_owner,
    -- Display category / type
    be.display_category_id,
    be.display_category_name,
    be.lighting_type,
    fh.masDisplayType_id   AS display_type_id,
    fh.masDisplayType_name AS display_type_name,
    -- Geography
    fh.masCity_id   AS city_id,
    fh.masCity_name AS city_name,
    be.city_area,
    be.city_geojson,
    be.city_alternative_names,
    fh.masProvince_id   AS province_id,
    fh.masProvince_name AS province_name,
    be.province_alternative_names,
    fh.masDistrict_id   AS district_id,
    fh.masDistrict_name AS district_name,
    fh.sub_district     AS sub_district_name,
    fh.district_avg_price_ooh,
    fh.district_avg_price_dooh,
    be.district_alternative_names,
    -- Survey period
    ac.survey_id,
    fh.survey_year,
    fh.survey_month,
    fh.survey_month_name,
    fh.period_key,
    ac.survey_address,
    ac.survey_latitude,
    ac.survey_longitude,
    ac.survey_created_at,
    ac.survey_updated_at,
    ac.survey_deleted_at,
    ac.survey_approved_at,
    -- Ads
    fh.svyAds_id         AS ads_id,
    fh.svyAds_created_at AS ads_created_at,
    ac.status,
    ac.ads_latitude,
    ac.ads_longitude,
    ac.ads_updated_at,
    ac.ads_deleted_at,
    ac.ads_rejected_at,
    ac.ads_survey_campaign_status,
    ac.ads_survey_campaign_status_name,
    ac.ads_campaign_type_name,
    ac.ads_status_reason,
    ac.ads_sub_brand,
    ap.ads_photo_url,
    ap.ads_default_photo_url,
    -- Brand / industry
    fh.masSurveySubBrand_id   AS sub_brand_id,
    fh.masSurveySubBrand_name AS sub_brand_name,
    fh.masSurveyBrand_id      AS brand_id,
    fh.masSurveyBrand_name    AS brand_name,
    fh.masSubIndustry_id      AS sub_industry_id,
    fh.masSubIndustry_name    AS sub_industry_name,
    fh.masIndustry_id         AS industry_id,
    fh.masIndustry_name       AS industry_name,
    -- Price
    fh.screen_rate_price,
    pl.estimate_ads_price,
    pl.estimate_ads_price_source,
    -- Dedup key
    CASE
      WHEN fh.masDisplayType_name = 'OOH' THEN
        CONCAT(CAST(fh.scrInventory_id AS STRING), '_',
               CAST(fh.survey_month    AS STRING), '_',
               CAST(fh.survey_year     AS STRING))
      WHEN fh.masDisplayType_name = 'DOOH' THEN CAST(fh.svyAds_id AS STRING)
      ELSE NULL
    END AS total_ads_count_id
  FROM fact_hub         AS fh
  JOIN ads_core         AS ac ON ac.ads_id          = fh.svyAds_id
  JOIN inventory_extras AS ie ON ie.inventory_id    = fh.scrInventory_id
  JOIN screen_extras    AS se ON se.inventory_id    = fh.scrInventory_id
  JOIN billboard_extras AS be ON be.scrInventory_id = fh.scrInventory_id
  JOIN price_logic      AS pl ON pl.ads_id          = fh.svyAds_id
                             AND pl.survey_year     = fh.survey_year
                             AND pl.survey_month    = fh.survey_month
  LEFT JOIN ads_photo   AS ap ON ap.ads_id          = fh.svyAds_id
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY fh.svyAds_id, fh.survey_year, fh.survey_month
    ORDER BY pl.estimate_ads_price_source DESC
  ) = 1
),

-- ============================================================
-- PART B: snapshot logic (billboard-snapshot/app/queries.py)
-- ============================================================

-- How many ads each inventory has carried, and its latest survey period
inventory_activity AS (
  SELECT
    inventory_id,
    COUNT(DISTINCT ads_id) AS historical_ads_count,
    MAX(CONCAT(CAST(survey_year AS STRING), '-',
               LPAD(CAST(survey_month AS STRING), 2, '0'))) AS last_active_period
  FROM final
  WHERE inventory_id IS NOT NULL
  GROUP BY inventory_id
),

-- One row per inventory_id: a priced period beats a newer unpriced one,
-- then the most recent period wins
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
    SAFE_CAST(REGEXP_REPLACE(CAST(estimatedImpressionMonth AS STRING), r'[^0-9.\-]', '')
              AS FLOAT64) AS estimated_impression_month,
    cpm,
    SAFE_CAST(estimate_ads_price AS FLOAT64) AS estimate_ads_price,
    estimate_ads_price_source,
    survey_year, survey_month,
    -- NEW: size
    inventory_sizeX,
    inventory_sizeY,
    inventory_size_label,
    inventory_area_m2,
    inventory_size_class
  FROM final
  WHERE inventory_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY inventory_id
    ORDER BY
      CASE WHEN SAFE_CAST(estimate_ads_price AS FLOAT64) > 0 THEN 0 ELSE 1 END,
      survey_year DESC, survey_month DESC
  ) = 1
),

deduped_inventory AS (
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
)

-- One row per priced billboard. The first 18 columns are exactly what the
-- site reads today; the NEW columns are extra and are ignored until the
-- site is updated to use them.
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
  last_active_period,
  -- NEW: size
  SAFE_CAST(inventory_sizeX AS FLOAT64)                  AS size_x,
  SAFE_CAST(inventory_sizeY AS FLOAT64)                  AS size_y,
  COALESCE(inventory_size_label, 'Unknown')              AS size_label,
  inventory_area_m2                                      AS area_m2,
  COALESCE(inventory_size_class, 'Unknown')              AS size_class
FROM deduped_inventory
WHERE monthly_price      > 0
  AND monthly_impression > 0
"""
