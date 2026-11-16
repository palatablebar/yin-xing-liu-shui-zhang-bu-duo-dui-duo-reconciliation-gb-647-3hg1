"""SQLite 存储：批次、流水行、候选、匹配结果快照。

约定：
- 原始金额与手续费一律按原币存 Decimal 字符串，不落 float，也不被折算覆盖；
- 一次运行的所有行在同一个事务里写入，失败整体回滚；
- 同一 run_id 重跑先删后插，行数、合计与首跑一致；
- 匹配与折算结果按 result set 版本化：旧版本完整保留，active 指针在一次事务内切换；
- 大整数编号（流水号、凭证号）按字符串保形，不过 int()。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .manifest import json_safe

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    bank_sha256 TEXT NOT NULL DEFAULT '',
    ledger_sha256 TEXT NOT NULL DEFAULT '',
    bank_file TEXT NOT NULL DEFAULT '',
    ledger_file TEXT NOT NULL DEFAULT '',
    bank_bytes INTEGER NOT NULL DEFAULT 0,
    ledger_bytes INTEGER NOT NULL DEFAULT 0,
    bank_raw INTEGER NOT NULL DEFAULT 0,
    ledger_raw INTEGER NOT NULL DEFAULT 0,
    bank_valid INTEGER NOT NULL DEFAULT 0,
    ledger_valid INTEGER NOT NULL DEFAULT 0,
    bank_quar INTEGER NOT NULL DEFAULT 0,
    ledger_quar INTEGER NOT NULL DEFAULT 0,
    segments INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'empty',
    input_states_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
    run_id TEXT NOT NULL,
    side TEXT NOT NULL,
    seq INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    txn_no TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '',
    direction INTEGER NOT NULL DEFAULT 0,
    amount TEXT NOT NULL DEFAULT '',
    fee TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'valid',
    reason TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '[]',
    raw_amount TEXT NOT NULL DEFAULT '',
    raw_date TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'CNY',
    currency_source TEXT NOT NULL DEFAULT 'default',
    raw_currency TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id, side, status);
CREATE TABLE IF NOT EXISTS candidates (
    run_id TEXT NOT NULL,
    cand_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id, tier);
CREATE TABLE IF NOT EXISTS result_sets (
    result_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    rule_version TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    rates_json TEXT NOT NULL DEFAULT '{}',
    rates_sha256 TEXT NOT NULL DEFAULT '',
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'superseded',
    totals_json TEXT NOT NULL DEFAULT '{}',
    counts_json TEXT NOT NULL DEFAULT '{}',
    identities_json TEXT NOT NULL DEFAULT '[]',
    differences_json TEXT NOT NULL DEFAULT '[]',
    currencies_json TEXT NOT NULL DEFAULT '[]',
    currency_identities_json TEXT NOT NULL DEFAULT '[]',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_run_version ON result_sets(run_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_active
    ON result_sets(run_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS result_matches (
    result_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    rate TEXT NOT NULL DEFAULT '1',
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    bank_signed TEXT NOT NULL DEFAULT '0',
    ledger_signed TEXT NOT NULL DEFAULT '0',
    bank_sum_base TEXT NOT NULL DEFAULT '0',
    ledger_sum_base TEXT NOT NULL DEFAULT '0',
    bank_signed_base TEXT NOT NULL DEFAULT '0',
    ledger_signed_base TEXT NOT NULL DEFAULT '0',
    diff TEXT NOT NULL,
    diff_base TEXT NOT NULL DEFAULT '0',
    native_gap_base TEXT NOT NULL DEFAULT '0',
    fx_diff TEXT NOT NULL DEFAULT '0',
    day_gap INTEGER NOT NULL DEFAULT 0,
    fee_alloc TEXT NOT NULL DEFAULT '0',
    fee_actual TEXT NOT NULL DEFAULT '0',
    fee_alloc_base TEXT NOT NULL DEFAULT '0',
    fee_actual_base TEXT NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_result_matches ON result_matches(result_id, tier);
"""

# 旧库升级：只补列，不删数据。
MIGRATIONS = {
    "records": (
        ("currency", "TEXT NOT NULL DEFAULT 'CNY'"),
        ("currency_source", "TEXT NOT NULL DEFAULT 'default'"),
        ("raw_currency", "TEXT NOT NULL DEFAULT ''"),
    ),
    "candidates": (("currency", "TEXT NOT NULL DEFAULT 'CNY'"),),
    "result_sets": (("currency_identities_json", "TEXT NOT NULL DEFAULT '[]'"),),
    "result_matches": (("native_gap_base", "TEXT NOT NULL DEFAULT '0'"),),
}

RUN_COLUMNS = (
    "run_id", "created_at", "rule_version", "params_json", "bank_sha256", "ledger_sha256",
    "bank_file", "ledger_file", "bank_bytes", "ledger_bytes", "bank_raw", "ledger_raw",
    "bank_valid", "ledger_valid", "bank_quar", "ledger_quar", "segments", "status",
    "input_states_json", "duration_ms",
)

RESULT_MATCH_COLUMNS = (
    "result_id", "match_id", "tier", "currency", "rate", "bank_ids", "ledger_ids",
    "bank_sum", "ledger_sum", "bank_signed", "ledger_signed", "bank_sum_base",
    "ledger_sum_base", "bank_signed_base", "ledger_signed_base", "diff", "diff_base",
    "native_gap_base", "fx_diff", "day_gap", "fee_alloc", "fee_actual", "fee_alloc_base",
    "fee_actual_base",
)


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, ddl in columns:
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ---------- 批次 ----------
    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json") or "{}")
        data["input_states"] = json.loads(data.pop("input_states_json") or "{}")
        return data

    def latest_run(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return self._run_row(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def save_run(self, run: dict, records: list[dict], candidates: list[dict],
                 matches: list[dict], result: dict | None = None) -> None:
        """整批原子写入；result 一并落库并切换 active，失败时旧版保持原样。"""
        params_json = json.dumps(run.get("params", {}), ensure_ascii=False, sort_keys=True)
        states_json = json.dumps(run.get("input_states", {}), ensure_ascii=False)
        with self._conn() as conn:
            run_id = run["run_id"]
            conn.execute("DELETE FROM records WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
            conn.execute(
                """INSERT OR REPLACE INTO runs (
                    run_id, created_at, rule_version, params_json, bank_sha256, ledger_sha256,
                    bank_file, ledger_file, bank_bytes, ledger_bytes, bank_raw, ledger_raw,
                    bank_valid, ledger_valid, bank_quar, ledger_quar, segments, status,
                    input_states_json, duration_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, run["created_at"], run["rule_version"], params_json,
                    run.get("bank_sha256", ""), run.get("ledger_sha256", ""),
                    run.get("bank_file", ""), run.get("ledger_file", ""),
                    run.get("bank_bytes", 0), run.get("ledger_bytes", 0),
                    run.get("bank_raw", 0), run.get("ledger_raw", 0),
                    run.get("bank_valid", 0), run.get("ledger_valid", 0),
                    run.get("bank_quar", 0), run.get("ledger_quar", 0),
                    run.get("segments", 0), run.get("status", "empty"),
                    states_json, run.get("duration_ms", 0),
                ),
            )
            conn.executemany(
                """INSERT INTO records (run_id, side, seq, row_no, txn_no, account, posted_at,
                    direction, amount, fee, memo, counterparty, status, reason, flags_json,
                    raw_json, raw_amount, raw_date, currency, currency_source, raw_currency)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, r["side"], r["seq"], r["row_no"], r["txn_no"], r.get("account", ""),
                        r.get("posted_at", ""), r.get("direction", 0), str(r.get("amount", "")),
                        str(r.get("fee", "")), r.get("memo", ""), r.get("counterparty", ""),
                        r.get("status", "valid"), r.get("reason", ""),
                        json.dumps(r.get("flags", []), ensure_ascii=False),
                        json.dumps(r.get("raw", []), ensure_ascii=False),
                        str(r.get("raw_amount", "")), str(r.get("raw_date", "")),
                        r.get("currency", run.get("params", {}).get("base_currency", "CNY")),
                        r.get("currency_source", "default"),
                        str(r.get("raw_currency", "")),
                    )
                    for r in records
                ],
            )
            conn.executemany(
                """INSERT INTO candidates (run_id, cand_id, tier, currency, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, c["cand_id"], c["tier"], c.get("currency", "CNY"),
                        json.dumps(c["bank_ids"], ensure_ascii=False),
                        json.dumps(c["ledger_ids"], ensure_ascii=False),
                        str(c["bank_sum"]), str(c["ledger_sum"]), str(c["diff"]), c["day_gap"],
                    )
                    for c in candidates
                ],
            )
            if result is not None:
                self._write_result(conn, result, matches)

    def set_status(self, run_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))

    # ---------- 行 ----------
    def records(self, run_id: str, side: str | None = None,
                status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM records WHERE run_id = ?"
        args: list = [run_id]
        if side:
            sql += " AND side = ?"
            args.append(side)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY side, row_no, seq"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["flags"] = json.loads(item.pop("flags_json") or "[]")
            item["raw"] = json.loads(item.pop("raw_json") or "[]")
            out.append(item)
        return out

    def quarantine(self, run_id: str) -> list[dict]:
        return self.records(run_id, status="quarantine")

    def candidates(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY tier, cand_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def replace_candidates(self, run_id: str, candidates: list[dict]) -> None:
        with self._conn() as conn:
            self._write_candidates(conn, run_id, candidates)

    @staticmethod
    def _write_candidates(conn, run_id: str, candidates: list[dict]) -> None:
        conn.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
        conn.executemany(
            """INSERT INTO candidates (run_id, cand_id, tier, currency, bank_ids, ledger_ids,
                bank_sum, ledger_sum, diff, day_gap) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    run_id, c["cand_id"], c["tier"], c.get("currency", "CNY"),
                    json.dumps(c["bank_ids"], ensure_ascii=False),
                    json.dumps(c["ledger_ids"], ensure_ascii=False),
                    str(c["bank_sum"]), str(c["ledger_sum"]), str(c["diff"]), c["day_gap"],
                )
                for c in candidates
            ],
        )

    # ---------- 结果集 ----------
    @staticmethod
    def _result_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json") or "{}")
        data["rates"] = json.loads(data.pop("rates_json") or "{}")
        data["totals"] = json.loads(data.pop("totals_json") or "{}")
        data["counts"] = json.loads(data.pop("counts_json") or "{}")
        data["identities"] = json.loads(data.pop("identities_json") or "[]")
        data["differences"] = json.loads(data.pop("differences_json") or "[]")
        data["currencies"] = json.loads(data.pop("currencies_json") or "[]")
        data["currency_identities"] = json.loads(
            data.pop("currency_identities_json", "[]") or "[]"
        )
        data["active"] = data["status"] == "active"
        return data

    def _write_result(self, conn, result: dict, matches: list[dict] | None = None,
                      activate: bool = True) -> int:
        run_id = result["run_id"]
        existing = conn.execute(
            "SELECT result_id, version, status FROM result_sets WHERE result_id = ?",
            (result["result_id"],),
        ).fetchone()
        if existing:
            version = existing["version"]
            status = existing["status"]
            conn.execute(
                """UPDATE result_sets SET rule_version=?, params_json=?,
                    rates_json=?, rates_sha256=?, base_currency=?, totals_json=?,
                    counts_json=?, identities_json=?, differences_json=?, currencies_json=?,
                    currency_identities_json=?, duration_ms=? WHERE result_id=?""",
                (
                    result.get("rule_version", ""),
                    json.dumps(result.get("params", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(json_safe(result.get("rates", {})), ensure_ascii=False,
                               sort_keys=True),
                    result.get("rates_sha256", ""), result.get("base_currency", "CNY"),
                    json.dumps(json_safe(result.get("totals", {})), ensure_ascii=False),
                    json.dumps(json_safe(result.get("counts", {})), ensure_ascii=False),
                    json.dumps(json_safe(result.get("identities", [])), ensure_ascii=False),
                    json.dumps(json_safe(result.get("differences", [])), ensure_ascii=False),
                    json.dumps(json_safe(result.get("currencies", [])), ensure_ascii=False),
                    json.dumps(json_safe(result.get("currency_identities", [])),
                               ensure_ascii=False),
                    result.get("duration_ms", 0), result["result_id"],
                ),
            )
        else:
            version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS next FROM result_sets WHERE run_id = ?",
                (run_id,),
            ).fetchone()["next"]
            status = "superseded"
            conn.execute(
                """INSERT INTO result_sets (result_id, run_id, version, created_at,
                    rule_version, params_json, rates_json, rates_sha256, base_currency, status,
                    totals_json, counts_json, identities_json, differences_json,
                    currencies_json, currency_identities_json, duration_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result["result_id"], run_id, version, result["created_at"],
                    result.get("rule_version", ""),
                    json.dumps(result.get("params", {}), ensure_ascii=False, sort_keys=True),
                    json.dumps(json_safe(result.get("rates", {})), ensure_ascii=False,
                               sort_keys=True),
                    result.get("rates_sha256", ""), result.get("base_currency", "CNY"),
                    status,
                    json.dumps(json_safe(result.get("totals", {})), ensure_ascii=False),
                    json.dumps(json_safe(result.get("counts", {})), ensure_ascii=False),
                    json.dumps(json_safe(result.get("identities", [])), ensure_ascii=False),
                    json.dumps(json_safe(result.get("differences", [])), ensure_ascii=False),
                    json.dumps(json_safe(result.get("currencies", [])), ensure_ascii=False),
                    json.dumps(json_safe(result.get("currency_identities", [])),
                               ensure_ascii=False),
                    result.get("duration_ms", 0),
                ),
            )
        if matches is not None:
            self._write_result_matches(conn, result["result_id"], matches)
        if activate:
            # 先撤旧 active 再激活目标，整个事务提交或回滚；失败时旧版仍是 active。
            conn.execute(
                "UPDATE result_sets SET status='superseded' WHERE run_id=? AND status='active'"
                " AND result_id<>?", (run_id, result["result_id"]),
            )
            conn.execute(
                "UPDATE result_sets SET status='active' WHERE result_id=?", (result["result_id"],)
            )
        return version

    @staticmethod
    def _write_result_matches(conn, result_id: str, matches: list[dict]) -> None:
        conn.execute("DELETE FROM result_matches WHERE result_id = ?", (result_id,))
        conn.executemany(
            f"""INSERT INTO result_matches ({','.join(RESULT_MATCH_COLUMNS)})
                VALUES ({','.join('?' for _ in RESULT_MATCH_COLUMNS)})""",
            [
                (
                    result_id, m["match_id"], m["tier"], m.get("currency", "CNY"),
                    str(m.get("rate", "1")),
                    json.dumps(m["bank_ids"], ensure_ascii=False),
                    json.dumps(m["ledger_ids"], ensure_ascii=False),
                    str(m["bank_sum"]), str(m["ledger_sum"]),
                    str(m.get("bank_signed", "0")), str(m.get("ledger_signed", "0")),
                    str(m.get("bank_sum_base", "0")), str(m.get("ledger_sum_base", "0")),
                    str(m.get("bank_signed_base", "0")), str(m.get("ledger_signed_base", "0")),
                    str(m["diff"]), str(m.get("diff_base", "0")),
                    str(m.get("native_gap_base", "0")), str(m.get("fx_diff", "0")),
                    int(m.get("day_gap", 0)), str(m.get("fee_alloc", "0")),
                    str(m.get("fee_actual", "0")), str(m.get("fee_alloc_base", "0")),
                    str(m.get("fee_actual_base", "0")),
                )
                for m in matches
            ],
        )

    def save_result(self, result: dict, matches: list[dict]) -> None:
        with self._conn() as conn:
            self._write_result(conn, result, matches)

    def save_recompute(self, run_id: str, candidates: list[dict], result: dict,
                       matches: list[dict]) -> None:
        """换汇率重算：候选、结果快照与 active 指针在同一事务内完成。"""
        with self._conn() as conn:
            self._write_candidates(conn, run_id, candidates)
            self._write_result(conn, result, matches)

    def activate_result(self, result_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM result_sets WHERE result_id = ?", (result_id,)
            ).fetchone()
            if not row:
                raise KeyError(result_id)
            run_id = row["run_id"]
            conn.execute(
                "UPDATE result_sets SET status='superseded' WHERE run_id=? AND status='active'"
                " AND result_id<>?", (run_id, result_id),
            )
            conn.execute(
                "UPDATE result_sets SET status='active' WHERE result_id=?", (result_id,)
            )
        return self.get_result(result_id)

    def results(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM result_sets WHERE run_id = ? ORDER BY version DESC", (run_id,)
            ).fetchall()
        return [self._result_row(row) for row in rows]

    def get_result(self, result_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM result_sets WHERE result_id = ?", (result_id,)
            ).fetchone()
        return self._result_row(row) if row else None

    def active_result(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM result_sets WHERE run_id = ? AND status = 'active'",
                (run_id,),
            ).fetchone()
        return self._result_row(row) if row else None

    def result_matches(self, result_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM result_matches WHERE result_id = ? ORDER BY tier, match_id",
                (result_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def matches(self, run_id: str) -> list[dict]:
        """当前生效结果集的匹配组；没有结果集时回退到旧 matches 表（只读兼容）。"""
        active = self.active_result(run_id)
        if active:
            return self.result_matches(active["result_id"])
        with self._conn() as conn:
            has_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='matches'"
            ).fetchone()
            if not has_table:
                return []
            rows = conn.execute(
                "SELECT * FROM matches WHERE run_id = ? ORDER BY tier, match_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            item.setdefault("currency", "CNY")
            item.setdefault("rate", "1")
            out.append(item)
        return out
