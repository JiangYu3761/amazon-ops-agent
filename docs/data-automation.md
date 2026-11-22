# API 自动同步与经营计算

本次实现以 SP-API 和 Ads API 为第一阶段：后台同步报告、校验账号与产品范围、保存来源快照，再由同一套后端程序计算页面与 Agent 使用的指标。没有授权或数据不完整时显示原因，不用示例数据或零值代替实际结果。

## 已实现范围

| 板块 | 本版计算或接入 | 仍需的来源／限制 |
|---|---|---|
| 店铺与账户 | SKU 订购销售额、件数、均价；有明确订单数时计算订单均价 | 账户健康、结算和主体信息不由销售报告推断 |
| 产品与合规 | 内部事项、逾期和审批关联信息 | FCC、危险品、标签仍按指定责任人办理与验收，API 不自动批准 |
| Listing 与素材 | SKU Sessions、Page Views、件数／Session、明确订单数时的订单转化 | 不用件数冒充订单；素材制作、SEO 因果诊断和页面自动修改未实现 |
| 广告 | **SP 商品粒度**花费、曝光、点击、CTR、CPC、ACOS、ROAS、广告订单转化、归因成熟提示 | SB、SD、DSP 未接入；SP 花费／销售只称“SP广告花费率”，不冒充全渠道 TACOS |
| 备货与库存 | FBA 可售、预留、在途、不可售；逐 SKU 新鲜度；完整已结束销售日均速、可售天数、补货点和按 MOQ／箱规取整的建议量 | 交期、安全天数等由管理员配置；无 ETA 在途不计入当前可售或可靠到货；不是季节预测或自动采购 |
| 活动与排期 | 期间活动数量、事项与逾期提示 | 没有活动归属订单、促销费用和对照实验时不计算增量 ROI |
| 成本与利润 | 按销售发生日匹配有效 SKU 成本，计算已知成本贡献；SP 花费单独扣除 | 成本仍可 CSV 补录；缺成本不当成零。未接全退款、实际税费、仓储、期间费用和结算，**不显示真实净利润或回款** |
| 订单与售后 | 对应内部事项、责任和逾期 | 本版不接客户个人信息；未接订单／退货队列时，退款率、退货率、处理 SLA 明确缺失 |
| 数据与复盘 | 数据完整日期、来源更新时间、异常说明、已有排名观测与事项绩效 | 自然位、广告位、BSR 不混合；SP-API／Ads 授权不等于已有关键词排名采集 |

本次不是“所有经营算法已经完善”的结论；上表列出的边界保持可见。库存建议属于可解释的均速基准模型，不能称最优采购量；程序不修改亚马逊竞价、预算、价格、Listing 或审批状态。

## 后台处理链路

```mermaid
flowchart LR
  A[管理员配置连接和 SKU 映射] --> B[数据库持久队列]
  B --> C[API 生命周期中的后台调度线程]
  C --> D[只读请求和异步报告轮询]
  D --> E[站点 币种 日期 产品 粒度校验]
  E --> F[原子保存报告快照 事实 日期覆盖]
  F --> G[确定性计算引擎]
  G --> H[九个板块与同范围 Agent]
```

- `SYNC_WORKER_ENABLED=true` 默认启动后台线程。没有启用的连接或缺少授权绑定时，不发平台请求；界面区分后端可用、凭据已配置、授权待验证和真实同步成功。
- 调度和任务存于 PostgreSQL，单连接只允许一个活动任务。多 API 进程通过数据库锁及租约竞争领取；报告 ID 与阶段检查点持久保存，重启后可以接续。
- 心跳独立续约。到期租约不能发布，也不能被旧心跳复活；取消、修改映射或配置版本后，旧执行结果不能覆盖新状态。
- 限流和暂时网络故障使用退避与有限重试。失败不清空已保存的数据；取消和修正保留历史。
- 一次回补窗口限定 1–31 天，属于本应用的处理边界。SP 销售按单日 SKU 报告获取；不把多日 ASIN 合计伪拆为每天。广告建议至少回补 14 天，以覆盖已选的 7 日归因及迟到更正。
- FBA 库存是当前快照；全量连续分页，不把短时效 nextToken 当长期游标，不把未查询到的 SKU 填为零，不伪造历史库存。
- Ads 验证所选 profile 的授权、站点、币种、时区和可用 Marketplace ID；只使用 `spAdvertisedProduct`，不把整个 Campaign 花费复制给多个产品。

## 配置与身份

在“功能覆盖与系统设置”的数据来源管理中，由管理员设置连接、账号标识、站点／区域、币种、业务时区、profile、SKU／ASIN 映射、同步周期和算法参数。普通成员可在现有工作空间权限下查看指标，不能配置连接、触发同步、导入 CSV 或修改参数。

凭据保存在部署端环境或未提交的 `.env`，网页只显示缺少哪些绑定，不接收或回显密钥。默认名称为：

```env
AMAZON_SP_API_CLIENT_ID=
AMAZON_SP_API_CLIENT_SECRET=
AMAZON_SP_API_REFRESH_TOKEN=
AMAZON_ADS_CLIENT_ID=
AMAZON_ADS_CLIENT_SECRET=
AMAZON_ADS_REFRESH_TOKEN=
SYNC_WORKER_ENABLED=true
```

每个店铺使用独立且稳定的环境变量前缀。例如连接前缀 `STORE2` 对应 `STORE2_SP_API_CLIENT_ID` 等；Compose 的可选 `.env` 会把自定义绑定提供给 API。不要在已有前缀中切换到另一店铺的授权；这是部署配置的身份边界，不能仅凭自填账号标识证明 SP 卖家主体。

产品只能绑定一个稳定的 `account_key`；同产品的同类来源只能绑定一个连接，防止跨店铺或同源重复统计。已有事实的连接不能更换账号、profile、凭据前缀、区域、站点、币种或时区。不同店铺的同型号应建立独立产品档案。本版延续现有工作空间身份，尚不是跨组织租户权限系统。

新建产品可填写 IANA 业务时区。连接时区必须与产品档案一致；既有档案的时区错误需由管理员在部署侧核对维护，不能通过改变报告标签静默迁移历史日期。

平台请求需要 LWA `api.amazon.com`、所选区域的 SP／Ads 官方端点，以及 Amazon 返回的已校验 HTTPS S3 下载域名。下载不会把 API 授权头或 Cookie 带到 S3，限制原始／解压体积，签名 URL 不写入前端、检查点或公共错误。

## 指标与来源规则

- 所有金额内部使用 Decimal；比率先合计分子分母再相除。零分母返回空值和解释，真实零保留为零；缺失成本不自动按零。
- 销售额与广告归因销售额分开，不能相加。广告报表粒度、实体 ID、归因窗及成熟度分开处理；重叠报表不能相加。
- 只有 API 明确证明完整的业务日才进入完整日期覆盖。CSV 缺行不代表零销量；当日尚未结束时保持部分数据。补货均速排除尚未结束的今天。
- 库存按每 SKU 不晚于分析日的最新记录获取，不用全产品统一最大日期而丢掉其他 SKU；`fulfillable` 已经是可售量，不再重复扣除 `reserved`。
- 成本按 SKU、销售发生日及有效版本匹配，未来成本不能倒用到过去。
- 日期按产品业务时区，页面和 Agent 使用相同日期范围。指标响应包含值、单位、状态、公式、输入、缺失原因、覆盖和来源时间；Agent 不据被截断的原始明细重算整期合计。
- API 与原 CSV 分开保存。相同产品、数据集和日期优先选取已发布 API 分区，完整性仍由其 coverage 决定；不会把 API 与 CSV 重复相加。旧 API 报告、修改前后 CSV、操作者和内容校验值保留。

## 数据库与接口

`009_data_automation.sql` 是增量迁移，不替换原业务表。新增连接、映射、同步任务、规范化报告快照、API 事实、日期覆盖、参数版本历史、CSV 快照及工作进程心跳。

主要接口：

- `GET /api/products/{id}/analytics?start_date=…&end_date=…`
- `GET/PUT /api/products/{id}/analytics/policy`
- `GET/POST /api/integrations/connections`
- `PUT /api/integrations/connections/{id}`（乐观版本校验）
- `POST /api/integrations/connections/{id}/sync`
- `POST /api/integrations/jobs/{id}/retry`、`/cancel`

新接口均需现有个人登录，写操作要求管理员，并复用 Origin／请求头校验。旧 CSV 导入也已加后端权限验证。旧经营汇总兼容接口改为读取相同引擎，避免两套算法不一致。

## 验证边界与官方依据

公式单元测试、HTTP MockTransport 及独立 PostgreSQL 测试分别验证计算、连接协议、任务恢复与数据隔离；模拟请求不等于真实亚马逊授权验收。真实店铺 Client ID／Secret／Refresh Token 尚未提供，因此没有读取实际销售、库存或广告账号。

SP 官方模型与 schema 已核对。Ads v3 异步流程与 profile 接口通过官方 Postman 核对；商品粒度字段白名单仍需要目标授权账号实测，缺字段将明确失败或缺失，不降级为 Campaign 级花费冒充商品指标。Amazon 文档网站在当前网络策略下无法直接访问，未把全部字段、回溯限制或延迟声称为已现场核实。

- [SP Reports 模型](https://github.com/amzn/selling-partner-api-models/blob/main/models/reports-api-model/reports_2021-06-30.json)
- [Sales & Traffic schema](https://github.com/amzn/selling-partner-api-models/blob/main/schemas/reports/sellerSalesAndTrafficReport.json)
- [FBA Inventory 模型](https://github.com/amzn/selling-partner-api-models/blob/main/models/fba-inventory-api-model/fbaInventory.json)
- [Amazon Ads 官方 Postman](https://github.com/amzn/ads-advanced-tools-docs/blob/main/postman/Amazon_Ads_API.postman_collection.json)

本次实际执行的测试数量、截图及剩余限制记录于 [验收记录](data-automation-validation.md)。
