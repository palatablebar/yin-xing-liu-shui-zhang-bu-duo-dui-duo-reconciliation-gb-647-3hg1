"""运行参数与常量：容差、日期窗口、子集上限、编码链、空值哨兵、账户登记。

所有阈值只在这里定义一次，规则模块与页面都从这里取，避免两处不一致。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

RULE_VERSION = "2026.11.1"
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

# 编码回退链：先按带 BOM 的 UTF-8 解，再按 UTF-8，最后按 GB18030。
ENCODING_CHAIN = ("utf-8-sig", "utf-8", "gb18030")

# 空值哨兵：金额列命中这些值表示「没有填」，不是 0。
SENTINELS = ("", "-", "--", "n/a", "na", "null", "none", "未知")

# 借贷方向别名：正号表示资金流入，负号表示资金流出。
DIRECTION_ALIASES = {
    "贷": 1, "收": 1, "收入": 1, "credit": 1, "cr": 1, "+": 1, "c": 1,
    "借": -1, "付": -1, "支出": -1, "debit": -1, "dr": -1, "-": -1, "d": -1,
}

# 账户登记：未登记的账户进隔离区，不参与对账。
ACCOUNTS = {
    "6222-0012": "基本户",
    "6222-0034": "一般户",
    "6222-0056": "货款户",
}

# 关账本位币。原币用于核对，人民币用于关账，两者不混用。
BASE_CURRENCY = "CNY"

# 币种别名：输入列里既可能写 ISO 代码，也可能写中文名或符号。
CURRENCY_ALIASES = {
    "cny": "CNY", "rmb": "CNY", "人民币": "CNY", "元": "CNY", "￥": "CNY", "¥": "CNY",
    "usd": "USD", "美元": "USD", "美金": "USD", "us$": "USD", "$": "USD",
    "hkd": "HKD", "港币": "HKD", "港元": "HKD", "hk$": "HKD", "hkd$": "HKD",
}

# 默认汇率（1 单位原币折合人民币），可在运行时按批次调整并留版本。
# 汇率不进入 run_id：同一批原始文件换汇率重跑仍属同一批次，结果按 result set 版本化。
DEFAULT_RATES = {
    "CNY": Decimal("1.000000"),
    "USD": Decimal("7.200000"),
    "HKD": Decimal("0.920000"),
}

# 汇率保留 6 位小数；人民币金额折算后量化到分。
RATE_QUANTUM = Decimal("0.000001")


def dec(value) -> Decimal:
    """把存储里的字符串金额转回 Decimal；已是 Decimal 时原样返回。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value if value not in (None, "") else "0"))


@dataclass(frozen=True)
class Settings:
    db_path: str
    rule_version: str = RULE_VERSION
    tolerance: Decimal = Decimal("0.02")
    window_days: int = 3
    subset_max: int = 4
    fee_quantum: Decimal = Decimal("0.01")
    tz_name: str = "Asia/Shanghai"
    sample_dir: str = str(PROJECT_DIR / "sample_data")
    encodings: tuple = ENCODING_CHAIN
    sentinels: tuple = SENTINELS
    accounts: dict = field(default_factory=lambda: dict(ACCOUNTS))
    base_currency: str = BASE_CURRENCY
    default_rates: dict = field(default_factory=lambda: dict(DEFAULT_RATES))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("LEDGERBANK_DB", str(PROJECT_DIR / "var" / "ledgerbank.sqlite")),
            rule_version=os.environ.get("RULE_VERSION", RULE_VERSION),
            sample_dir=os.environ.get("SAMPLE_DIR", str(PROJECT_DIR / "sample_data")),
            tz_name=os.environ.get("TZ", "Asia/Shanghai"),
        )

    def params(self) -> dict:
        # 汇率故意不进入 params：params 参与 run_id，换汇率应留在同一批次内按版本重跑。
        return {
            "tolerance": str(self.tolerance),
            "window_days": self.window_days,
            "subset_max": self.subset_max,
            "fee_quantum": str(self.fee_quantum),
            "timezone": self.tz_name,
            "base_currency": self.base_currency,
        }

    def default_rate(self, currency: str) -> Decimal:
        return self.default_rates.get(currency, Decimal("1.000000"))
