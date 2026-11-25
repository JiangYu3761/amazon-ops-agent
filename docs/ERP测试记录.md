# ERP 测试与验收入口

## 重跑条件

使用 PostgreSQL 16 的本机测试实例、项目后端要求的 Python 依赖、Node.js、Playwright 和 Chromium。将有权创建和删除测试库的本机测试连接串设置到环境变量 `WORKBENCH_TEST_PG`。套件按次创建随机名称的专用数据库，在退出时删除。Compose 的 `scripts/test_process.sh` 也会使用单独测试库。

```bash
python scripts/check_workbench.py --backend
python scripts/check_workbench.py
python scripts/check_workbench.py --erp-ui
python scripts/build_frontend.py
python scripts/build_workbench_manual.py --check
```

第一项执行全部后端测试，第二项验证原有工作台，第三项逐一验证 ERP 页面，每个页面套件使用独立数据库。可将 `CHROMIUM_PATH` 指向本机 Chromium。逐项调试可用 `WORKBENCH_BROWSER_SCRIPT=frontend/tests/erp_stock.cjs python scripts/check_workbench.py`。源代码中的测试只使用虚构的订单、SKU、回执与金额。

## 本版结果

2026 年 11 月 25 日，迁移 001–024 在本地 PostgreSQL 16 环境完成验证。源目录全量后端回归 273 项全部通过，无跳过。独立 Compose 后端回归同为 273 项，其中 16 项因精简容器未包含源码或 Git 环境而跳过，这些项目已在源目录环境通过。12 组 ERP Chromium 页面套件全部通过。另用 Firefox 验证了退款、退货、结算对账和费用分摊。静态发布目录的 27 个脚本和样式均可找到，手册一致性检查通过。

## 实际验收点

| 业务 | 主要回归文件与断言 |
|---|---|
| 自发货及截单 | `test_erp_fulfillment.py` 与 `erp_fulfillment.cjs`。未付款禁止放行。单笔订单可跨包裹分配，两种取消时点各走独立状态，未获得实际仓库回执时保留跟进。 |
| 采购与应付 | `test_erp_procurement.py`、`test_erp_payables.py`、`check_erp_procurement.cjs`、`check_erp_payables.cjs`。验证拒绝自审、驳回快照、并发签发、跨仓权限、币种精度、余额和付款去重。 |
| 收货与调拨 | `test_erp_receiving.py`、`test_erp_stock.py`、`check_erp_receiving.cjs`、`erp_stock.cjs`。验证少收释放余量、分次质检、双仓数量守恒、并发库存竞争及原收货冲销。 |
| 扫码收货 | `test_erp_receiving_scan.py` 和 `check_erp_scan.cjs`。按仓内来源匹配、一码对应多通知、FNSKU 映射、无权限及确认过账。 |
| 退款与包裹 | `test_erp_refunds.py`、`test_erp_returns.py`、`test_erp_return_stock.py`、`erp_return_stock.cjs`。多笔退款和人工标记分开，销毁件不入库；外部仓报告不可增加本地可用量。 |
| 结算对账 | `test_erp_settlements.py`、`check_erp_reconciliation.cjs`。跨期退款按发放日归集，验证费用符号、银行凭证修订、原流水冲正、CSV 整批校验和重复导入。 |
| 搜索词及经营环比 | `test_erp_search_terms.py`、`test_erp_comparison.py`、`erp_search_terms.cjs`、`erp_comparison.cjs`。验证报告粒度、归因窗口、币种、原始行、零分母与负基数。 |
| 费用分摊和多店经营 | `test_erp_fee_allocations.py`、`check_erp_reconciliation.cjs`、`test_erp_shop_sales.py`、`erp_shop_sales.cjs`。验证最小货币单位守恒及冲正规则，同店多仓授权去重、异币不相加、缺日报与数据修订。 |
| 待办和直达 | `test_erp_queue.py`、`erp_queue.cjs`。同 SKU 产生多条业务单时可定位正确通知；状态变化后待检余量变化并在结清后移出。 |
| 版本升级 | `test_erp_migration.py`。旧库保留业务记录，多个进程并发迁移只执行一次；坏迁移回滚后可重试。 |

后端文件在 `backend/tests/`，浏览器文件在 `frontend/tests/` 和 `scripts/`。第三方平台付款、物流签收和实际外部资金均以原系统真实回执为准，测试使用模拟凭证。
