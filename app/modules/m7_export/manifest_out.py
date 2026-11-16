"""运行清单导出：run_id、输入 sha256、规则版本、参数、汇率、计数与合计。

汇率与结果版本进入清单，复核人能确认这份导出用的是哪一版折算。
"""
from __future__ import annotations

import json
from decimal import Decimal

from ...manifest import build_manifest


def manifest_text(run: dict, reconciliation: dict | None = None) -> str:
    payload = build_manifest(run)
    if reconciliation:
        payload["totals"] = {
            key: format(value, "f") if isinstance(value, Decimal) else value
            for key, value in reconciliation["totals"].items()
        }
        payload["counts"].update(reconciliation["counts"])
        payload["result"] = {
            "result_id": reconciliation.get("result_id"),
            "version": reconciliation.get("result_version"),
            "base_currency": reconciliation.get("base_currency", "CNY"),
            "rates": reconciliation.get("rates", {}),
            "currencies": reconciliation.get("currencies", []),
            "currency_identities": reconciliation.get("currency_identities", []),
            "identities": reconciliation.get("identities", []),
        }
        payload["params"] = payload.get("params", {})
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
