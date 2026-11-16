"""匹配求解：候选排序 → 不重叠挑选 → 组内合计与手续费分摊。

匹配只在同币种内发生，所有金额先在原币上算清；人民币值按本次结果快照的汇率逐行折算，
折算差异单独列出，不混入原币差额，也不为了让人民币相等而改动原币。

匹配差额（原币）= 银行原币签向匹配额 − 账簿原币签向匹配额；
折算尾差（人民币）= 逐行折算后的人民币差额 − 原币差额整笔折算；
人民币匹配差额 = 原币差额整笔折算 + 折算尾差。组、币种、批次三层都可加。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from ...config import dec
from ..m2_contract.currency import rate_for, to_base
from ..m4_candidates import window
from . import fees, subset


def _match_id(bank_ids: list[str], ledger_ids: list[str]) -> str:
    # 业务号在两侧各自唯一且只属一个币种，因此 ID 不必再掺币种；
    # 保持既有匹配组编号稳定，便于跨版本对照。
    key = hashlib.sha1(
        f"{','.join(bank_ids)}|{','.join(ledger_ids)}".encode("utf-8")
    ).hexdigest()[:10]
    return f"m-{key}"


def _index(records: list[dict], side: str) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for record in records:
        if record["side"] != side or record["status"] != "valid":
            continue
        table[record["txn_no"]] = {
            "txn_no": record["txn_no"],
            "amount": dec(record["amount"]),
            "fee": dec(record["fee"]),
            "direction": int(record["direction"]),
            "posted_at": window.parse_instant(record["posted_at"]),
            "currency": record.get("currency") or "CNY",
        }
    return table


def solve(candidates: list[dict], records: list[dict], settings,
          rates: dict | None = None) -> list[dict]:
    bank = _index(records, "bank")
    ledger = _index(records, "ledger")
    ordered = sorted(candidates, key=subset.priority_key)
    chosen = subset.choose_disjoint(ordered)
    quantum = settings.fee_quantum
    snapshot = rates or {
        code: Decimal(str(value)) for code, value in settings.default_rates.items()
    }

    groups: list[dict] = []
    for candidate in chosen:
        bank_rows = [bank[key] for key in candidate["bank_ids"] if key in bank]
        ledger_rows = [ledger[key] for key in candidate["ledger_ids"] if key in ledger]
        if len(bank_rows) != len(candidate["bank_ids"]) or len(ledger_rows) != len(candidate["ledger_ids"]):
            continue
        # 候选本身保证同币种；这里再校验一次，避免手工构造的候选跨币种混入。
        currency = bank_rows[0]["currency"]
        if any(row["currency"] != currency for row in bank_rows + ledger_rows):
            continue
        rate = rate_for(snapshot, currency)

        bank_sum = sum((row["amount"] for row in bank_rows), Decimal("0.00"))
        ledger_sum = sum((row["amount"] for row in ledger_rows), Decimal("0.00"))
        bank_signed = sum((row["amount"] * row["direction"] for row in bank_rows), Decimal("0.00"))
        ledger_signed = sum((row["amount"] * row["direction"] for row in ledger_rows), Decimal("0.00"))
        fee_actual = sum((row["fee"] for row in bank_rows), Decimal("0.00"))

        # 人民币一律逐行折算再汇总，与对账模块按行折算的口径完全一致；
        # 若把「组内合计」整笔折算，会与逐行相加差一分，导致两侧恒等式对不平。
        bank_sum_base = sum((to_base(row["amount"], rate, quantum) for row in bank_rows),
                            Decimal("0.00"))
        ledger_sum_base = sum((to_base(row["amount"], rate, quantum) for row in ledger_rows),
                              Decimal("0.00"))
        bank_signed_base = sum(
            (to_base(row["amount"] * row["direction"], rate, quantum) for row in bank_rows),
            Decimal("0.00"),
        )
        ledger_signed_base = sum(
            (to_base(row["amount"] * row["direction"], rate, quantum) for row in ledger_rows),
            Decimal("0.00"),
        )
        fee_actual_base = sum((to_base(row["fee"], rate, quantum) for row in bank_rows),
                              Decimal("0.00"))
        # 折算差异是唯一可能让「原币在容差内、人民币对不平」的来源，必须能逐层加总：
        # 组 → 币种 → 批次都按同一签向差额定义，尾差因此完全可加。
        native_gap = bank_signed - ledger_signed
        native_gap_base = to_base(native_gap, rate, quantum)
        base_gap = bank_signed_base - ledger_signed_base

        groups.append(
            {
                "match_id": _match_id(candidate["bank_ids"], candidate["ledger_ids"]),
                "tier": int(candidate["tier"]),
                "currency": currency,
                "rate": rate,
                "bank_ids": list(candidate["bank_ids"]),
                "ledger_ids": list(candidate["ledger_ids"]),
                "bank_sum": bank_sum,
                "ledger_sum": ledger_sum,
                "bank_signed": bank_signed,
                "ledger_signed": ledger_signed,
                "bank_sum_base": bank_sum_base,
                "ledger_sum_base": ledger_sum_base,
                "bank_signed_base": bank_signed_base,
                "ledger_signed_base": ledger_signed_base,
                "diff": abs(native_gap).quantize(quantum),
                "diff_base": abs(base_gap).quantize(quantum),
                "native_gap_base": native_gap_base,
                "fx_diff": base_gap - native_gap_base,
                "day_gap": int(candidate["day_gap"]),
                "fee_actual": fee_actual,
                "fee_actual_base": fee_actual_base,
            }
        )

    groups.sort(key=lambda group: (group["tier"], group["match_id"]))
    # 手续费分摊是关账动作：以人民币实际手续费合计为目标，按各组银行人民币签向金额
    # 绝对值做最大余额法分配。原币手续费 fee_actual 原样保留，作为折算与复核证据。
    allocated_total = sum((group["fee_actual_base"] for group in groups), Decimal("0.00"))
    allocations = fees.allocate(
        allocated_total,
        [abs(group["bank_signed_base"]) for group in groups],
        quantum,
        [group["match_id"] for group in groups],
    )
    for group, allocated in zip(groups, allocations):
        group["fee_alloc"] = allocated
        group["fee_alloc_base"] = allocated
    return groups
