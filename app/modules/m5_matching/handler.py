"""匹配入口：求解、查看匹配组、按汇率重算与版本切换。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ...errors import DomainError
from . import service

router = APIRouter(prefix="/api/matches", tags=["匹配"])
rates_router = APIRouter(prefix="/api/rates", tags=["折算"])


class RatesIn(BaseModel):
    rates: dict[str, str] = {}
    result_id: str | None = None


@router.get("")
def get_matches(request: Request):
    store = request.app.state.store
    return service.payload(store, store.latest_run())


@router.post("")
def post_matches(request: Request):
    store = request.app.state.store
    return service.run_matching(store, store.latest_run(), request.app.state.settings)


@rates_router.get("")
def get_rates(request: Request):
    """当前批次的汇率、可用币种与结果版本列表。"""
    store = request.app.state.store
    settings = request.app.state.settings
    run = store.latest_run()
    active = store.active_result(run["run_id"]) if run else None
    return {
        "run_id": run["run_id"] if run else None,
        "base_currency": settings.base_currency,
        "default_rates": {
            code: format(value, "f") for code, value in sorted(settings.default_rates.items())
        },
        "active_result": _summary(active),
        "results": [_summary(item) for item in store.results(run["run_id"])] if run else [],
    }


@rates_router.post("")
def post_rates(request: Request, body: RatesIn):
    """按新汇率重跑；失败时旧结果完整保留，active 不切换。"""
    store = request.app.state.store
    settings = request.app.state.settings
    run = store.latest_run()
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    if not body.rates and not body.result_id:
        raise DomainError("E_RATES", "需要提供汇率或要切换的结果版本", 400)
    overrides: dict[str, str] = {}
    for code, value in body.rates.items():
        try:
            Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise DomainError("E_RATES", f"汇率不是数字：{code}={value!r}", 400) from exc
        overrides[code] = str(value)
    try:
        payload = service.apply_rates(store, run, settings, overrides, body.result_id)
    except ValueError as exc:
        # 汇率非正、币种未登记等由折算层抛出；统一映射成稳定错误码，不落到通用 400。
        raise DomainError("E_RATES", str(exc), 400) from exc
    payload["run_id"] = run["run_id"]
    return payload


def _summary(result: dict | None) -> dict | None:
    if not result:
        return None
    return {
        "result_id": result["result_id"],
        "version": result["version"],
        "created_at": result["created_at"],
        "status": result["status"],
        "active": result["active"],
        "rates": result["rates"],
        "rates_sha256": result["rates_sha256"],
        "totals": {key: format(value, "f") if isinstance(value, Decimal) else value
                   for key, value in result["totals"].items()},
        "counts": result["counts"],
    }
