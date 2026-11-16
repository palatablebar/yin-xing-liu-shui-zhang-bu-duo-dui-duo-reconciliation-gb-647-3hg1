"""对账：按币种核对原币，再按结果快照汇率折算人民币关账。

三层结论同时给出，互不掩盖：
1. 原币核对：同一币种内，银行合计 = 匹配 + 未匹配 − 手续费；账簿合计 = 匹配 + 未匹配。
   匹配差额只在原币容差内允许非 0，跨币种一律不配对。
2. 人民币关账：逐行折算后合计，银行合计(CNY) = 匹配(CNY) + 未匹配(CNY) − 手续费(CNY)。
3. 差异归因：人民币匹配差额 = Σ(原币差额 × 汇率) + Σ折算尾差，两项分开列出，
   复核人能看出「对不平」究竟来自业务尾差还是折算分位。
"""
from __future__ import annotations

from decimal import Decimal

from ... import reasons
from ...config import dec
from ..m2_contract.currency import rate_for, to_base
from ..m5_matching.subset import TIER_NAMES

ZERO = Decimal("0.00")


def _signed(record: dict) -> Decimal:
    return dec(record["amount"]) * int(record["direction"])


def _currency(record: dict) -> str:
    return record.get("currency") or "CNY"


def reconcile(records: list[dict], matches: list[dict], settings,
              rates: dict | None = None) -> dict:
    quantum = settings.fee_quantum
    snapshot = rates or {
        code: Decimal(str(value)) for code, value in settings.default_rates.items()
    }

    def base(amount: Decimal, currency: str) -> Decimal:
        return to_base(amount, rate_for(snapshot, currency), quantum)

    valid = [record for record in records if record["status"] == "valid"]
    bank = [record for record in valid if record["side"] == "bank"]
    ledger = [record for record in valid if record["side"] == "ledger"]

    matched_bank_ids: set[str] = set()
    matched_ledger_ids: set[str] = set()
    tier_counts: dict[str, int] = {}
    for group in matches:
        matched_bank_ids.update(group["bank_ids"])
        matched_ledger_ids.update(group["ledger_ids"])
        tier = str(group["tier"])
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    unmatched_bank = [record for record in bank if record["txn_no"] not in matched_bank_ids]
    unmatched_ledger = [record for record in ledger if record["txn_no"] not in matched_ledger_ids]

    # ---------- 按币种汇总原币与人民币 ----------
    codes = sorted({_currency(record) for record in valid}
                   | {group.get("currency") or "CNY" for group in matches})
    by_currency: dict[str, dict] = {}
    for code in codes:
        by_currency[code] = {
            "currency": code,
            "rate": str(rate_for(snapshot, code)),
            "bank_gross": ZERO,
            "bank_fee": ZERO,
            "bank_matched": ZERO,
            "bank_unmatched": ZERO,
            "ledger_total": ZERO,
            "ledger_matched": ZERO,
            "ledger_unmatched": ZERO,
            "match_gap": ZERO,
            "bank_gross_base": ZERO,
            "bank_fee_base": ZERO,
            "bank_matched_base": ZERO,
            "bank_unmatched_base": ZERO,
            "ledger_total_base": ZERO,
            "ledger_matched_base": ZERO,
            "ledger_unmatched_base": ZERO,
            "match_gap_base": ZERO,
            "match_gap_native_base": ZERO,
            "fx_diff": ZERO,
            "bank_valid": 0,
            "ledger_valid": 0,
        }

    for record in bank:
        item = by_currency[_currency(record)]
        item["bank_gross"] += _signed(record)
        item["bank_fee"] += dec(record["fee"])
        item["bank_gross_base"] += base(_signed(record), _currency(record))
        item["bank_fee_base"] += base(dec(record["fee"]), _currency(record))
        item["bank_valid"] += 1
    for record in ledger:
        item = by_currency[_currency(record)]
        item["ledger_total"] += _signed(record)
        item["ledger_total_base"] += base(_signed(record), _currency(record))
        item["ledger_valid"] += 1
    for record in unmatched_bank:
        item = by_currency[_currency(record)]
        item["bank_unmatched"] += _signed(record)
        item["bank_unmatched_base"] += base(_signed(record), _currency(record))
    for record in unmatched_ledger:
        item = by_currency[_currency(record)]
        item["ledger_unmatched"] += _signed(record)
        item["ledger_unmatched_base"] += base(_signed(record), _currency(record))

    # 匹配组的人民币金额来自组内快照值，确保与匹配页同一批结果。
    matched_bank_base = ZERO
    matched_ledger_base = ZERO
    fee_alloc_total = ZERO
    fee_actual_base_total = ZERO
    for group in matches:
        code = group.get("currency") or "CNY"
        item = by_currency.setdefault(code, {
            "currency": code, "rate": str(rate_for(snapshot, code)),
        })
        for key in ("bank_matched", "bank_unmatched", "ledger_matched", "ledger_unmatched",
                    "bank_gross", "bank_fee", "ledger_total", "match_gap", "fx_diff",
                    "bank_gross_base", "bank_fee_base", "bank_matched_base",
                    "bank_unmatched_base", "ledger_total_base", "ledger_matched_base",
                    "ledger_unmatched_base", "match_gap_base", "match_gap_native_base",
                    "bank_valid", "ledger_valid"):
            item.setdefault(key, ZERO if key not in ("bank_valid", "ledger_valid") else 0)
        bank_signed = dec(group.get("bank_signed", "0"))
        ledger_signed = dec(group.get("ledger_signed", "0"))
        bank_signed_base = dec(group.get("bank_signed_base", "0"))
        ledger_signed_base = dec(group.get("ledger_signed_base", "0"))
        item["bank_matched"] += bank_signed
        item["ledger_matched"] += ledger_signed
        item["bank_matched_base"] += bank_signed_base
        item["ledger_matched_base"] += ledger_signed_base
        item["match_gap"] += bank_signed - ledger_signed
        item["match_gap_base"] += bank_signed_base - ledger_signed_base
        item["match_gap_native_base"] += dec(group.get("native_gap_base", "0"))
        item["fx_diff"] += dec(group.get("fx_diff", "0"))
        matched_bank_base += bank_signed_base
        matched_ledger_base += ledger_signed_base
        fee_alloc_total += dec(group.get("fee_alloc", "0"))
        fee_actual_base_total += dec(group.get("fee_actual_base", "0"))

    # ---------- 人民币关账合计 ----------
    bank_gross_base = sum((item["bank_gross_base"] for item in by_currency.values()), ZERO)
    bank_fee_base = sum((item["bank_fee_base"] for item in by_currency.values()), ZERO)
    bank_total_base = bank_gross_base - bank_fee_base
    bank_unmatched_base = sum((item["bank_unmatched_base"] for item in by_currency.values()), ZERO)
    ledger_total_base = sum((item["ledger_total_base"] for item in by_currency.values()), ZERO)
    ledger_unmatched_base = sum((item["ledger_unmatched_base"] for item in by_currency.values()), ZERO)
    match_gap_base = matched_bank_base - matched_ledger_base

    # 原币差额整笔折算（按组汇总后再折）加上各组折算尾差，必须正好等于人民币差额。
    # fx_booked 取各组尾差之和而不是两者相减，这样恒等式才真正校验分层可加性。
    native_gap_base = sum(
        (item["match_gap_native_base"] for item in by_currency.values()), ZERO
    )
    fx_booked = sum((item["fx_diff"] for item in by_currency.values()), ZERO)

    bank_quarantine = sum(1 for record in records
                          if record["status"] == "quarantine" and record["side"] == "bank")
    ledger_quarantine = sum(1 for record in records
                            if record["status"] == "quarantine" and record["side"] == "ledger")

    identities = [
        {
            "key": "bank_amount",
            "label": "银行合计（人民币）",
            "unit": "元",
            "left": bank_total_base,
            "right": matched_bank_base + bank_unmatched_base - bank_fee_base,
            "balanced": bank_total_base == matched_bank_base + bank_unmatched_base - bank_fee_base,
        },
        {
            "key": "ledger_amount",
            "label": "账簿合计（人民币）",
            "unit": "元",
            "left": ledger_total_base,
            "right": matched_ledger_base + ledger_unmatched_base,
            "balanced": ledger_total_base == matched_ledger_base + ledger_unmatched_base,
        },
        {
            "key": "bank_count",
            "label": "银行笔数",
            "unit": "笔",
            "left": len(bank),
            "right": len(matched_bank_ids) + len(unmatched_bank),
            "balanced": len(bank) == len(matched_bank_ids) + len(unmatched_bank),
        },
        {
            "key": "ledger_count",
            "label": "账簿笔数",
            "unit": "笔",
            "left": len(ledger),
            "right": len(matched_ledger_ids) + len(unmatched_ledger),
            "balanced": len(ledger) == len(matched_ledger_ids) + len(unmatched_ledger),
        },
        {
            "key": "fee_alloc",
            "label": "手续费分摊（人民币）",
            "unit": "元",
            "left": fee_alloc_total,
            "right": fee_actual_base_total,
            "balanced": fee_alloc_total == fee_actual_base_total,
        },
        {
            "key": "match_gap",
            "label": "匹配差额（人民币）",
            "unit": "元",
            "left": match_gap_base,
            "right": native_gap_base + fx_booked,
            "balanced": match_gap_base == native_gap_base + fx_booked,
        },
    ]

    # 逐币种原币核对：跨币种不配对，所以每个币种都必须自己平。
    currency_identities = []
    for code, item in sorted(by_currency.items()):
        net = item["bank_matched"] + item["bank_unmatched"] - item["bank_fee"]
        ledger_net = item["ledger_matched"] + item["ledger_unmatched"]
        currency_identities.append({
            "key": f"currency:{code}",
            "currency": code,
            "label": f"{code} 原币核对",
            "unit": "原币",
            "rate": item["rate"],
            "bank_gross": item["bank_gross"],
            "bank_total": net,
            "bank_matched": item["bank_matched"],
            "bank_unmatched": item["bank_unmatched"],
            "bank_fee": item["bank_fee"],
            "ledger_total": ledger_net,
            "ledger_matched": item["ledger_matched"],
            "ledger_unmatched": item["ledger_unmatched"],
            "match_gap": item["match_gap"],
            "match_gap_base": item["match_gap_base"],
            "match_gap_native_base": item["match_gap_native_base"],
            "fx_diff": item["fx_diff"],
            "fx_diff_booked": item["fx_diff"],
            "balanced": item["bank_gross"] - item["bank_fee"] == net
            and item["ledger_total"] == ledger_net,
        })

    differences: list[dict] = []
    for record in sorted(unmatched_bank, key=lambda item: (item["txn_no"], item["side"])):
        differences.append({
            "kind": "未匹配",
            "side": "银行",
            "ref": record["txn_no"],
            "currency": _currency(record),
            "rate": str(rate_for(snapshot, _currency(record))),
            "amount": _signed(record),
            "amount_base": base(_signed(record), _currency(record)),
            "reason": "BANK_UNMATCHED",
            "note": record.get("memo", ""),
        })
    for record in sorted(unmatched_ledger, key=lambda item: (item["txn_no"], item["side"])):
        differences.append({
            "kind": "未匹配",
            "side": "账簿",
            "ref": record["txn_no"],
            "currency": _currency(record),
            "rate": str(rate_for(snapshot, _currency(record))),
            "amount": _signed(record),
            "amount_base": base(_signed(record), _currency(record)),
            "reason": "LEDGER_UNMATCHED",
            "note": record.get("memo", ""),
        })
    for code, item in sorted(by_currency.items()):
        if item["bank_fee"] == 0:
            continue
        differences.append({
            "kind": "手续费",
            "side": "银行",
            "ref": "FEE",
            "currency": code,
            "rate": item["rate"],
            "amount": -item["bank_fee"],
            "amount_base": -item["bank_fee_base"],
            "reason": "FEE_TOTAL",
            "note": "计入银行侧",
        })
    if match_gap_base != 0:
        differences.append({
            "kind": "容差",
            "side": "双方",
            "ref": "GAP",
            "currency": "CNY",
            "rate": "1",
            "amount": match_gap_base,
            "amount_base": match_gap_base,
            "reason": "TOLERANCE_GAP",
            "note": f"人民币匹配差额，原币容差 {settings.tolerance}",
        })
    if fx_booked != 0:
        differences.append({
            "kind": "折算",
            "side": "双方",
            "ref": "FX",
            "currency": "CNY",
            "rate": "1",
            "amount": fx_booked,
            "amount_base": fx_booked,
            "reason": "FX_DIFF",
            "note": "逐行折算与差额整笔折算的分位差",
        })

    totals = {
        # 以下均为人民币关账口径；原币明细见 currencies。
        "bank_gross": bank_gross_base,
        "bank_fee": bank_fee_base,
        "bank_total": bank_total_base,
        "bank_matched": matched_bank_base,
        "bank_unmatched": bank_unmatched_base,
        "ledger_total": ledger_total_base,
        "ledger_matched": matched_ledger_base,
        "ledger_unmatched": ledger_unmatched_base,
        "match_gap": match_gap_base,
        "fee_alloc_total": fee_alloc_total,
        "fee_actual_total": fee_actual_base_total,
        "native_match_gap_base": native_gap_base,
        "fx_diff_total": fx_booked,
    }
    counts = {
        "bank_valid": len(bank),
        "bank_matched_rows": len(matched_bank_ids),
        "bank_unmatched_rows": len(unmatched_bank),
        "ledger_valid": len(ledger),
        "ledger_matched_rows": len(matched_ledger_ids),
        "ledger_unmatched_rows": len(unmatched_ledger),
        "matched_groups": len(matches),
        "matched_tier": tier_counts,
        "bank_quarantine": bank_quarantine,
        "ledger_quarantine": ledger_quarantine,
    }
    currencies = [
        {
            "currency": code,
            "rate": item["rate"],
            "bank_gross": item["bank_gross"],
            "bank_fee": item["bank_fee"],
            "bank_total": item["bank_matched"] + item["bank_unmatched"] - item["bank_fee"],
            "bank_matched": item["bank_matched"],
            "bank_unmatched": item["bank_unmatched"],
            "ledger_total": item["ledger_matched"] + item["ledger_unmatched"],
            "ledger_matched": item["ledger_matched"],
            "ledger_unmatched": item["ledger_unmatched"],
            "match_gap": item["match_gap"],
            "match_gap_base": item["match_gap_base"],
            "fx_diff": item["fx_diff"],
            "bank_gross_base": item["bank_gross_base"],
            "bank_fee_base": item["bank_fee_base"],
            "bank_total_base": item["bank_gross_base"] - item["bank_fee_base"],
            "ledger_total_base": item["ledger_total_base"],
            "bank_matched_base": item["bank_matched_base"],
            "ledger_matched_base": item["ledger_matched_base"],
            "match_gap_native_base": item["match_gap_native_base"],
            "fx_diff_booked": item["fx_diff"],
        }
        for code, item in sorted(by_currency.items())
    ]
    return {
        "totals": totals,
        "counts": counts,
        "identities": identities,
        "currency_identities": currency_identities,
        "currencies": currencies,
        "differences": differences,
        "rates": {code: str(value) for code, value in sorted(snapshot.items())},
        "tier_names": TIER_NAMES,
    }


def reason_label(code: str) -> str:
    return reasons.label(code)
