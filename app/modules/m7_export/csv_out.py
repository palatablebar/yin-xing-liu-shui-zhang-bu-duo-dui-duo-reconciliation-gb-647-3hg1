"""报告 CSV：结果信息、合计、币种核对、匹配组、未匹配、隔离。

每个匹配组同时给出：原币、汇率、原币金额、折算人民币金额、原币差额、折算差异，
复核人无需反推就能看出这一组为什么在同币种内成立、在人民币上差多少。
列结构用 `COLUMNS` 一处定义，避免各区块列错位。
大整数业务号不做数值转换，避免科学计数法与精度丢失。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from ... import reasons
from ..m5_matching.subset import TIER_NAMES

COLUMNS = [
    "区块", "编号", "层级", "币种", "汇率", "金额",
    "银行笔数", "账簿笔数", "银行流水", "账簿凭证",
    "银行原币金额", "账簿原币金额", "原币差额",
    "银行人民币", "账簿人民币", "人民币差额", "折算差异",
    "手续费(原币)", "手续费(人民币)", "原因",
]
HEADER = COLUMNS

# 列下标，按名称取，改列顺序时不用手数。
IDX = {name: index for index, name in enumerate(COLUMNS)}

# 关键字参数不能带括号，给带符号的列名起别名。
ALIAS = {
    "手续费_原币": "手续费(原币)",
    "手续费_人民币": "手续费(人民币)",
}


def _row(**values) -> list[str]:
    out = [""] * len(COLUMNS)
    for name, value in values.items():
        key = ALIAS.get(name, name)
        if key not in IDX:
            raise KeyError(f"未知导出列：{key}")
        out[IDX[key]] = _money(value)
    return out


def _money(value) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if value in (None, ""):
        return ""
    return str(value)


def _rate(value) -> str:
    if value in (None, ""):
        return ""
    return format(Decimal(str(value)), "f")


def report_text(records: list[dict], matches: list[dict], reconciliation: dict) -> str:
    totals = reconciliation["totals"]
    base = reconciliation.get("base_currency", "CNY")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)

    rates_text = " ".join(
        f"{code}={_rate(rate)}"
        for code, rate in sorted(reconciliation.get("rates", {}).items())
    )
    writer.writerow(_row(
        区块="结果",
        编号=reconciliation.get("result_id") or "-",
        币种=base,
        汇率=f"版本 {reconciliation.get('result_version') or '-'}",
        原因=f"汇率：{rates_text}",
    ))

    for key, label in (
        ("bank_total", f"银行合计（{base}）"),
        ("ledger_total", f"账簿合计（{base}）"),
        ("bank_fee", f"手续费合计（{base}）"),
        ("bank_matched", f"银行匹配（{base}）"),
        ("bank_unmatched", f"银行未匹配（{base}）"),
        ("ledger_matched", f"账簿匹配（{base}）"),
        ("ledger_unmatched", f"账簿未匹配（{base}）"),
        ("match_gap", f"匹配差额（{base}）"),
        ("native_match_gap_base", f"其中原币差额折算（{base}）"),
        ("fx_diff_total", f"其中折算尾差（{base}）"),
    ):
        writer.writerow(_row(区块="合计", 编号=label, 币种=base, 金额=totals.get(key)))

    for item in reconciliation.get("currency_identities", []):
        writer.writerow(_row(
            区块="币种核对", 编号=item["currency"], 层级=item["label"],
            币种=item["currency"], 汇率=_rate(item["rate"]),
            银行原币金额=item["bank_total"],
            账簿原币金额=item["ledger_total"],
            原币差额=item["match_gap"],
            人民币差额=item["match_gap_base"],
            折算差异=item["fx_diff"],
            手续费_原币=item["bank_fee"],
            原因="平衡" if item["balanced"] else "不平",
        ))

    for group in matches:
        writer.writerow(_row(
            区块="匹配组", 编号=group["match_id"],
            层级=TIER_NAMES.get(int(group["tier"]), ""),
            币种=group.get("currency", base), 汇率=_rate(group.get("rate")),
            银行笔数=len(group["bank_ids"]), 账簿笔数=len(group["ledger_ids"]),
            银行流水=" ".join(group["bank_ids"]), 账簿凭证=" ".join(group["ledger_ids"]),
            银行原币金额=Decimal(group["bank_sum"]),
            账簿原币金额=Decimal(group["ledger_sum"]),
            原币差额=Decimal(group["diff"]),
            银行人民币=Decimal(group.get("bank_sum_base", "0")),
            账簿人民币=Decimal(group.get("ledger_sum_base", "0")),
            人民币差额=Decimal(group.get("diff_base", "0")),
            折算差异=Decimal(group.get("fx_diff", "0")),
            手续费_原币=Decimal(group.get("fee_actual", "0")),
            手续费_人民币=Decimal(group.get("fee_alloc", group.get("fee_alloc_base", "0"))),
        ))

    for item in reconciliation["differences"]:
        if item["kind"] == "容差":
            continue
        writer.writerow(_row(
            区块=item["kind"], 编号=item["ref"], 层级=item["side"],
            币种=item.get("currency", base), 汇率=_rate(item.get("rate")),
            金额=item.get("amount"),
            银行人民币=item.get("amount_base", item.get("amount")),
            原因=reasons.label(item["reason"]),
        ))

    for record in records:
        if record["status"] != "quarantine":
            continue
        writer.writerow(_row(
            区块="隔离", 编号=record["txn_no"] or "-",
            层级="银行" if record["side"] == "bank" else "账簿",
            币种=record.get("currency", base),
            金额=record.get("raw_amount", ""),
            原因=f"{record['reason']} {reasons.label(record['reason'])}",
        ))

    return "﻿" + buffer.getvalue()
