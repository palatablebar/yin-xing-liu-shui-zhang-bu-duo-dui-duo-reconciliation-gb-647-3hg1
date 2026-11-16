"""平台编排：采信 → 质量 → 候选 → 匹配 → 折算 → 结果快照。

模块之间只通过本文件衔接，模块内部不互相 import 规则函数；同一组输入得到同一 run_id，
重跑先删后插，行数与合计不变。折算结果按 result set 版本化：
- result_id 由 run_id + 规则版本 + 汇率摘要决定，同一输入同一汇率重跑命中同一版本；
- 调整汇率得到新版本，旧版本完整保留，active 指针原子切换；
- 失败整体回滚，旧版本仍是 active。
"""
from __future__ import annotations

import hashlib
import time
from decimal import Decimal

from .errors import DomainError
from .manifest import make_run_id, sha256_hex, utc_now_iso
from .modules.m1_ingest import service as m1_service
from .modules.m2_contract import service as m2_service
from .modules.m2_contract import currency as currency_mod
from .modules.m3_quality import quarantine as quarantine_mod
from .modules.m3_quality import rules as quality_rules
from .modules.m4_candidates import candidate as candidate_mod
from .modules.m5_matching import solve as solve_mod
from .modules.m6_diff import differences as differences_mod

DEFAULT_BANK = "bank_statement_dirty.csv"
DEFAULT_LEDGER = "ledger_book_dirty.csv"


def _parse(settings, side: str, filename: str, raw: bytes) -> tuple[dict, list[dict]]:
    aliases = m2_service.alias_index(side)
    parsed = m1_service.read_and_map(settings, side, filename, raw, aliases)
    records = []
    if not parsed["state"]:
        records = quality_rules.build_records(side, parsed["records"], settings)
    return parsed, records


def _side_meta(parsed: dict) -> dict:
    return {
        "file": parsed["filename"],
        "sha": parsed["sha256"] if "sha256" in parsed else "",
        "bytes": parsed["bytes"],
        "raw": parsed["raw_rows"],
        "state": parsed["state"] or "OK",
        "segments": parsed["segments"],
    }


def _counts(records: list[dict]) -> dict:
    bank = [record for record in records if record["side"] == "bank"]
    ledger = [record for record in records if record["side"] == "ledger"]
    return {
        "bank_raw": len(bank),
        "ledger_raw": len(ledger),
        "bank_valid": sum(1 for record in bank if record["status"] == "valid"),
        "ledger_valid": sum(1 for record in ledger if record["status"] == "valid"),
        "bank_quar": sum(1 for record in bank if record["status"] == "quarantine"),
        "ledger_quar": sum(1 for record in ledger if record["status"] == "quarantine"),
    }


def _run(settings, bank_meta: dict, ledger_meta: dict, records: list[dict],
         status: str, duration_ms: int = 0) -> dict:
    counts = _counts(records)
    params = settings.params()
    return {
        "run_id": make_run_id(bank_meta["sha"], ledger_meta["sha"],
                              settings.rule_version, params),
        "created_at": utc_now_iso(),
        "rule_version": settings.rule_version,
        "params": params,
        "bank_sha256": bank_meta["sha"],
        "ledger_sha256": ledger_meta["sha"],
        "bank_file": bank_meta["file"],
        "ledger_file": ledger_meta["file"],
        "bank_bytes": bank_meta["bytes"],
        "ledger_bytes": ledger_meta["bytes"],
        "bank_raw": counts["bank_raw"],
        "ledger_raw": counts["ledger_raw"],
        "bank_valid": counts["bank_valid"],
        "ledger_valid": counts["ledger_valid"],
        "bank_quar": counts["bank_quar"],
        "ledger_quar": counts["ledger_quar"],
        "segments": bank_meta["segments"] + ledger_meta["segments"],
        "status": status,
        "input_states": {"bank": bank_meta["state"], "ledger": ledger_meta["state"]},
        "duration_ms": duration_ms,
    }


def _result_id(run_id: str, rule_version: str, rates_sha: str) -> str:
    key = hashlib.sha256(f"{run_id}|{rule_version}|{rates_sha}".encode("utf-8")).hexdigest()
    return "res-" + key[:20]


def _rates_for(settings, store, run_id: str | None, overrides: dict | None) -> dict:
    """汇率优先级：显式覆盖 > 同批次当前 active 版本 > 默认表。

    这样普通重跑不会悄悄把已调整的汇率打回默认值。
    """
    base = dict(settings.default_rates)
    if run_id:
        active = store.active_result(run_id)
        if active and active.get("rates"):
            base.update({code: Decimal(str(value)) for code, value in active["rates"].items()})
    return currency_mod.normalize_rates(base, overrides, settings.base_currency)


def build_result(settings, run: dict, records: list[dict], candidates: list[dict],
                 matches: list[dict], rates: dict, duration_ms: int = 0) -> dict:
    reconciliation = differences_mod.reconcile(records, matches, settings, rates)
    rates_sha = currency_mod.rates_sha(rates)
    return {
        "result_id": _result_id(run["run_id"], run["rule_version"], rates_sha),
        "run_id": run["run_id"],
        "created_at": utc_now_iso(),
        "rule_version": run["rule_version"],
        "params": run.get("params", {}),
        "rates": {code: format(value, "f") for code, value in rates.items()},
        "rates_sha256": rates_sha,
        "base_currency": settings.base_currency,
        "totals": reconciliation["totals"],
        "counts": reconciliation["counts"],
        "identities": reconciliation["identities"],
        "currency_identities": reconciliation["currency_identities"],
        "currencies": reconciliation["currencies"],
        "differences": reconciliation["differences"],
        "duration_ms": duration_ms,
    }


def _summary(settings, store, run: dict, records: list[dict],
             candidates: list[dict], result: dict) -> dict:
    matches = store.matches(run["run_id"])
    return {
        "run_id": run["run_id"],
        "result_id": result["result_id"],
        "result_version": (store.get_result(result["result_id"]) or {}).get("version"),
        "rates": result["rates"],
        "base_currency": result["base_currency"],
        "status": run["status"],
        "created_at": run["created_at"],
        "counts": result["counts"],
        "totals": {key: format(value, "f") for key, value in result["totals"].items()},
        "candidates": len(candidates),
        "matches": len(matches),
        "quarantine_distribution": quarantine_mod.distribution(records),
        "input_states": run["input_states"],
        "bank_sha256": run["bank_sha256"],
        "ledger_sha256": run["ledger_sha256"],
    }


def _empty_meta() -> dict:
    return {"file": "", "sha": "", "bytes": 0, "raw": 0, "state": "", "segments": 0}


def _execute(settings, store, run: dict, records: list[dict],
             duration_ms: int = 0, rates: dict | None = None) -> dict:
    candidates = candidate_mod.build(records, settings)
    snapshot = rates or _rates_for(settings, store, run["run_id"], None)
    matches = solve_mod.solve(candidates, records, settings, snapshot)
    result = build_result(settings, run, records, candidates, matches, snapshot, duration_ms)
    store.save_run(run, records, candidates, matches, result)
    return _summary(settings, store, run, records, candidates, result)


def run_demo(settings, store, bank_name: str = DEFAULT_BANK,
             ledger_name: str = DEFAULT_LEDGER) -> dict:
    started = time.monotonic()
    bank_file, bank_raw = m1_service.resolve_sample(settings, bank_name)
    ledger_file, ledger_raw = m1_service.resolve_sample(settings, ledger_name)
    parsed_bank, bank_records = _parse(settings, "bank", bank_file, bank_raw)
    parsed_ledger, ledger_records = _parse(settings, "ledger", ledger_file, ledger_raw)
    parsed_bank["sha256"] = sha256_hex(bank_raw)
    parsed_ledger["sha256"] = sha256_hex(ledger_raw)
    records = bank_records + ledger_records
    duration = int((time.monotonic() - started) * 1000)
    run = _run(settings, _side_meta(parsed_bank), _side_meta(parsed_ledger),
               records, "matched", duration)
    return _execute(settings, store, run, records, duration)


def import_side(settings, store, side: str, filename: str, raw: bytes) -> dict:
    started = time.monotonic()
    parsed, records = _parse(settings, side, filename, raw)
    parsed_bytes = len(raw)
    meta = _side_meta(parsed)
    meta["bytes"] = parsed_bytes
    meta["sha"] = sha256_hex(raw)

    previous = store.latest_run()
    other = "ledger" if side == "bank" else "bank"
    other_records: list[dict] = []
    other_meta = _empty_meta()
    # 只要上一批次有另一侧，就沿用它的行；否则单独重导一侧会把已配对的一侧弄丢。
    if previous and previous.get(f"{other}_sha256"):
        other_records = store.records(previous["run_id"], side=other)
        other_meta = {
            "file": previous.get(f"{other}_file", ""),
            "sha": previous.get(f"{other}_sha256", ""),
            "bytes": previous.get(f"{other}_bytes", 0),
            "raw": previous.get(f"{other}_raw", 0),
            "state": previous.get("input_states", {}).get(other, "OK"),
            "segments": previous.get("segments", 0),
        }

    if side == "bank":
        bank_meta, ledger_meta = meta, other_meta
    else:
        bank_meta, ledger_meta = other_meta, meta
    combined = other_records + records
    both = bool(bank_meta["sha"] and ledger_meta["sha"])
    status = "matched" if both else f"partial_{side}"
    duration = int((time.monotonic() - started) * 1000)
    run = _run(settings, bank_meta, ledger_meta, combined, status, duration)
    summary = _execute(settings, store, run, combined, duration) if both else None
    if summary is None:
        store.save_run(run, combined, [], [])
        summary = {
            "run_id": run["run_id"],
            "result_id": None,
            "result_version": None,
            "rates": {},
            "base_currency": settings.base_currency,
            "status": run["status"],
            "created_at": run["created_at"],
            "counts": _summary_counts(store, run),
            "totals": {},
            "candidates": 0,
            "matches": 0,
            "quarantine_distribution": quarantine_mod.distribution(combined),
            "input_states": run["input_states"],
            "bank_sha256": run["bank_sha256"],
            "ledger_sha256": run["ledger_sha256"],
        }
    summary.update({
        "side": side,
        "state": parsed["state"],
        "encoding": parsed["encoding"],
        "bom": parsed["bom"],
        "segments": parsed["segments"],
        "raw_rows": parsed["raw_rows"],
        "valid": sum(1 for record in records if record["status"] == "valid"),
        "quarantine": sum(1 for record in records if record["status"] == "quarantine"),
    })
    return summary


def _summary_counts(store, run: dict) -> dict:
    records = store.records(run["run_id"])
    return {
        "bank_valid": sum(1 for r in records if r["side"] == "bank" and r["status"] == "valid"),
        "ledger_valid": sum(1 for r in records
                            if r["side"] == "ledger" and r["status"] == "valid"),
        "bank_quarantine": sum(1 for r in records
                               if r["side"] == "bank" and r["status"] == "quarantine"),
        "ledger_quarantine": sum(1 for r in records
                                 if r["side"] == "ledger" and r["status"] == "quarantine"),
    }


def recompute_with_rates(settings, store, run: dict | None, overrides: dict | None,
                         result_id: str | None = None) -> dict:
    """按指定汇率重跑求解与折算，或切回已有结果版本。

    - 不覆盖原币金额，记录行保持原样；
    - 同一组汇率得到同一 result_id，重跑只更新该版本，不新增重复版本；
    - 新汇率生成新版本，旧版本完整保留；写入在一个事务里完成；
    - 指定 result_id 时只做原子切换，不重算、不产生新版本。
    """
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    if run["status"] != "matched":
        raise DomainError("E_NOT_MATCHED", "批次还没有两侧数据，无法重算", 409)
    if result_id:
        target = store.get_result(result_id)
        if not target or target["run_id"] != run["run_id"]:
            raise DomainError("E_NO_RESULT", "结果版本不存在", 404)
        result = store.activate_result(result_id)
        return _recompute_payload(store, run, result, store.result_matches(result_id))

    started = time.monotonic()
    records = store.records(run["run_id"])
    snapshot = _rates_for(settings, store, run["run_id"], overrides)
    candidates = candidate_mod.build(records, settings)
    matches = solve_mod.solve(candidates, records, settings, snapshot)
    duration = int((time.monotonic() - started) * 1000)
    result = build_result(settings, run, records, candidates, matches, snapshot, duration)
    store.save_recompute(run["run_id"], candidates, result, matches)
    result = store.get_result(result["result_id"])
    return _recompute_payload(store, run, result, matches)


def _recompute_payload(store, run: dict, result: dict, matches: list[dict]) -> dict:
    return {
        "run_id": run["run_id"],
        "result_id": result["result_id"],
        "result_version": result["version"],
        "rates": result["rates"],
        "base_currency": result["base_currency"],
        "totals": result["totals"],
        "counts": result["counts"],
        "identities": result["identities"],
        "currency_identities": result["currency_identities"],
        "currencies": result["currencies"],
        "differences": result["differences"],
        "matches": matches,
        "results": store.results(run["run_id"]),
    }
