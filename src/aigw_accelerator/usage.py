"""Daily and month-to-date Unity AI Gateway spend estimates.

Values are derived from documented system tables and must not be treated as
native near-real-time budget counters or absolute enforcement signals.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

SPEND_ESTIMATES_SQL = """
WITH hosted_usage AS (
  SELECT
    u.identity_metadata.run_by AS principal,
    u.usage_date AS usage_date,
    MAX(u.usage_end_time) AS latest_event_at,
    SUM(u.usage_quantity * lp.pricing.effective_list.default) AS spend_usd
  FROM system.billing.usage AS u
  JOIN system.billing.list_prices AS lp
    ON u.sku_name = lp.sku_name
   AND u.cloud = lp.cloud
   AND u.usage_unit = lp.usage_unit
   AND u.usage_end_time >= lp.price_start_time
   AND (lp.price_end_time IS NULL OR u.usage_end_time < lp.price_end_time)
  WHERE u.billing_origin_product = 'MODEL_SERVING'
    AND u.usage_metadata.ai_gateway.endpoint_name IS NOT NULL
    AND u.identity_metadata.run_by IS NOT NULL
    AND u.usage_unit = 'DBU'
    AND lp.currency_code = 'USD'
    AND u.usage_date >= date_trunc('MONTH', current_date())
  GROUP BY u.identity_metadata.run_by, u.usage_date
),
external_usage AS (
  SELECT
    identity_metadata.run_by AS principal,
    usage_date,
    MAX(usage_end_time) AS latest_event_at,
    SUM(usage_quantity) AS spend_usd
  FROM system.ai_gateway.external_model_spend
  WHERE identity_metadata.run_by IS NOT NULL
    AND usage_date >= date_trunc('MONTH', current_date())
  GROUP BY identity_metadata.run_by, usage_date
),
all_usage AS (
  SELECT principal, usage_date, latest_event_at, spend_usd FROM hosted_usage
  UNION ALL
  SELECT principal, usage_date, latest_event_at, spend_usd FROM external_usage
)
SELECT
  principal,
  SUM(CASE WHEN usage_date = current_date() THEN spend_usd ELSE 0 END) AS daily_spend_usd,
  SUM(spend_usd) AS month_to_date_spend_usd,
  MAX(latest_event_at) AS latest_event_at
FROM all_usage
GROUP BY principal
ORDER BY month_to_date_spend_usd DESC
LIMIT 2000
""".strip()


def spend_estimates_by_principal(warehouse_id: str | None = None) -> dict[str, Any]:
    """Return daily and month-to-date estimated spend by requesting principal.

    Databricks-hosted models use DBU list-price conversion from
    ``system.billing.usage``. External provider models use estimated USD from
    ``system.ai_gateway.external_model_spend``. Source cadences differ; the
    returned values are descriptive estimates, not enforcement counters.
    """
    warehouse = (warehouse_id or os.environ.get("DATABRICKS_WAREHOUSE_ID", "")).strip()
    if not warehouse or not all(ch.isalnum() or ch in "-_" for ch in warehouse):
        raise RuntimeError("DATABRICKS_WAREHOUSE_ID must be configured to query spend telemetry")

    from databricks import sql
    from databricks.sdk.core import Config

    config = Config()
    connection = sql.connect(
        server_hostname=config.host,
        http_path=f"/sql/1.0/warehouses/{warehouse}",
        credentials_provider=lambda: config.authenticate,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(SPEND_ESTIMATES_SQL)
            columns = [column[0] for column in cursor.description or []]
            raw_rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]
    finally:
        connection.close()

    rows = [
        {
            "principal": row.get("principal"),
            "daily_spend_usd": float(row.get("daily_spend_usd") or 0),
            "month_to_date_spend_usd": float(row.get("month_to_date_spend_usd") or 0),
            "latest_event_at": row.get("latest_event_at"),
        }
        for row in raw_rows
    ]
    return {
        "periods": {"daily": "today_utc", "monthly": "month_to_date"},
        "currency": "USD",
        "data_sources": [
            "system.billing.usage + system.billing.list_prices (Databricks-hosted models)",
            "system.ai_gateway.external_model_spend (external model estimates)",
        ],
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "is_enforcement_signal": False,
        "totals": {
            "daily_spend_usd": sum(row["daily_spend_usd"] for row in rows),
            "month_to_date_spend_usd": sum(row["month_to_date_spend_usd"] for row in rows),
        },
        "rows": rows,
    }
