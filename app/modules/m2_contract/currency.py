"""币种与原币/人民币折算。

两条线严格分开：
- 原币：银行流水与账簿凭证各自记录币种和原币金额，核对只在同一币种内进行；
- 人民币：关账按批次结果快照里的汇率折算，汇率不写回原始行。

这里只放纯函数，不读写数据库，保证页面、CSV、清单拿到同一套折算输入。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ...config import CURRENCY_ALIASES, RATE_QUANTUM

# 金额里可能夹带的币种记号。长的记号必须先匹配，否则 US$ 会被拆成 $。
TOKENS = tuple(sorted(
    ("US$", "HK$", "HKD$", "RMB", "CNY", "USD", "HKD", "人民币", "美元", "美金",
     "港币", "港元", "元", "￥", "¥", "$"),
    key=len,
    reverse=True,
))
TOKEN_RE = re.compile("|".join(re.escape(token) for token in TOKENS), re.IGNORECASE)
CODE_RE = re.compile(r"^[A-Z]{3}$")

# 允许出现在文件与汇率表里的币种；未登记的币种不能悄悄参与折算。
SUPPORTED = frozenset({"CNY", "USD", "HKD"})


def _text(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def strip_currency(text: str) -> str:
    """去掉金额中的币种记号、千分位与空白，保留正负号与数字。"""
    cleaned = TOKEN_RE.sub("", text).replace(",", "")
    return re.sub(r"\s+", "", cleaned)


def normalize_currency(raw) -> str | None:
    """把币种列/金额符号归一成 ISO 代码；认不出返回 None，由质量规则隔离。

    只接受登记过的币种，避免 3 个字母的任意代码（如 ABC）被当成可用币种。
    """
    text = _text(raw)
    if not text:
        return None
    key = text.lower()
    if key in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[key]
    upper = text.upper()
    if CODE_RE.match(upper) and upper in SUPPORTED:
        return upper
    return None


def detect_currency(raw_amount) -> str | None:
    """金额串里带币种记号时推断币种；没有记号返回 None。"""
    text = _text(raw_amount)
    for match in TOKEN_RE.finditer(text):
        code = normalize_currency(match.group(0))
        if code:
            return code
    return None


def resolve_currency(currency_raw, amount_raw, default: str) -> tuple[str | None, str]:
    """优先级：显式币种列 > 金额里的记号 > 批次本位币默认值。

    返回 (币种, 来源)。来源写入证据列，复核人能看到币种是怎么来的。
    """
    explicit = normalize_currency(currency_raw)
    if explicit:
        return explicit, "column"
    if _text(currency_raw):
        # 币种列有值但认不出：不能悄悄当成人民币。
        return None, "unknown"
    detected = detect_currency(amount_raw)
    if detected:
        return detected, "amount_symbol"
    return default, "default"


def to_base(amount: Decimal, rate: Decimal, quantum: Decimal) -> Decimal:
    """原币金额 × 汇率，按人民币分位四舍五入。"""
    return (Decimal(amount) * Decimal(rate)).quantize(quantum, rounding=ROUND_HALF_UP)


def normalize_rates(defaults: dict, overrides: dict | None, base_currency: str,
                    rate_quantum: Decimal = RATE_QUANTUM) -> dict:
    """合并默认汇率与本次覆盖值，统一量化到 6 位小数。

    本位币汇率恒为 1；覆盖值必须是正的有限十进制数，否则抛 ValueError。
    """
    rates: dict[str, Decimal] = {}
    for code, value in dict(defaults).items():
        rates[str(code).upper()] = Decimal(str(value)).quantize(
            rate_quantum, rounding=ROUND_HALF_UP
        )
    for code, value in dict(overrides or {}).items():
        key = normalize_currency(code)
        if not key:
            raise ValueError(f"汇率币种无法识别：{code!r}")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"汇率不是数字：{code}={value!r}") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError(f"汇率必须为正数：{code}={value!r}")
        rates[key] = parsed.quantize(rate_quantum, rounding=ROUND_HALF_UP)
    rates[base_currency] = Decimal("1").quantize(rate_quantum)
    return {code: rates[code] for code in sorted(rates)}


def rate_for(rates: dict, currency: str) -> Decimal:
    try:
        return Decimal(str(rates[currency]))
    except KeyError as exc:
        raise ValueError(f"缺少币种汇率：{currency}") from exc


def rates_sha(rates: dict) -> str:
    payload = json.dumps(
        {code: format(Decimal(str(value)), "f") for code, value in sorted(rates.items())},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
