"""匹配服务：求解、落库、查询。

对外只暴露当前 active 结果集的匹配组，确保页面、CSV 与清单共用同一批结果。
"""
from __future__ import annotations

from decimal import Decimal

from ...errors import DomainError
from ...pipeline import build_result, recompute_with_rates
from . import solve


def payload(store, run: dict | None) -> dict:
    if not run:
        return {"run_id": None, "result_id": None, "count": 0, "tiers": {}, "groups": [],
                "fee_alloc_total": "0.00", "fee_actual_total": "0.00",
                "fee_alloc_native": {}, "fee_actual_native": {}, "rates": {}}
    result = store.active_result(run["run_id"])
    groups = store.matches(run["run_id"])
    tiers: dict[str, int] = {}
    for group in groups:
        key = str(group["tier"])
        tiers[key] = tiers.get(key, 0) + 1
    # 人民币是关账单位，只有它可以在币种之间相加；原币按币种分列，避免 5 USD + 5 HKD。
    fee_alloc = sum((Decimal(group["fee_alloc"]) for group in groups), Decimal("0.00"))
    fee_actual_base = sum((Decimal(group.get("fee_actual_base", "0")) for group in groups),
                          Decimal("0.00"))
    native_alloc: dict[str, Decimal] = {}
    native_actual: dict[str, Decimal] = {}
    for group in groups:
        code = group.get("currency") or "CNY"
        native_actual[code] = native_actual.get(code, Decimal("0.00")) + Decimal(
            group.get("fee_actual", "0")
        )
        native_alloc[code] = native_alloc.get(code, Decimal("0.00")) + Decimal(
            group.get("fee_alloc", "0")
        )
    return {
        "run_id": run["run_id"],
        "result_id": result["result_id"] if result else None,
        "result_version": result["version"] if result else None,
        "rates": result["rates"] if result else {},
        "base_currency": result["base_currency"] if result else "CNY",
        "count": len(groups),
        "tiers": tiers,
        "groups": groups,
        "fee_alloc_total": format(fee_alloc, "f"),
        "fee_actual_total": format(fee_actual_base, "f"),
        "fee_alloc_native": {code: format(value, "f")
                             for code, value in sorted(native_alloc.items())},
        "fee_actual_native": {code: format(value, "f")
                              for code, value in sorted(native_actual.items())},
    }


def run_matching(store, run: dict | None, settings) -> dict:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    candidates = store.candidates(run["run_id"])
    if not candidates:
        raise DomainError("E_NO_CANDIDATES", "还没有候选", 409)
    records = store.records(run["run_id"])
    rates = _current_rates(store, run, settings)
    groups = solve.solve(candidates, records, settings, rates)
    result = build_result(settings, run, records, candidates, groups, rates)
    store.save_recompute(run["run_id"], candidates, result, groups)
    store.set_status(run["run_id"], "matched")
    return payload(store, store.get_run(run["run_id"]))


def _current_rates(store, run: dict, settings) -> dict:
    from ..m2_contract import currency

    base = dict(settings.default_rates)
    active = store.active_result(run["run_id"])
    if active and active.get("rates"):
        base.update({code: Decimal(str(value)) for code, value in active["rates"].items()})
    return currency.normalize_rates(base, None, settings.base_currency)


def apply_rates(store, run: dict | None, settings, overrides: dict,
                result_id: str | None = None) -> dict:
    return recompute_with_rates(settings, store, run, overrides, result_id)
