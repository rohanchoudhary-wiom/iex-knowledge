-- Export to data/input/outage_devices.csv.
-- Only active devices whose plan survives 12 hours beyond their last valid ping are retained.
-- ponytail: registry is latest-state until an event-time customer/address history source exists.
WITH csp_names AS (
  SELECT
    TRIM(CSP_ID) AS csp_id,
    MIN(TRIM(PARTNER_NAME)) AS csp_name
  FROM PROD_DB.DBT_CSP.DIM_CSP
  WHERE ETL_CURRENT
    AND NULLIF(TRIM(CSP_ID), '') IS NOT NULL
    AND NULLIF(TRIM(PARTNER_NAME), '') IS NOT NULL
  GROUP BY 1
  HAVING COUNT(DISTINCT TRIM(PARTNER_NAME)) = 1
),
customer_registry AS (
  SELECT
    UPPER(TRIM(c.DEVICE_ID)) AS device_id,
    TRIM(d.CSP_ID) AS registry_csp_id,
    n.csp_name,
    NULLIF(TRIM(c.NAME), '') AS customer_name,
    COALESCE(NULLIF(TRIM(c.ADDRESS), ''), NULLIF(TRIM(a.ADDRESS), '')) AS customer_address,
    c.MOBILE AS mobile,
    a.LAT AS latitude,
    a.LNG AS longitude,
    CASE
      WHEN a.LAT BETWEEN 6 AND 38 AND a.LNG BETWEEN 68 AND 98
      THEN H3_LATLNG_TO_CELL_STRING(a.LAT, a.LNG, 9)
    END AS h3_id,
    CONVERT_TIMEZONE('Asia/Kolkata', c.PLAN_EXPIRY_TIME)::TIMESTAMP_NTZ AS plan_expiry_ist,
    TRUE AS is_active
  FROM PROD_DB.DYNAMODB_READ.CUSTOMER_V_2 c
  LEFT JOIN PROD_DB.PUBLIC.T_ADDRESS a
    ON a.ID = c.GOOGLE_ADDRESS_ID
    AND NOT COALESCE(a._FIVETRAN_DELETED, FALSE)
  LEFT JOIN PROD_DB.PUBLIC.T_DEVICE d
    ON UPPER(TRIM(d.DEVICE_ID)) = UPPER(TRIM(c.DEVICE_ID))
    AND NOT COALESCE(d._FIVETRAN_DELETED, FALSE)
  LEFT JOIN csp_names n
    ON n.csp_id = TRIM(d.CSP_ID)
  WHERE NOT COALESCE(c._FIVETRAN_DELETED, FALSE)
    AND NULLIF(TRIM(c.DEVICE_ID), '') IS NOT NULL
    AND UPPER(TRIM(c.STATUS)) = 'ACTIVE'
    AND c.PLAN_EXPIRY_TIME IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY UPPER(TRIM(c.DEVICE_ID))
    ORDER BY c.UPDATED_AT DESC NULLS LAST, c.MODIFIED_TIME DESC NULLS LAST,
             c._FIVETRAN_SYNCED DESC NULLS LAST, c.GOOGLE_ADDRESS_ID DESC NULLS LAST
  ) = 1
),
ping_bounds AS (
  SELECT
    DATE_TRUNC(
      HOUR,
      DATEADD(DAY, -15, CONVERT_TIMEZONE('Asia/Kolkata', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
    ) AS ping_start_hour_ist,
    DATEADD(
      HOUR, 1,
      DATE_TRUNC(HOUR, CONVERT_TIMEZONE('Asia/Kolkata', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ)
    ) AS ping_end_hour_ist
),
ping_normalized AS (
  SELECT
    UPPER(TRIM(p.DEVICE_ID)) AS device_id,
    DATEDIFF(HOUR, b.ping_start_hour_ist, p.HOUR_START_IST) AS hour_offset,
    CASE
      WHEN p.HOUR_END_IST = DATEADD(HOUR, 1, p.HOUR_START_IST)
        THEN DATEADD(HOUR, 1, p.HOUR_START_IST)
      WHEN DATE_PART(HOUR, p.HOUR_START_IST) = 23
       AND p.HOUR_END_IST = DATE_TRUNC(DAY, p.HOUR_START_IST)
        THEN DATEADD(HOUR, 1, p.HOUR_START_IST)
    END AS effective_end_ist,
    p.TOTAL_PINGS_RECEIVED,
    p.FIRST_PING_TS_IST,
    p.LAST_PING_TS_IST,
    HASH(
      p.TOTAL_PINGS_RECEIVED, p.TOTAL_PINGS_MISSED,
      p.FIRST_PING_TS_IST, p.LAST_PING_TS_IST, p.PING_BITMAP
    ) AS value_hash
  FROM PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX p
  JOIN customer_registry r
    ON r.device_id = UPPER(TRIM(p.DEVICE_ID))
  CROSS JOIN ping_bounds b
  WHERE p.HOUR_START_IST >= b.ping_start_hour_ist
    AND p.HOUR_START_IST < b.ping_end_hour_ist
    AND p.INSERTED_AT IS NOT NULL
),
ping_hour_quality AS (
  SELECT
    device_id,
    hour_offset,
    COUNT(DISTINCT IFF(effective_end_ist IS NOT NULL, value_hash, NULL)) AS value_variants,
    COUNT_IF(
      effective_end_ist IS NULL
      OR TOTAL_PINGS_RECEIVED IS NULL
      OR TOTAL_PINGS_RECEIVED < 0
      OR (TOTAL_PINGS_RECEIVED = 0 AND (FIRST_PING_TS_IST IS NOT NULL OR LAST_PING_TS_IST IS NOT NULL))
      OR (
        TOTAL_PINGS_RECEIVED > 0
        AND (
          FIRST_PING_TS_IST IS NULL OR LAST_PING_TS_IST IS NULL
          OR FIRST_PING_TS_IST < DATEADD(HOUR, hour_offset, b.ping_start_hour_ist)
          OR FIRST_PING_TS_IST >= effective_end_ist
          OR LAST_PING_TS_IST < FIRST_PING_TS_IST
          OR LAST_PING_TS_IST >= effective_end_ist
        )
      )
    ) AS invalid_rows,
    MAX(TOTAL_PINGS_RECEIVED) AS total_pings_received,
    MAX(LAST_PING_TS_IST) AS last_successful_ping_ist
  FROM ping_normalized
  CROSS JOIN ping_bounds b
  GROUP BY device_id, hour_offset
),
ping_successes AS (
  SELECT device_id, hour_offset, last_successful_ping_ist
  FROM ping_hour_quality
  WHERE value_variants = 1
    AND invalid_rows = 0
    AND total_pings_received > 0
),
last_success_by_device AS (
  SELECT device_id, MAX(last_successful_ping_ist) AS last_successful_ping_ist
  FROM ping_successes
  GROUP BY device_id
),
ping_words AS (
  SELECT
    device_id,
    BITOR_AGG(IFF(FLOOR(hour_offset / 60) = 0, BITSHIFTLEFT(1, MOD(hour_offset, 60)), 0)) AS ping_bits_0,
    BITOR_AGG(IFF(FLOOR(hour_offset / 60) = 1, BITSHIFTLEFT(1, MOD(hour_offset, 60)), 0)) AS ping_bits_1,
    BITOR_AGG(IFF(FLOOR(hour_offset / 60) = 2, BITSHIFTLEFT(1, MOD(hour_offset, 60)), 0)) AS ping_bits_2,
    BITOR_AGG(IFF(FLOOR(hour_offset / 60) = 3, BITSHIFTLEFT(1, MOD(hour_offset, 60)), 0)) AS ping_bits_3,
    BITOR_AGG(IFF(FLOOR(hour_offset / 60) = 4, BITSHIFTLEFT(1, MOD(hour_offset, 60)), 0)) AS ping_bits_4,
    BITOR_AGG(IFF(FLOOR(hour_offset / 60) = 5, BITSHIFTLEFT(1, MOD(hour_offset, 60)), 0)) AS ping_bits_5,
    BITOR_AGG(IFF(FLOOR(hour_offset / 60) = 6, BITSHIFTLEFT(1, MOD(hour_offset, 60)), 0)) AS ping_bits_6
  FROM ping_successes
  GROUP BY device_id
),
registry AS (
  SELECT r.*, s.last_successful_ping_ist
  FROM customer_registry r
  JOIN last_success_by_device s USING (device_id)
  -- ponytail: no valid success in the 15-day snapshot means ineligible.
  WHERE r.plan_expiry_ist > DATEADD(HOUR, 12, s.last_successful_ping_ist)
),
outage_members AS (
  SELECT
    o.OUTAGE_ID AS outage_id,
    UPPER(TRIM(o.DEVICE_ID)) AS device_id,
    TRIM(o.CSP_ID) AS outage_csp_id,
    o.MEMBER_FIRST_FAIL_AT_IST AS member_first_fail_at_ist,
    v.OPENED_AT_IST AS outage_trigger_time
  FROM PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.OUTAGE_MEMBER_V3 o
  JOIN PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.OUTAGE_V3 v
    ON v.OUTAGE_ID = o.OUTAGE_ID
    AND NOT COALESCE(v._FIVETRAN_DELETED, FALSE)
  WHERE NOT COALESCE(o._FIVETRAN_DELETED, FALSE)
    AND o.MEMBER_FIRST_FAIL_AT_IST >= DATEADD(
      DAY, -15, CONVERT_TIMEZONE('Asia/Kolkata', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
    )
    AND o.MEMBER_FIRST_FAIL_AT_IST <
      CONVERT_TIMEZONE('Asia/Kolkata', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
  QUALIFY COUNT(DISTINCT TRIM(o.CSP_ID)) OVER (PARTITION BY o.OUTAGE_ID) = 1
),
mapped_outage_members AS (
  SELECT
    o.outage_id,
    o.device_id,
    r.registry_csp_id AS csp_id,
    r.csp_name,
    o.member_first_fail_at_ist,
    o.outage_trigger_time
  FROM outage_members o
  JOIN registry r
    ON r.device_id = o.device_id
  WHERE r.registry_csp_id IS NOT NULL
  QUALIFY COUNT(DISTINCT r.registry_csp_id) OVER (PARTITION BY o.outage_id) = 1
),
export_rows AS (
  SELECT
    r.device_id,
    r.registry_csp_id AS csp_id,
    r.csp_name,
    r.customer_name,
    r.customer_address,
    r.mobile,
    r.latitude,
    r.longitude,
    r.h3_id,
    o.outage_id,
    o.member_first_fail_at_ist,
    TRUE AS is_active,
    o.outage_trigger_time,
    r.plan_expiry_ist,
    r.last_successful_ping_ist,
    b.ping_start_hour_ist,
    DATEDIFF(HOUR, b.ping_start_hour_ist, b.ping_end_hour_ist) AS ping_hour_count,
    COALESCE(p.ping_bits_0, 0) AS ping_bits_0,
    COALESCE(p.ping_bits_1, 0) AS ping_bits_1,
    COALESCE(p.ping_bits_2, 0) AS ping_bits_2,
    COALESCE(p.ping_bits_3, 0) AS ping_bits_3,
    COALESCE(p.ping_bits_4, 0) AS ping_bits_4,
    COALESCE(p.ping_bits_5, 0) AS ping_bits_5,
    COALESCE(p.ping_bits_6, 0) AS ping_bits_6
  FROM registry r
  LEFT JOIN mapped_outage_members o
    ON o.device_id = r.device_id
  LEFT JOIN ping_words p
    ON p.device_id = r.device_id
  CROSS JOIN ping_bounds b
  WHERE r.registry_csp_id IS NOT NULL
)
SELECT DISTINCT
  device_id,
  csp_id,
  csp_name,
  customer_name,
  customer_address,
  mobile,
  latitude,
  longitude,
  h3_id,
  outage_id,
  member_first_fail_at_ist,
  is_active,
  outage_trigger_time,
  plan_expiry_ist,
  last_successful_ping_ist,
  ping_start_hour_ist,
  ping_hour_count,
  ping_bits_0,
  ping_bits_1,
  ping_bits_2,
  ping_bits_3,
  ping_bits_4,
  ping_bits_5,
  ping_bits_6
FROM export_rows
ORDER BY device_id, outage_id;
