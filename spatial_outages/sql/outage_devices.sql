-- Export to data/input/outage_devices.csv.
-- The fleet stays complete; only unambiguous outages from the last 15 days are attached.
WITH fleet AS (
  SELECT
    UPPER(TRIM(c.DEVICE_ID)) AS device_id,
    TRIM(d.CSP_ID) AS csp_id,
    c.MOBILE AS mobile,
    a.LAT AS latitude,
    a.LNG AS longitude,
    H3_LATLNG_TO_CELL_STRING(a.LAT, a.LNG, 9) AS h3_id
  FROM PROD_DB.DYNAMODB_READ.CUSTOMER_V_2 c
  JOIN PROD_DB.PUBLIC.T_ADDRESS a
    ON a.ID = c.GOOGLE_ADDRESS_ID
    AND NOT COALESCE(a._FIVETRAN_DELETED, FALSE)
  JOIN PROD_DB.PUBLIC.T_DEVICE d
    ON UPPER(TRIM(d.DEVICE_ID)) = UPPER(TRIM(c.DEVICE_ID))
    AND NOT COALESCE(d._FIVETRAN_DELETED, FALSE)
  WHERE NOT COALESCE(c._FIVETRAN_DELETED, FALSE)
    AND UPPER(TRIM(c.STATUS)) = 'ACTIVE'
    AND NULLIF(TRIM(c.DEVICE_ID), '') IS NOT NULL
    AND NULLIF(TRIM(d.CSP_ID), '') IS NOT NULL
    AND a.LAT BETWEEN 6 AND 38
    AND a.LNG BETWEEN 68 AND 98
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY UPPER(TRIM(c.DEVICE_ID))
    ORDER BY c.UPDATED_AT DESC NULLS LAST, c.MODIFIED_TIME DESC NULLS LAST,
             c._FIVETRAN_SYNCED DESC NULLS LAST, c.GOOGLE_ADDRESS_ID DESC NULLS LAST
  ) = 1
),
outage_members AS (
  SELECT
    o.OUTAGE_ID AS outage_id,
    UPPER(TRIM(o.DEVICE_ID)) AS device_id,
    o.MEMBER_FIRST_FAIL_AT_IST AS member_first_fail_at_ist
  FROM PROD_DB.BUSINESS_EFFICIENCY_ROUTER_OUTAGE_DETECTION_PUBLIC.OUTAGE_MEMBER_V3 o
  JOIN fleet f
    ON f.device_id = UPPER(TRIM(o.DEVICE_ID))
  WHERE NOT COALESCE(o._FIVETRAN_DELETED, FALSE)
    AND o.MEMBER_FIRST_FAIL_AT_IST >= DATEADD(
      DAY, -15, CONVERT_TIMEZONE('Asia/Kolkata', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
    )
    AND o.MEMBER_FIRST_FAIL_AT_IST <
      CONVERT_TIMEZONE('Asia/Kolkata', CURRENT_TIMESTAMP())::TIMESTAMP_NTZ
  QUALIFY COUNT(DISTINCT f.csp_id) OVER (PARTITION BY o.OUTAGE_ID) = 1
)
SELECT DISTINCT
  f.device_id,
  f.csp_id,
  f.mobile,
  f.latitude,
  f.longitude,
  f.h3_id,
  o.outage_id,
  o.member_first_fail_at_ist
FROM fleet f
LEFT JOIN outage_members o
  ON o.device_id = f.device_id
ORDER BY device_id, outage_id;
