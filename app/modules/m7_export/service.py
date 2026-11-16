"""导出服务：报告 CSV 与运行清单 JSON。

导出只读 active 结果快照，与页面、/api/differences 使用同一 result_id。
"""
from __future__ import annotations

from decimal import Decimal

from ...errors import DomainError
from ..m6_diff import service as diff_service
from . import csv_out, manifest_out


def _result(store, run: dict, settings) -> dict:
    return diff_service.payload(store, run, settings)


def _money(value) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return "" if value is None else str(value)


def report(store, run: dict | None, settings) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    result = _result(store, run, settings)
    return csv_out.report_text(store.records(run["run_id"]), store.matches(run["run_id"]),
                               result)


def manifest(store, run: dict | None, settings) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    result = _result(store, run, settings)
    return manifest_out.manifest_text(run, result)


def summary(store, run: dict | None, settings) -> dict:
    if not run:
        return {"run_id": None, "result_id": None, "result_version": None,
                "rates": {}, "base_currency": settings.base_currency,
                "counts": {}, "totals": {}}
    result = _result(store, run, settings)
    return {
        "run_id": run["run_id"],
        "result_id": result.get("result_id"),
        "result_version": result.get("result_version"),
        "rates": result.get("rates", {}),
        "base_currency": result.get("base_currency", "CNY"),
        "counts": result["counts"],
        "totals": {key: _money(value) for key, value in result["totals"].items()},
    }
