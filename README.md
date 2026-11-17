# HOMA · 亚马逊运营工作台

源码主仓库：[JiangYu3761/amazon-ops-agent](https://github.com/JiangYu3761/amazon-ops-agent)。后续开发、代码同步和服务器升级以本仓库的 `main` 分支为准。

以产品型号为主线的运营项目工具。**v0.4仍不是全自动亚马逊运营Agent**；详细功能边界与验证记录见 [需求覆盖表](docs/requirements-coverage.md)，网页“功能覆盖与系统设置”也逐项列明。

## 当前界面

- 全流程 SOP 事项详情升级为逐阶段审批单，包含业务表单、完整审批链、每阶段人员与计划／预计／实际时间、退回修订、附件及历史快照；入口保留产品和筛选上下文。见 [审批单说明](docs/approval-workflows.md)。

- 新增「流程协作」：可点击流程图、时间/地点/人员、动态填写项目、附件上传、指定人员会签/或签/顺序审核、版本和操作记录。只有流程管理员可配置字段与审核人。首次管理员设置与具体边界见 [流程协作说明](docs/process-collaboration.md)。
- 多产品 × 店铺、合规、Listing、广告、备货、活动、财务、售后、复盘九个板块的进度矩阵。
- 共享79项SOP；清单与看板；按状态、负责人、阶段、关键词筛选。
- 独立“产品交付清单”：四大分类 → 具体工作项与预期交付 → 对应文件链接；按型号查看文件准备情况和阶段状态，支持多个链接、版本历史、缺件筛选与CSV导出。
- 任务自建、责任分配、依赖、文字/链接证据与审批；更改交付要求后旧审批失效。
- 活动月历与任务截止日、成员工作负载、待审批队列。
- 销售/广告/库存/成本CSV：下载模板、先校验、预览有效行和错误行、再提交导入。
- 独立排名类型与曲线、监控参数；采集器尚未接通，明确显示未运行。
- 常驻Agent入口；默认型号+页面+筛选范围；示例使用规则摘要，服务端可配置外部模型。

## 运行模式

**静态预览**：无构建依赖。只使用当前浏览器的示例工作空间，顶部一直显示来源。内置的G11、S-CK024和HOMA Loom进度、成员与曲线是示例，不代表实际经营状态。旧浏览器演示数据保留在原存储键，本版使用独立的`homa.ops.workspace.v4`键。

```bash
cd frontend
python3 -m http.server 8080
```

Sites打包：`python scripts/build_frontend.py` 生成 `dist/`，不在静态托管内运行Python服务或PostgreSQL。

**完整后端**：FastAPI + PostgreSQL 16。环境依赖安装由Dockerfile完成。

```bash
cp .env.example .env
# 修改数据库密码，并同步DATABASE_URL。不要提交.env。
docker compose up --build -d
```

Web：`http://localhost:8080`；API文档：`http://localhost:8000/docs`。数据库只在Compose内部网络开放。已有数据库升级后须运行 `sh scripts/migrate.sh`。新建产品生成79项SOP；已有旧模板可点击“补全新版SOP”，按旧任务映射保留证据和状态，不替换整库。

流程协作已提供个人登录、管理员配置权限和指定人员填写/审核。旧任务配置接口也要求管理员登录；已启用协作的节点通过流程接口处理。其他经营 API 尚未具备完整访问隔离；团队正式使用前仍应放在已有的私有访问网关后。升级口令独立于流程账户。具体权限边界见上面的流程协作说明。

## Agent页面范围

默认只传当前型号、当前页面及其任务筛选，不传其他页面数据。切换页面或型号切换对话范围；对话ID与产品绑定，拒绝跨产品复用。

显式范围：`@全局`、`@流程`、`@交付清单`（或`@资料`）、`@店铺`、`@页面`、`@广告`、`@库存`、`@活动`、`@利润`、`@售后`、`@合规`、`@排名`、`@建议`、`@导入`、`@团队`。

服务端环境变量：

```env
LLM_API_BASE=https://api.openai.com/v1
LLM_API_KEY=
LLM_MODEL=
```

密钥不放在网页中。兼容Chat Completions接口，不保证所有模型都接受相同参数。外部模型与Amazon API尚未实机验收。

## 产品交付清单

选择型号后，按“店铺运营、Listing与素材、广告运营、供应链与交付”四类查看同一套79项SOP。表格前三列固定为大分类、具体工作项/预期交付、对应文件链接；后面显示准备情况、工作阶段状态与负责人。原九个专业工作台继续保留。

一项工作可以关联多个现有文件或文件夹，记录名称、版本、准备状态与备注。文件“已准备”是人工确认，不代表内容已审核或阶段已完成；更新交付会使任务原审批失效并重新验收，历史链接保留但不计入当前准备情况。模板中的预期文件名是交付建议，并非已存在的文件。

此功能保存文件索引，不上传、读取或更改原平台的文件权限。静态模式只存当前浏览器；服务端模式需执行`db/006_product_documents.sql`，通过PostgreSQL共享索引。Agent默认只读取当前型号和当前清单筛选后的记录，不读取链接中的文件正文。

## 数据导入

标准模板位于 `frontend/templates/`，不是Amazon原始报表自动映射器。日期使用`YYYY-MM-DD`、UTF-8编码、文件≤10 MB。必填值不可空，成本不得默认为0。币种、SKU、ASIN需与产品一致；同一业务键重复导入更新原行。

| 类型 | 必填列 |
|---|---|
| 销售 | date, sku, units_ordered, ordered_revenue |
| 广告 | date, campaign_name, impressions, clicks, spend, sales, orders |
| 库存 | date, sku, fulfillable, reserved, inbound, unsellable |
| 成本 | date, sku, product_cost, first_mile_cost, fba_fee, referral_fee, other_cost |

模板有示例行，导入真实数据前应替换。用户先看预校验结果，再决定提交有效行。后端会再次校验；产品或币种不一致会拒绝本批文件。销售额不等于回款，广告销售不加到总销售额里；退款、税费、仓储与期间费用未接全前不显示净利润。

## 系统升级

仅适用于完整Docker部署。静态站点会显示“未连接升级后端”。

```env
APP_VERSION=0.4.0
APP_BRANCH=main
UPDATE_ENABLED=true
UPDATE_ADMIN_TOKEN=高强度随机口令
GITHUB_DEPLOY_KEY_PATH=/服务器上的绝对路径/github_deploy_key
```

当前更新容器使用 `/opt/amazon-ops-agent`，宿主机项目也应部署在同一绝对路径，以使Docker socket创建容器时正确解析挂载。为私有仓库配置只读Deploy Key，并使用SSH origin。只接受main快进更新与干净工作目录。流程：pg_dump备份→代码更新→构建→逐文件事务迁移→重启→健康检查。失败恢复旧代码；数据库备份留在`backups/`，不会自动逆向迁移。

`updater`挂载Docker socket，具有宿主机高权限，不能作为普通业务容器暴露。完整升级、挂载和恢复链路仍需在实际服务器演练。

## 开发验证

```bash
PYTHONPATH=backend python -m unittest discover -s backend/tests -v
node frontend/tests/operating-rules.cjs
python -m py_compile backend/app/*.py
node --check frontend/app.js
node --check frontend/process.js
node --check frontend/approvals.js
# 在运行的 Compose 开发栈上，创建并清理独立测试数据库：
bash scripts/test_process.sh
```

流程协作已在 PostgreSQL 16、FastAPI 与 nginx 开发容器中完成迁移、接口权限和浏览器协作检查；外部模型、Amazon API 与生产升级恢复仍需单独验收。
