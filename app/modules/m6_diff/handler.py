"""差异与批次入口。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from . import service

router = APIRouter(tags=["差异"])


@router.get("/api/differences")
def get_differences(request: Request):
    store = request.app.state.store
    return service.payload(store, store.latest_run(), request.app.state.settings)


@router.get("/api/batches")
def get_batches(request: Request):
    return service.batches_payload(request.app.state.store)


@router.get("/api/batches/{run_id}")
def get_batch(request: Request, run_id: str):
    return service.batch_detail(request.app.state.store, run_id, request.app.state.settings)


@router.get("/api/batches/{run_id}/results")
def get_batch_results(request: Request, run_id: str):
    store = request.app.state.store
    run = store.get_run(run_id)
    if not run:
        from ...errors import DomainError

        raise DomainError("E_NO_RUN", "批次不存在", 404)
    return {
        "run_id": run_id,
        "active": store.active_result(run_id),
        "results": store.results(run_id),
    }
