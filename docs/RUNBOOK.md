# 运行手册 ledgerbank

## 启动与停止

```bash
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:18206/api/health
docker compose down -v --remove-orphans
```

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `ledgerbank-data` 保存批次、流水行、候选与匹配。重建后从「载入样例」重新开始。

## 主流程

1. `POST /api/demo/run`：读取两份脏样例，采信、质量、候选、匹配一次跑完。
2. `GET /api/quarantine`：查看隔离行与原因分布。
3. `GET /api/candidates`：查看已生成候选。
4. `GET /api/matches`：查看匹配组、币种、汇率、原币与人民币折算。
5. `GET /api/differences`：查看按币种的原币核对、人民币守恒与差异归因。
6. `GET /api/export/report.csv` 与 `GET /api/export/manifest.json`：导出。

多币种样例：

```bash
curl -s -X POST 'http://127.0.0.1:18206/api/demo/run?bank=bank_statement_fx.csv&ledger=ledger_book_fx.csv'
```

## 汇率与结果版本

```bash
# 查看当前生效结果、汇率与历史版本
curl -s http://127.0.0.1:18206/api/rates

# 调整 USD 汇率并重跑（生成新版本，旧版保留）
curl -s -X POST http://127.0.0.1:18206/api/rates \
  -H 'Content-Type: application/json' \
  -d '{"rates": {"USD": "7.500000"}}'

# 切回某个历史结果（只切换，不重算、不新增版本）
curl -s -X POST http://127.0.0.1:18206/api/rates \
  -H 'Content-Type: application/json' \
  -d '{"result_id": "res-..."}'

# 查看某批次的全部结果版本
curl -s http://127.0.0.1:18206/api/batches/<run_id>/results
```

- 同一组汇率重跑得到同一 `result_id`，不会重复记账。
- 汇率写入失败或非法（0、负数、非数字）返回 400，事务回滚，旧版本仍是 active。
- `POST /api/matches` 重跑沿用当前 active 汇率，不会把已调整的汇率打回默认。
- 原始原币金额与币种保存在 `records`，任何汇率操作都不改写它们。

## 单步重跑

```bash
curl -s -X POST http://127.0.0.1:18206/api/candidates
curl -s -X POST http://127.0.0.1:18206/api/matches
```

同一输入重跑命中同一 `run_id`，先删后插，行数与合计不变。

## 导入自有文件

```bash
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=bank -F file=@sample_data/bank_statement_dirty.csv
```

支持 UTF-8（含 BOM）、UTF-8 无 BOM、GB18030；列名按 `GET /api/contract` 的别名表映射。
币种列可写 ISO 代码或中文名；没有币种列时看金额里的记号（`US$`、`HK$`、`¥`），
都没有则按本位币 CNY。未登记币种整行进隔离区 `CURRENCY_UNKNOWN`。

## 失败处理

- 400：参数、文件或汇率不可用，页面顶部提示原因，保留已填内容；旧结果不切换。
- 404：批次、样例或结果版本不存在，先执行 `POST /api/demo/run` 或核对 result_id。
- 409：还没有候选就求解匹配，或批次只有一侧数据就要重算汇率。
- 隔离行不是失败：按原因码修正源文件后重新导入。

## 日志

```bash
docker compose logs -f app
```
