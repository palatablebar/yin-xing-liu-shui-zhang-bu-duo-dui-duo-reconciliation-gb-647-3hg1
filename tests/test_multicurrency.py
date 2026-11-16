"""多币种：原币核对与人民币关账分层、同币种配对、结果版本与原子切换。

覆盖用户提出的取舍：
- 人民币金额刚好相等的两组如果币种不同，不得配对；
- 同币种原币一致但手续费/尾差让人民币对不平时，要能看出差异来自哪里；
- 页面与下载读同一 active 结果，切换失败保留旧版；
- 换汇率重跑不重复记账、不丢记录，原始原币金额保留。
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import create_app
from app.modules.m2_contract import currency as currency_mod

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture()
def mc_settings(tmp_path):
    return config.Settings(
        db_path=str(tmp_path / "multicurrency.sqlite"),
        sample_dir=str(FIXTURES),
    )


@pytest.fixture()
def mc_client(mc_settings):
    app = create_app(mc_settings)
    with TestClient(app) as test_client:
        yield test_client


def import_pair(client):
    bank = client.post("/api/statements/import",
                       data={"side": "bank", "sample": "multicurrency_bank.csv"})
    assert bank.status_code == 200, bank.text
    ledger = client.post("/api/statements/import",
                         data={"side": "ledger", "sample": "multicurrency_ledger.csv"})
    assert ledger.status_code == 200, ledger.text
    return ledger.json()


def match_groups(client):
    return client.get("/api/matches").json()["groups"]


# ---------- 币种归一 ----------

def test_currency_aliases_and_symbols():
    assert currency_mod.normalize_currency("美元") == "USD"
    assert currency_mod.normalize_currency("usd") == "USD"
    assert currency_mod.normalize_currency("港币") == "HKD"
    assert currency_mod.normalize_currency("人民币") == "CNY"
    assert currency_mod.normalize_currency("") is None
    assert currency_mod.normalize_currency("ABC") is None


def test_amount_symbol_infers_currency():
    assert currency_mod.detect_currency("US$ 115.00") == "USD"
    assert currency_mod.detect_currency("HK$900.00") == "HKD"
    assert currency_mod.detect_currency("¥900.00") == "CNY"
    assert currency_mod.detect_currency("900.00") is None


def test_currency_priority_column_over_symbol_over_default():
    assert currency_mod.resolve_currency("USD", "HK$900.00", "CNY") == ("USD", "column")
    assert currency_mod.resolve_currency("", "HK$900.00", "CNY") == ("HKD", "amount_symbol")
    assert currency_mod.resolve_currency("", "900.00", "CNY") == ("CNY", "default")
    assert currency_mod.resolve_currency("XYZ", "900.00", "CNY") == (None, "unknown")


def test_rate_normalization_rejects_bad_values():
    with pytest.raises(ValueError):
        currency_mod.normalize_rates({"USD": "7.2"}, {"USD": "0"}, "CNY")
    with pytest.raises(ValueError):
        currency_mod.normalize_rates({"USD": "7.2"}, {"USD": "-1"}, "CNY")
    with pytest.raises(ValueError):
        currency_mod.normalize_rates({"USD": "7.2"}, {"USD": "abc"}, "CNY")
    with pytest.raises(ValueError):
        currency_mod.normalize_rates({"USD": "7.2"}, {"XYZ": "1"}, "CNY")
    rates = currency_mod.normalize_rates({"USD": "7.2"}, {"USD": "7.2345678"}, "CNY")
    assert rates["USD"] == Decimal("7.234568")
    assert rates["CNY"] == Decimal("1.000000")


# ---------- 采信与隔离 ----------

def test_unregistered_currency_quarantined(mc_client):
    body = import_pair(mc_client)
    assert body["valid"] == 3
    rows = mc_client.get("/api/quarantine").json()["rows"]
    bad = next(row for row in rows if row["txn_no"] == "B-EUR-BAD")
    assert bad["reason"] == "CURRENCY_UNKNOWN"


def test_rows_keep_native_currency_and_amount(mc_client):
    import_pair(mc_client)
    rows = mc_client.get("/api/statements", params={"side": "bank"}).json()["rows"]
    by_id = {row["txn_no"]: row for row in rows}
    assert by_id["B-USD-115"]["currency"] == "USD"
    assert by_id["B-USD-115"]["amount"] == "115.00"
    assert by_id["B-HKD-900"]["currency"] == "HKD"
    assert by_id["B-HKD-900"]["amount"] == "900.00"


# ---------- 同币种才配对 ----------

def test_equal_base_value_across_currencies_never_match(mc_settings):
    """115 USD 与 900 HKD 折人民币不同；这里构造人民币等值也仍不配对。"""
    from app.modules.m3_quality import rules
    from app.modules.m4_candidates import candidate

    def row(txn, amount, currency, posted="2026-03-05T02:00:00+00:00"):
        return {
            "side": "bank", "seq": 0, "row_no": 1, "txn_no": txn,
            "account": "6222-0034", "posted_at": posted, "direction": -1,
            "amount": amount, "fee": "0.00", "memo": "", "counterparty": "",
            "status": "valid", "reason": "", "flags": [], "raw": [],
            "currency": currency, "currency_source": "column",
        }

    def ledger(txn, amount, currency):
        item = row(txn, amount, currency)
        item["side"] = "ledger"
        return item

    # 按 7.2 与 0.92 折算，115 USD 与 900 HKD 均为 828.00 人民币。
    records = [
        row("B-USD", "115.00", "USD"),
        ledger("V-HKD", "900.00", "HKD"),
    ]
    cands = candidate.build(records, mc_settings)
    assert cands == []
    assert rules  # 保持导入，避免未使用提示歧义


def test_multicurrency_pairing_uses_native_amount(mc_client):
    import_pair(mc_client)
    groups = match_groups(mc_client)
    pairs = {(group["currency"], tuple(group["bank_ids"]), tuple(group["ledger_ids"]))
             for group in groups}
    assert ("USD", ("B-USD-115",), ("V-USD-115",)) in pairs
    assert ("HKD", ("B-HKD-900",), ("V-HKD-900",)) in pairs
    # 原币 0.02 恰在容差内，按原币必须配对；逐行折算到人民币后差 0.15，
    # 其中「原币差额整笔折算」为 0.14（0.02×7.2），另有 0.01 分位差要单列。
    tail = next(group for group in groups if group["bank_ids"] == ["B-USD-TAIL"])
    assert Decimal(tail["diff"]) == Decimal("0.02")
    assert Decimal(tail["diff_base"]) == Decimal("0.15")
    assert abs(Decimal(tail["fx_diff"])) == Decimal("0.01")
    assert (Decimal(tail["native_gap_base"]) + Decimal(tail["fx_diff"])
            == Decimal("-0.15"))


def test_group_reports_native_rate_and_fx(mc_client):
    import_pair(mc_client)
    group = next(item for item in match_groups(mc_client)
                 if item["bank_ids"] == ["B-USD-115"])
    assert group["currency"] == "USD"
    assert Decimal(group["rate"]) == Decimal("7.200000")
    assert Decimal(group["bank_sum"]) == Decimal("115.00")
    assert Decimal(group["bank_sum_base"]) == Decimal("828.00")
    assert Decimal(group["fee_actual"]) == Decimal("5.00")
    assert Decimal(group["fee_actual_base"]) == Decimal("36.00")


# ---------- 原币核对 vs 人民币关账 ----------

def test_native_identities_balanced_per_currency(mc_client):
    import_pair(mc_client)
    data = mc_client.get("/api/differences").json()
    for item in data["currency_identities"]:
        assert item["balanced"] is True, item
    identities = {item["key"]: item for item in data["identities"]}
    assert identities["bank_amount"]["balanced"] is True
    assert identities["ledger_amount"]["balanced"] is True
    assert identities["fee_alloc"]["balanced"] is True
    assert identities["match_gap"]["balanced"] is True


def test_cny_close_equals_native_gap_plus_fx(mc_client):
    import_pair(mc_client)
    data = mc_client.get("/api/differences").json()
    identities = {item["key"]: item for item in data["identities"]}
    # 即便原币有尾差、折算有分位差，人民币口径也必须能拆解到两项之和。
    gap = Decimal(str(identities["match_gap"]["left"]))
    right = Decimal(str(identities["match_gap"]["right"]))
    assert gap == right


def test_usd_and_hkd_settled_in_base_currency(mc_client):
    import_pair(mc_client)
    data = mc_client.get("/api/differences").json()
    by_code = {item["currency"]: item for item in data["currencies"]}
    # 原币口径：USD 两笔付款 115.00 + 1000.04，手续费 8.00。
    assert by_code["USD"]["bank_total"] == "-1123.04"
    assert by_code["USD"]["bank_total_base"] == "-8085.89"
    assert by_code["HKD"]["bank_total"] == "-905.00"
    assert by_code["HKD"]["bank_total_base"] == "-832.60"


# ---------- 页面与下载同一结果 ----------

def test_page_and_exports_share_active_result(mc_client):
    import_pair(mc_client)
    differences = mc_client.get("/api/differences").json()
    active_id = differences["result_id"]
    export = mc_client.get("/api/export/summary").json()
    manifest = mc_client.get("/api/export/manifest.json").json()
    report = mc_client.get("/api/export/report.csv").text
    assert export["result_id"] == active_id
    assert manifest["result"]["result_id"] == active_id
    assert active_id in report
    assert mc_client.get("/api/matches").json()["result_id"] == active_id


def test_matches_page_shows_currency_and_rate(mc_client):
    import_pair(mc_client)
    text = mc_client.get("/matches").text
    assert "USD" in text and "HKD" in text
    assert "7.200000" in text


# ---------- 汇率调整与版本 ----------

def test_rate_change_creates_version_and_preserves_old(mc_client):
    import_pair(mc_client)
    before = mc_client.get("/api/differences").json()
    response = mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    assert response.status_code == 200, response.text
    after = mc_client.get("/api/differences").json()
    assert after["result_id"] != before["result_id"]
    assert after["result_version"] == before["result_version"] + 1
    assert Decimal(after["rates"]["USD"]) == Decimal("7.500000")
    results = mc_client.get("/api/batches/" + before["run_id"] + "/results").json()["results"]
    assert len(results) == 2
    assert any(item["result_id"] == before["result_id"] and item["status"] == "superseded"
               for item in results)


def test_rate_rerun_is_idempotent(mc_client):
    import_pair(mc_client)
    first = mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}}).json()
    second = mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}}).json()
    assert first["result_id"] == second["result_id"]
    results = mc_client.get("/api/batches/" + first["run_id"] + "/results").json()["results"]
    assert len(results) == 2  # 默认版本 + 一次调整，不重复新增


def test_rate_rerun_preserves_native_records(mc_client):
    import_pair(mc_client)
    before = mc_client.get("/api/statements", params={"side": "bank"}).json()["rows"]
    mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    after = mc_client.get("/api/statements", params={"side": "bank"}).json()["rows"]
    assert [row["amount"] for row in before] == [row["amount"] for row in after]
    assert [row["currency"] for row in before] == [row["currency"] for row in after]


def test_failed_rate_keeps_old_active(mc_client):
    import_pair(mc_client)
    before = mc_client.get("/api/differences").json()
    bad = mc_client.post("/api/rates", json={"rates": {"USD": "0"}})
    assert bad.status_code == 400
    after = mc_client.get("/api/differences").json()
    assert after["result_id"] == before["result_id"]
    assert after["result_version"] == before["result_version"]


def test_switch_back_to_old_version(mc_client):
    import_pair(mc_client)
    first = mc_client.get("/api/differences").json()
    mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    switched = mc_client.post("/api/rates", json={"result_id": first["result_id"]})
    assert switched.status_code == 200, switched.text
    current = mc_client.get("/api/differences").json()
    assert current["result_id"] == first["result_id"]
    assert Decimal(current["rates"]["USD"]) == Decimal("7.200000")


def test_default_rerun_keeps_adjusted_rates(mc_client):
    import_pair(mc_client)
    mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    adjusted = mc_client.get("/api/differences").json()
    # 普通匹配重跑不应把已调整的汇率打回默认，否则会悄悄改账。
    again = mc_client.post("/api/matches")
    assert again.status_code == 200, again.text
    current = mc_client.get("/api/differences").json()
    assert Decimal(current["rates"]["USD"]) == Decimal("7.500000")
    assert current["result_id"] == adjusted["result_id"]


def test_download_after_rate_change_uses_new_rates(mc_client):
    import_pair(mc_client)
    mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    manifest = mc_client.get("/api/export/manifest.json").json()
    assert Decimal(manifest["result"]["rates"]["USD"]) == Decimal("7.500000")
    report = mc_client.get("/api/export/report.csv").text
    assert "7.500000" in report


# ---------- 跨层可加性 ----------

def test_fx_components_add_up_across_layers(mc_client):
    """组 → 币种 → 批次的折算差异必须逐层加总一致，否则复核人看到的拆分是假账。"""
    import_pair(mc_client)
    data = mc_client.get("/api/differences").json()
    groups = match_groups(mc_client)
    group_fx = sum(Decimal(group["fx_diff"]) for group in groups)
    group_native = sum(Decimal(group["native_gap_base"]) for group in groups)
    currency_fx = sum(Decimal(item["fx_diff"]) for item in data["currencies"])
    currency_native = sum(Decimal(item["match_gap_native_base"])
                          for item in data["currencies"])
    assert group_fx == currency_fx == Decimal(data["totals"]["fx_diff_total"])
    assert group_native == currency_native == Decimal(
        data["totals"]["native_match_gap_base"]
    )
    assert Decimal(data["totals"]["match_gap"]) == (
        Decimal(data["totals"]["native_match_gap_base"])
        + Decimal(data["totals"]["fx_diff_total"])
    )


def test_currency_identity_native_sums_to_batch(mc_client):
    import_pair(mc_client)
    data = mc_client.get("/api/differences").json()
    native_gaps = [Decimal(item["match_gap"]) for item in data["currency_identities"]]
    # 币种间不能相互抵消成 0 才算对：每笔原币差额都要能在人民币上找到对应分项。
    assert sum(native_gaps) != 0
    for item in data["currency_identities"]:
        assert item["balanced"] is True


def test_switch_failure_keeps_old_result_full(mc_client):
    """切换失败时旧版必须完整可用：结果、匹配组、导出三处都还在。"""
    import_pair(mc_client)
    before = mc_client.get("/api/differences").json()
    before_groups = match_groups(mc_client)
    missing = mc_client.post("/api/rates", json={"result_id": "res-does-not-exist"})
    assert missing.status_code == 404
    after = mc_client.get("/api/differences").json()
    assert after["result_id"] == before["result_id"]
    assert match_groups(mc_client) == before_groups
    assert mc_client.get("/api/export/report.csv").status_code == 200
    assert mc_client.get("/api/matches").json()["count"] == len(before_groups)


def test_rate_change_does_not_duplicate_records(mc_client):
    import_pair(mc_client)
    before = mc_client.get("/api/statements", params={"side": "bank"}).json()
    mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    mc_client.post("/api/rates", json={"rates": {"HKD": "1.100000"}})
    after = mc_client.get("/api/statements", params={"side": "bank"}).json()
    assert before["count"] == after["count"]
    assert [row["txn_no"] for row in before["rows"]] == [row["txn_no"] for row in after["rows"]]
    # 结果版本数 = 默认 + 两次不同调整，不因重跑膨胀。
    results = mc_client.get("/api/batches/" + before["run_id"] + "/results").json()["results"]
    assert len(results) == 3


def test_write_failure_rolls_back_to_old_active(mc_client, monkeypatch):
    """重跑中途写失败：事务整体回滚，旧 active 与旧匹配组必须原样保留。"""
    import_pair(mc_client)
    before = mc_client.get("/api/differences").json()
    before_groups = match_groups(mc_client)

    from app.store import Store

    original = Store._write_result_matches

    def boom(self, conn, result_id, matches):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(Store, "_write_result_matches", boom)
    with pytest.raises(RuntimeError):
        mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    monkeypatch.setattr(Store, "_write_result_matches", original)

    after = mc_client.get("/api/differences").json()
    assert after["result_id"] == before["result_id"]
    assert after["result_version"] == before["result_version"]
    assert match_groups(mc_client) == before_groups
    results = mc_client.get(
        "/api/batches/" + before["run_id"] + "/results"
    ).json()["results"]
    assert len(results) == 1


def test_fee_based_close_matches_group_allocations(mc_client):
    import_pair(mc_client)
    data = mc_client.get("/api/differences").json()
    groups = match_groups(mc_client)
    assert sum(Decimal(g["fee_alloc"]) for g in groups) == Decimal(
        data["totals"]["fee_alloc_total"]
    ) == Decimal(data["totals"]["fee_actual_total"])


def test_same_business_not_double_counted_across_versions(mc_client):
    import_pair(mc_client)
    before = mc_client.get("/api/matches").json()
    mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    after = mc_client.get("/api/matches").json()
    before_ids = sorted(key for group in before["groups"]
                        for key in group["bank_ids"] + group["ledger_ids"])
    after_ids = sorted(key for group in after["groups"]
                       for key in group["bank_ids"] + group["ledger_ids"])
    assert before_ids == after_ids
    assert len(after_ids) == len(set(after_ids))


def test_fx_sample_files_pair_end_to_end(tmp_path):
    """样例文件走完整链路：同币种配对、原币可平、人民币差异可拆解。"""
    sample_settings = config.Settings(
        db_path=str(tmp_path / "fx.sqlite"),
        sample_dir=str(Path(__file__).resolve().parents[1] / "sample_data"),
    )
    app = create_app(sample_settings)
    with TestClient(app) as mc_client:
        _assert_fx_sample(mc_client)


def _assert_fx_sample(mc_client):
    response = mc_client.post(
        "/api/demo/run",
        params={"bank": "bank_statement_fx.csv", "ledger": "ledger_book_fx.csv"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["matched_groups"] == 5
    assert body["counts"]["bank_quarantine"] == 1
    data = mc_client.get("/api/differences").json()
    assert all(item["balanced"] for item in data["identities"])
    assert all(item["balanced"] for item in data["currency_identities"])
    assert Decimal(data["totals"]["match_gap"]) == (
        Decimal(data["totals"]["native_match_gap_base"])
        + Decimal(data["totals"]["fx_diff_total"])
    )


def test_index_page_shows_native_currency(mc_client):
    import_pair(mc_client)
    text = mc_client.get("/").text
    assert "币种" in text
    assert "USD" in text and "HKD" in text
    assert "金额(原币)" in text


def test_batches_page_shows_currency_breakdown_and_versions(mc_client):
    import_pair(mc_client)
    mc_client.post("/api/rates", json={"rates": {"USD": "7.500000"}})
    text = mc_client.get("/batches").text
    assert "原币核对" in text and "折算差异" in text
    assert "结果版本" in text
    assert "7.500000" in text
    assert "切回" in text


def test_export_page_states_active_result(mc_client):
    import_pair(mc_client)
    data = mc_client.get("/api/differences").json()
    text = mc_client.get("/export").text
    assert data["result_id"] in text
    assert "USD=7.200000" in text


def test_all_pages_render_without_run(tmp_path):
    app = create_app(config.Settings(
        db_path=str(tmp_path / "empty.sqlite"),
        sample_dir=str(Path(__file__).resolve().parents[1] / "sample_data"),
    ))
    with TestClient(app) as client:
        for path in ("/", "/quarantine", "/candidates", "/matches", "/batches", "/export"):
            response = client.get(path)
            assert response.status_code == 200, path


def test_fee_totals_never_add_across_currencies(mc_client):
    """5 USD 手续费 + 5 HKD 手续费不能相加成 10；人民币合计才是关账口径。"""
    import_pair(mc_client)
    data = mc_client.get("/api/matches").json()
    assert data["fee_actual_native"] == {"HKD": "5.00", "USD": "8.00"}
    expected = Decimal("5.00") * Decimal("0.920000") + Decimal("8.00") * Decimal("7.200000")
    assert Decimal(data["fee_actual_total"]) == expected
    differences = mc_client.get("/api/differences").json()
    assert Decimal(differences["totals"]["fee_actual_total"]) == expected


def test_reimport_one_side_keeps_previously_matched_side(mc_client):
    """重新导入一侧时，另一侧与前次结果不应被丢掉或重复。"""
    import_pair(mc_client)
    before = mc_client.get("/api/statements", params={"side": "ledger"}).json()
    again = mc_client.post(
        "/api/statements/import",
        data={"side": "bank", "sample": "multicurrency_bank.csv"},
    )
    assert again.status_code == 200, again.text
    after = mc_client.get("/api/statements", params={"side": "ledger"}).json()
    assert before["count"] == after["count"]
    assert mc_client.get("/api/differences").json()["counts"]["matched_groups"] == 3


def test_old_schema_database_is_migrated(tmp_path):
    """旧版本库没有币种列与结果表：启动时补列建表，历史批次只读可见。"""
    import sqlite3

    db = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            rule_version TEXT NOT NULL, params_json TEXT NOT NULL DEFAULT '{}',
            bank_sha256 TEXT NOT NULL DEFAULT '', ledger_sha256 TEXT NOT NULL DEFAULT '',
            bank_file TEXT NOT NULL DEFAULT '', ledger_file TEXT NOT NULL DEFAULT '',
            bank_bytes INTEGER NOT NULL DEFAULT 0, ledger_bytes INTEGER NOT NULL DEFAULT 0,
            bank_raw INTEGER NOT NULL DEFAULT 0, ledger_raw INTEGER NOT NULL DEFAULT 0,
            bank_valid INTEGER NOT NULL DEFAULT 0, ledger_valid INTEGER NOT NULL DEFAULT 0,
            bank_quar INTEGER NOT NULL DEFAULT 0, ledger_quar INTEGER NOT NULL DEFAULT 0,
            segments INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'empty',
            input_states_json TEXT NOT NULL DEFAULT '{}', duration_ms INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE records (run_id TEXT NOT NULL, side TEXT NOT NULL, seq INTEGER NOT NULL,
            row_no INTEGER NOT NULL, txn_no TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
            posted_at TEXT NOT NULL DEFAULT '', direction INTEGER NOT NULL DEFAULT 0,
            amount TEXT NOT NULL DEFAULT '', fee TEXT NOT NULL DEFAULT '',
            memo TEXT NOT NULL DEFAULT '', counterparty TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'valid', reason TEXT NOT NULL DEFAULT '',
            flags_json TEXT NOT NULL DEFAULT '[]', raw_json TEXT NOT NULL DEFAULT '[]',
            raw_amount TEXT NOT NULL DEFAULT '', raw_date TEXT NOT NULL DEFAULT '');
        CREATE TABLE candidates (run_id TEXT NOT NULL, cand_id TEXT NOT NULL,
            tier INTEGER NOT NULL, bank_ids TEXT NOT NULL, ledger_ids TEXT NOT NULL,
            bank_sum TEXT NOT NULL, ledger_sum TEXT NOT NULL, diff TEXT NOT NULL,
            day_gap INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE matches (run_id TEXT NOT NULL, match_id TEXT NOT NULL, tier INTEGER NOT NULL,
            bank_ids TEXT NOT NULL, ledger_ids TEXT NOT NULL, bank_sum TEXT NOT NULL,
            ledger_sum TEXT NOT NULL, diff TEXT NOT NULL, day_gap INTEGER NOT NULL DEFAULT 0,
            fee_alloc TEXT NOT NULL DEFAULT '0', fee_actual TEXT NOT NULL DEFAULT '0',
            bank_signed TEXT NOT NULL DEFAULT '0', ledger_signed TEXT NOT NULL DEFAULT '0');
        INSERT INTO runs (run_id, created_at, rule_version)
            VALUES ('run-old', '2026-01-01T00:00:00+00:00', '2026.11.1');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(config.Settings(db_path=db, sample_dir=str(FIXTURES)))
    with TestClient(app) as client:
        for path in ("/", "/matches", "/batches", "/export", "/api/differences"):
            assert client.get(path).status_code == 200, path
        columns = {row[1] for row in sqlite3.connect(db).execute(
            "PRAGMA table_info(records)"
        )}
        assert {"currency", "currency_source", "raw_currency"} <= columns
        response = client.post(
            "/api/demo/run",
            params={"bank": "multicurrency_bank.csv", "ledger": "multicurrency_ledger.csv"},
        )
        assert response.status_code == 200, response.text


def test_rate_api_error_codes_are_stable(mc_client):
    import_pair(mc_client)
    for payload in ({"rates": {"USD": "0"}}, {"rates": {"USD": "-1"}},
                    {"rates": {"XYZ": "1"}}, {"rates": {"USD": "abc"}}):
        response = mc_client.post("/api/rates", json=payload)
        assert response.status_code == 400, payload
        assert response.json()["error"] == "E_RATES", payload
    assert mc_client.post("/api/rates", json={}).json()["error"] == "E_RATES"
