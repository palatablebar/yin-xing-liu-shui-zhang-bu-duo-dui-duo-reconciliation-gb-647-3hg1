"""差异服务：对账结论、批次历史、单批详情。

只从 active 结果快照读取，不在这里重算，保证页面、CSV、清单三处数字一致。
"""
from __future__ import annotations

from ...errors import DomainError
from . import differences, history


def _with_meta(result: dict, run: dict) -> dict:
    payload = dict(result)
    payload["run_id"] = run["run_id"]
    payload["result_id"] = result.get("result_id")
    payload["result_version"] = result.get("version")
    payload["rates"] = result.get("rates", {})
    payload["base_currency"] = result.get("base_currency", "CNY")
    payload["tier_names"] = differences.TIER_NAMES
    return payload


def _normalize_fresh(fresh: dict) -> dict:
    """即时计算的兼容结果统一转成字符串金额，避免 JSON 层把 Decimal 变 float。"""
    def text(value):
        if hasattr(value, "as_tuple"):
            return format(value, "f")
        return value

    fresh["totals"] = {key: text(value) for key, value in fresh.get("totals", {}).items()}
    fresh["currencies"] = [
        {key: text(value) for key, value in item.items()}
        for item in fresh.get("currencies", [])
    ]
    fresh["currency_identities"] = [
        {key: text(value) for key, value in item.items()}
        for item in fresh.get("currency_identities", [])
    ]
    fresh["identities"] = [
        {key: text(value) for key, value in item.items()}
        for item in fresh.get("identities", [])
    ]
    fresh["differences"] = [
        {key: text(value) for key, value in item.items()}
        for item in fresh.get("differences", [])
    ]
    return fresh


def payload(store, run: dict | None, settings) -> dict:
    if not run:
        return {
            "run_id": None,
            "result_id": None,
            "result_version": None,
            "totals": {},
            "counts": {},
            "identities": [],
            "currency_identities": [],
            "currencies": [],
            "differences": [],
            "rates": {},
            "tier_names": differences.TIER_NAMES,
        }
    result = store.active_result(run["run_id"])
    if result:
        return _with_meta(result, run)
    # 旧批次没有结果快照时按当前默认汇率即时计算，只用于兼容读取。
    from ...pipeline import build_result
    from ...modules.m2_contract import currency as currency_mod

    records = store.records(run["run_id"])
    matches = store.matches(run["run_id"])
    rates = currency_mod.normalize_rates(settings.default_rates, None, settings.base_currency)
    fresh = build_result(settings, run, records, store.candidates(run["run_id"]),
                         matches, rates)
    fresh["result_id"] = None
    fresh["version"] = None
    return _with_meta(_normalize_fresh(fresh), run)


def batches_payload(store) -> dict:
    return {"count": len(store.list_runs(20)), "batches": history.batches(store)}


def batch_detail(store, run_id: str, settings) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("E_NO_RUN", "批次不存在", 404)
    result = payload(store, run, settings)
    result["manifest"] = history.manifest(store, run, result.get("totals"))
    return result
