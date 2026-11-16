"""批次历史与运行清单。"""
from __future__ import annotations

from ...manifest import build_manifest


def batches(store, limit: int = 20) -> list[dict]:
    runs = store.list_runs(limit)
    out = []
    for run in runs:
        active = store.active_result(run["run_id"])
        out.append({
            "run_id": run["run_id"],
            "created_at": run["created_at"],
            "status": run["status"],
            "rule_version": run["rule_version"],
            "bank_valid": run["bank_valid"],
            "ledger_valid": run["ledger_valid"],
            "bank_quarantine": run["bank_quar"],
            "ledger_quarantine": run["ledger_quar"],
            "result_id": active["result_id"] if active else None,
            "result_version": active["version"] if active else None,
            "rates": active["rates"] if active else {},
            "manifest": build_manifest(run),
        })
    return out


def manifest(store, run: dict, totals: dict | None = None,
             result: dict | None = None) -> dict:
    payload = build_manifest(run)
    if totals:
        payload["totals"] = {key: format(value, "f") if hasattr(value, "as_tuple") else value
                             for key, value in totals.items()}
    if result:
        payload["result"] = {
            "result_id": result.get("result_id"),
            "version": result.get("version"),
            "status": result.get("status"),
            "created_at": result.get("created_at"),
            "rates": result.get("rates", {}),
            "rates_sha256": result.get("rates_sha256", ""),
            "base_currency": result.get("base_currency", "CNY"),
            "currencies": result.get("currencies", []),
        }
        payload["totals"] = {
            key: format(value, "f") if hasattr(value, "as_tuple") else value
            for key, value in (result.get("totals") or totals or {}).items()
        }
    return payload
