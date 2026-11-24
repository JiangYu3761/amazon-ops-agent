"""Build the operator manual from the same definitions used by the workbench."""
import argparse
import html
import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
DOCS = ROOT / "docs"
TITLE = "HOMA 运营工作台执行手册"
GROUPS = {
    "工作空间": ("overview", "deliverables", "process", "workflow", "team"),
    "运营工作台": ("shop", "listing", "ads", "inventory", "calendar", "finance", "service", "compliance"),
    "分析与工具": ("ranking", "agent", "imports"),
    "系统设置": ("settings",),
}
TYPES = {"text": "单行文字", "textarea": "多行说明", "date": "日期", "number": "数值", "select": "单选", "url": "资料链接"}
COMPARISON = {"lte": "≤", "gte": "≥", "eq": "＝", "neq": "≠"}


@dataclass
class Chapter:
    anchor: str
    title: str
    blocks: list = field(default_factory=list)

    def text(self, value):
        self.blocks.append(("p", value))

    def heading(self, value):
        self.blocks.append(("h", value))

    def table(self, headers, rows):
        self.blocks.append(("table", (headers, list(rows))))

    def steps(self, rows):
        self.blocks.append(("steps", list(rows)))


def load_pages():
    pages = {}
    for name in ("workspace", "growth", "operations"):
        source = json.loads((FRONT / f"workbench-{name}.json").read_text(encoding="utf-8"))
        for page in source:
            if page["id"] in pages:
                raise ValueError(f"重复页面 {page['id']}")
            pages[page["id"]] = page
    expected = {key for keys in GROUPS.values() for key in keys}
    if set(pages) != expected:
        raise ValueError(f"页面目录不匹配：{set(pages) ^ expected}")
    return pages


def field_constraints(f):
    bounds = []
    if "min" in f:
        bounds.append(f"最低 {f['min']}")
    if "max" in f:
        bounds.append(f"最高 {f['max']}")
    if f["type"] == "select":
        bounds.append(" / ".join(f["options"]))
    elif f["type"] == "date":
        bounds.append("年-月-日")
    elif f["type"] == "url":
        bounds.append("HTTP(S) 地址")
    return "，".join(bounds) or "见字段说明"


def describe_check(check, fields):
    op = check["op"]
    keys = [fields[k] for k in check["keys"]]
    lead = f"「{keys[0]}」"
    if op in COMPARISON:
        return f"{lead} {COMPARISON[op]} 「{keys[1]}」", "不满足时阻止提交"
    if op == "required_if":
        return f"{lead}为「{check['when']}」时需填写「{keys[1]}」", "缺少条件字段时阻止提交"
    if op == "block_if":
        return f"{lead}为「{check['when']}」", "命中时阻止提交"
    if op == "deadline":
        return f"{lead}减产品业务时区的今天", "负数为逾期，零为今天到期，正数显示剩余天数"
    if op == "ratio":
        return f"{lead} ÷「{keys[1]}」× 100%", "分母为零时比例无定义"
    if op == "difference":
        return f"{lead}减「{keys[1]}」", "按所填写的单位解释差额"
    raise ValueError(f"未知业务规则 {op}")


def operation_page(page):
    c = Chapter(page["id"], page["title"])
    c.text(page["purpose"])
    c.table(("适用岗位", "工作范围与数据来源"), [(" / ".join(page["roles"]), page["scope"])])
    c.heading("选择办理类型")
    c.table(("办理类型", "什么时候用", "办理完成后交给谁、得到什么"),
            ((s["title"], s["purpose"], s["output"]) for s in page["sections"]))
    c.heading("岗位办理顺序")
    c.steps(page["flow"])
    for section in page["sections"]:
        c.heading(section["title"] + " · 填写与校验")
        c.text("从「" + section["action"] + "」开始。" + section["source"])
        c.table(("业务字段", "填写要求", "类型与允许值", "业务含义、来源或填写规则"), (
            (f["label"], "提交必填" if f["required"] else "按需填写",
             TYPES[f["type"]] + " · " + field_constraints(f), f["help"])
            for f in section["fields"]))
        names = {f["key"]: f["label"] for f in section["fields"]}
        checks = []
        for check in section["checks"]:
            expression, failure = describe_check(check, names)
            checks.append((check["label"], expression, failure, check["message"]))
        c.table(("确定性检查", "计算或条件", "输出或失败处理", "业务说明"), checks)
    c.heading("业务判断与证据不足的处理")
    c.text("以下是经办人和审核人执行的业务口径。表单可直接计算和阻断的项目见各办理类型上方的确定性检查。")
    c.table(("判断", "输入或来源", "判断方法", "缺失时如何处理", "交付或责任人"),
            ((r["name"], r["inputs"], r["logic"], r["missing"], r["output"]) for r in page["rules"]))
    c.heading("使用 AI 起草")
    a = page["ai"]
    c.table(("输入范围", "可交付草稿", "人工决定"), [(a["inputs"], a["draft"], a["human"])])
    c.text("岗位提示词　" + a["example"])
    c.heading("验收场景")
    c.table(("给定输入", "执行动作", "预期可核验结果"), ((t["input"], t["action"], t["expect"]) for t in page["tests"]))
    return c


def quick_start():
    c = Chapter("shift", "每日开工与业务单办理")
    c.text("经办人可以从这里开始当日工作。主管可直接查看团队待办；开发与验收人员可按页查字段、计算方法与预期结果。文中业务案例采用虚构数据。")
    c.steps([
        "登录后先看环境标识，核对当前产品、店铺映射、站点和业务时区。静态演示仅供练习，正式业务在已连接后端的工作空间办理。",
        "打开产品总览，先看自己的待办、今天到期和逾期项，再选一个具体型号核对经营线索。查看指标覆盖日期和证据来源；数据不可用时进入数据导入中心或系统设置核查。",
        "点进对应专业工作台，选择办理类型。先从已有业务单、原任务或信号继续，首次办理再建草稿。产品事项写明型号和站点，店铺级事项选店铺范围，再写明业务对象和标题，填表时用实际收集到的记录或回执作依据。",
        "填报时观察保存状态。有效店铺或产品范围、标题和对象齐全后，停止输入约一秒，正式工作台自动把草稿写入 PostgreSQL。资料未齐时先保存到本机。看到“已存服务器”即可换会话继续；出现版本冲突时，先保留原稿，再核对服务端版本。自动保存只保存草稿，提交审核需单独确认。",
        "检查规则结果与待补项，按需要生成辅助草稿。经办人核对事实后提交。未指定独立审核人时，由管理员在草稿上分配有效成员。",
        "审核人对照证据同意或填写退回原因。获批后经办人在业务平台或线下实际办理，回来登记平台回执、订单号或可查凭证。",
        "审核人按通过条件查看执行结果、填复核结论并关闭。收工时列出仍待审、待执行、待复核项和明天的责任人；专业审批任务回到原审批单处理。",
    ])
    c.table(("当前状态", "允许的下一步与操作者", "异常怎么处理"), [
        ("草稿", "经办人填写和提交，管理员可分配独立审核人。", "保存失败时保留输入并核对服务状态。必填、站点、对象、类型和规则在提交时校验。"),
        ("待审核", "指定审核人同意，或说明理由退回草稿。", "纠正内容由经办人在退回后办理；管理员角色本身不提供代审权。"),
        ("待执行", "审批已通过。经办人执行并登记可核对凭证。", "批准采购申请后，平台下单与供应商确认仍需经办人办理。"),
        ("待复核", "经办人已登记执行记录。指定审核人写入复核结论并结束办理。", "对证据有疑问时先补查来源，实际结果尚未取得时继续跟进。"),
        ("已归档", "按产品、事项与提交版本回看历史。", "同时编辑发生版本冲突时重新读取现有记录，逐项确认差异后再办理。"),
    ])
    c.text("业务单用于跨岗位登记判断、审批与执行证据。涉及原 SOP 的阶段流转、依赖、文件上传和签核时，打开所关联的产品任务。产品交付清单保存资料链接索引，审批环节上传件按该流程查看。")
    c.heading("共用字段")
    c.table(("字段", "来源与校验", "填写例"), [
        ("产品与站点", "按选定型号及其站点核对；跨型号事项逐一确认影响范围。", "型号演练甲 / 美国站"),
        ("业务对象", "填能在来源系统定位的 SKU、广告活动、事项号或其他标识。", "演练用 SKU-01"),
        ("标题", "以动词和对象描述本次要办的事。", "核实 SKU-01 的到货延期"),
        ("经办人和审核人", "创建人负责办理；由有权管理员分配不同的有效审核人。", "供应链经办人、独立复核人"),
        ("关联 SOP", "选本产品任务实例；模板索引与实例主键分别使用。", "关联本型号的补货事项"),
        ("执行证据", "执行时填可核实来源，如回执编号、受控链接或记录引用。", "演练采购回执 PO-示例-01"),
    ])
    return c


def lifecycle():
    c = Chapter("lifecycle", "从机会验证到退市")
    c.table(("阶段", "主办与入口", "拿什么来", "交给下一阶段什么"), [
        ("机会评估", "产品负责人 → 产品总览 → 产品机会与可行性", "客户场景、竞品证据、报价及验证条件", "有来源、假设、通过门槛及复查日的机会评估"),
        ("季度组合决策", "运营主管 → 产品总览 → 季度目标与组合取舍", "预算、考核口径、产品间取舍理由", "明确产品、币种、资源来源和优先级的决议"),
        ("上市准备", "项目负责人 → 全流程 SOP / 流程协作 / 产品交付清单", "上市窗口、前置项、版本化文件、指定人员", "各专业工作台接手的原任务与审批依据"),
        ("上线与获客", "运营、广告、设计 → Listing 与素材 / 广告运营 / 活动与排期", "有效素材、活动资格、预算与实验口径", "经审核的发布或投放方案与人工执行回执"),
        ("履约与经营", "供应链、财务、客服 → 备货与库存 / 成本与利润 / 订单与售后", "库存快照、有效销量、费用资料、售后原始记录", "采购与收货差异、已知贡献、售后处置证据"),
        ("成熟增长与复盘", "运营主管 → 排名监控 / Agent 决策中心 / 产品总览", "相同口径的观测、归因窗口、假设与结果", "继续、调整或停止的决定及下一次复查日"),
        ("收缩退市", "产品负责人 → 产品总览 → 生命周期行动计划", "在库品、未结费用、存续订单及售后承接方", "清货、停投和售后交接清单，再核对归档范围"),
    ])
    c.heading("跨页演练：一笔补货从发现到复核")
    c.text("以下为操作练习。示例用美国站 SKU-01，可售 120 件，已结束且覆盖完整的十天共售 100 件。假设日均销量 10 件，当前可售约 12 天；采购与运输合计需 20 天。")
    c.steps([
        "运营从总览进入备货与库存，核实快照时间、已结束销售日、SKU 映射及单位。以上数字在库存可用口径下提示约八天缺口；有在途但缺预计到货时间时请单独核实。",
        "供应链补录有效交期、安全库存、最小订购量与箱规，审阅建议数量并说明调整原因。数据缺失先补资料，必要时到导入中心校验库存或销量。",
        "创建补货业务单，填写站点、SKU、数量、依据和凭证来源，关联同一产品的原任务。独立审核人复核可用资金及备货假设，退回时写清需补材料。",
        "经办人在获批后下单，登记回执。物流与入仓环节继续在各自的原任务追踪。若到货短少，记录实际数量、差异理由与下一步责任人。",
        "财务在成本与利润页核对已发生的成本项目；复核人对照执行证据和最新库存结果填写结论。完整结算尚未取得时列出待补字段。",
    ])
    c.text("辅助分析默认依据已提供的范围、字段和规则。外部模型需由部署方配置，数据源、授权和实际覆盖单独核验；模型产出供人工审批，不能作为平台执行凭证。")
    return c


def intake_and_handoff():
    c = Chapter("intake", "把运营原文分给岗位，再跨页交接")
    c.heading("按原句确认去向")
    c.steps([
        "在产品总览或 Agent 决策中心的主业务队列，展开「把一段运营记录分给对应岗位」。选择办理产品，输入当次工作记录，点击「识别并预览」。",
        "识别在本地按句号、问号、感叹号、分号和换行拆句，匹配各工作台的业务词。一句可生成多个候选，每个候选显示原句和命中的词；无匹配的句子留给人工选板块。同一次预览会合并相同原句、相同目标的候选。",
        "逐条检查产品、建议办理板块和办理子页。点击「打开办理草稿」后补全业务对象、责任和专业字段；「来源原文」保留该候选对应的原句。保存业务草稿后可在正式记录中追溯。预览本身只产生建议。",
        "示例原文为“货件等待入仓。主图已交第二版。”。第一句可选备货与库存，第二句可选 Listing 与素材。经办人分别选对环节、核实货件号与素材版本，再决定是否保存或送审。",
    ])
    c.heading("店铺级 Case：无需关联 SKU")
    c.steps([
        "在「店铺与账户」新建 Case 办理单，将「办理范围」设为「按店铺办理」。选已有店铺连接带入账号、站点、经营时区；手工登记时填写可识别的店铺账号、站点代码和有效 IANA 时区。业务对象填平台 Case 号。",
        "店铺级核算沿用该账号、站点与经营时区，关联产品 SOP 只适用于产品范围。无产品时不附加产品销售或库存指标；需补某个 SKU 的资料时，另开有明确型号的产品事项。",
        "回到店铺队列可用单据范围筛选店铺级记录；建立后续工作时沿用相同店铺和站点。真店授权和数据覆盖按各来源状态另行核对。",
    ])
    c.heading("建立父子业务单")
    c.steps([
        "打开已保存的业务单，展开「安排后续工作」。指定目标工作台、后续环节和具体对象后点击「建立关联草稿」。例如从货件异常安排成本与利润的对账跟进，填入同一范围内的结算流水号。",
        "确认目标单内容后保存。保存成功后，子单可通过「来源单」进入上游，父单可通过「后续单」查看下游的实际办理状态。两个业务单各有经办人、审核人、版本和执行凭证。",
        "同一父单、同一目标页与环节、同一业务对象已有未归档后续单时，重复保存会返回已有记录。同一经营线索关联的在办单也按来源和对象核对；若关联范围冲突，刷新并核对现有记录。不同环节或不同对象可以各自立单。",
        "创建后续单时继承来源范围并核对对象。业务单的父子关系用于追溯交接，与原 SOP 的前置依赖和正式阶段流转分别办理。要审批任务本身，打开「查看关联 SOP」。",
    ])
    return c


def calculations(pages):
    c = Chapter("calculations", "正式核算、专用算法与 AI 草稿")
    c.text("点击「核算与办理建议」，正式后端依据表单定义校验字段、执行条件检查与适用的专用计算，并列出已有事实、待补项及人工决定。表单试算值的来源是经办人填写的输入。选定产品和期间的后端经营指标另外列出，须查阅其单位、状态、来源和覆盖。")
    c.text("产品事项按产品配置的站点和业务时区取数。手填站点应与所选产品一致。日期未另选时，库存使用业务当天为截止日，其他已接入的指标通常截至前一日，默认起日为截止日前 27 天。输入某条广告、货件或订单编号不会自动把产品汇总切成对应对象粒度。")
    c.text("「请 AI 起草」由操作者主动触发，正式环境才显示。模型收到业务表单类型、当前范围、核算与检查摘要及所提供的经营指标。模型未配置或暂时不可用时，仍可查看规则结果。建议与模型草稿均供人核对，审核与平台执行另有独立动作。")
    c.heading("按办理表查专用计算")
    c.text("以下案例为同范围、同口径的演练数字，金额采用示例原币。手工情景的可选输入全部空白时，部分计算会略过；开始填写后应补足前置依据。未核实的数据留空，只有已确认的零值才填 0。")
    data = [
        ("inventory", "replenish", "日均销量＝完整日销量÷天数；触发线＝日均销量×（交期＋安全天数）；净缺口＝日均销量×（交期＋安全天数＋复查周期）－可售－周期内确认在途。正缺口先提高至起订量，再按箱规向上取整，非正缺口为零。",
         "10 天售 100 件，可售 120 件，已确认在途 20 件，交期 20 天、安全 5 天、复查 5 天；起订 100，每箱 24。日均 10、触发线 250、净缺 160、建议 168 件；现货覆盖 12 天。",
         "核实完整销售日及新鲜快照。天数、起订量、箱规须为正整数；未确认到货的在途不作可抵用量。零销量时覆盖天数无法估计。"),
        ("inventory", "receiving", "货件收发核对＝实收－发运；货件短收率＝最大值（发运－实收，0）÷发运×100%。",
         "同货件发运 100、实收 93：差额 −7 件、短收率 7%。若实收 103，保留超收 3 件，短收率为 0%。",
         "发运量须大于零；按同一货件去重累计整件数，缺发运或实收任一项则补回执。"),
        ("finance", "budget_cash", "期末预测现金＝期初现金＋预计流入－计划流出。",
         "同期间 USD 期初 1,000，预计流入 200、流出 1,350，预测余额 −150 USD，提示复核资金缺口。",
         "币种代码须为三位大写字母；流入流出缺一不可，负的期初余额允许录入。"),
        ("finance", "contribution", "广告前已知成本贡献＝订购销售额－已匹配成本；已知成本盈亏平衡 ACOS＝广告前贡献÷订购销售额；扣已核广告花费贡献＝广告前贡献－已核花费。",
         "同币种收入 1,000、已匹配成本 700，10 件售出且全部匹配，广告费 100 且范围已核：广告前贡献 300、盈亏平衡 ACOS 30%、扣已核花费后贡献 200。",
         "销量未全部匹配或币种缺失时不出贡献。收入为零不出盈亏平衡比例；缺花费或覆盖说明不出扣广告后指标，结算与退款等费用另核。"),
        ("service", "returns", "同一销售批次退款订单率＝去重退款订单数÷批次订单数；退货件数率＝确认退货件数÷批次销售件数。",
         "同批 100 单、120 件；4 个退款订单、6 件退货，分别为 4% 和 5%。",
         "需先确认同批次、范围及截止日。每种比率独立校验分母、去重数量和非负整数；分母为零或分子超过分母时该指标不可用。"),
        ("service", "voc", "主因占比＝该主因事项数÷同批已归类总数×100%。",
         "同批 50 条已归类售后事项，每条只计一个主因，其中 12 条包装破损，主因占比为 24%。",
         "需确认同批去重且单一主因。已归类总数为零、主因数超出总数或口径不清时，补齐分类后再算。结果描述已归类样本组成。"),
        ("ranking", "rank_review", "可比排名变化＝前次位次－本次位次，正数表示上升。",
         "同一序列连续两次有效自然位观测从 31 变为 23，上升 8 位。",
         "两点必须同对象、同类型与同条件版本，位次为大于零的整数；无效、缺测或改范围时重建基线。"),
        ("ads", "investigation", "成熟 ACOS＝花费÷归因销售额×100%；目标差额＝成熟 ACOS－已批目标，单位为百分点。",
         "已成熟并刷新，花费 30、归因销售额 100、目标 25%，差额为 +5 个百分点，交经办人查词、价格、库存与样本。",
         "归因未成熟、口径或目标缺失时先复查。若有花费但归因销售额为零，提示核查转化并标明 ACOS 无定义。"),
        ("calendar", "readiness", "现货可承接预测贡献＝预测量与可售量的较小值×（活动单价－单件已知成本）。",
         "预测 100 件，可售 80，活动价 20、已知单件成本 14，承接贡献为 480；库存少于预测，需复核承接能力。",
         "缺预测、可售、同范围价本或批准底线依据时先补资料。该估算只覆盖已提供的成本和现货。"),
        ("calendar", "pricing_track", "距底价余量＝叠加后到手价－已批准底价；可折让空间＝（参考售价－已批准底价）÷参考售价×100%。",
         "参考价 100、叠加到手价 72、已批准底价 75；余量 −3、可折让空间 25%，请核对叠加与审批。",
         "统一币种和含税口径；参考价为零时比例无定义，参考价已低于底价时提示复核价格依据。"),
    ]
    for page_id, section_id, formula, example, missing in data:
        page = pages[page_id]
        section = next(s for s in page["sections"] if s["id"] == section_id)
        c.heading(page["title"] + " · " + section["title"])
        c.text(formula)
        c.table(("示例输入与结果", "缺失、异常及处理"), [(example, missing)])
    return c


def sop_chapter(pages, catalog):
    tasks = sorted(catalog["tasks"], key=lambda t: t["sequence"])
    if len(tasks) != 79 or len({t["key"] for t in tasks}) != len(tasks):
        raise ValueError("请审查 SOP 目录变动后同步更新手册范围。")
    c = Chapter("sop-index", "附录 · 79 项 SOP 到办理入口的索引")
    c.text("按模板稳定键或事项名检索。标准事项在每个产品下生成独立任务实例；到对应专业页办理业务单，再在同一产品的原审批单推进阶段并验收。复盘类识别为「Agent 决策中心」的工作范围。")
    task_by_key = {t["key"]:t for t in tasks}
    rows = []
    for t in tasks:
        target = "agent" if t["module"] == "review" else t["module"]
        files = t.get("delivery", {}).get("expected_files") or []
        deliverable = "、".join(files) if files else t["acceptance_criteria"]
        dependencies = "、".join(task_by_key[key]["title"] for key in t.get("depends_on", []))
        rows.append((str(t["sequence"]).zfill(2), t["key"], t["title"], t["phase"] + " / " + t["department"],
                     pages[target]["title"], deliverable, dependencies or "未列模板前置"))
    c.table(("序号", "模板稳定键", "原 SOP 事项", "阶段 / 部门", "专业办理页", "预期交付", "模板前置"), rows)
    return c


def escape_cell(x):
    return str(x).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def markdown(chapters):
    lines = ["# " + TITLE, "", "供运营、审核人、产品和工程团队使用。", "", "## 目录", ""]
    lines.extend(f"- [{c.title}](#{c.anchor})" for c in chapters)
    for c in chapters:
        lines.extend(["", f'<a id="{c.anchor}"></a>', "", "## " + c.title, ""])
        for kind, v in c.blocks:
            if kind == "p":
                lines.append(v)
            elif kind == "h":
                lines.append("### " + v)
            elif kind == "steps":
                lines.extend(f"{i}. {s}" for i,s in enumerate(v, 1))
            else:
                headers, rows = v
                lines.append("| " + " | ".join(headers) + " |")
                lines.append("| " + " | ".join("---" for _ in headers) + " |")
                lines.extend("| " + " | ".join(escape_cell(x) for x in row) + " |" for row in rows)
            lines.append("")
    return "\n".join(lines) + "\n"


def html_document(chapters):
    esc = html.escape
    sections, nav = [], []
    for c in chapters:
        parts = [f'<section id="{c.anchor}"><h2>{esc(c.title)}</h2>']
        nav.append(f'<a data-anchor="{c.anchor}" href="#{c.anchor}">{esc(c.title)}</a>')
        for kind, v in c.blocks:
            if kind == "p":
                parts.append(f"<p>{esc(v)}</p>")
            elif kind == "h":
                parts.append(f"<h3>{esc(v)}</h3>")
            elif kind == "steps":
                parts.append("<ol>" + "".join(f"<li>{esc(s)}</li>" for s in v) + "</ol>")
            else:
                headers, rows = v
                parts.append('<div class="scroll"><table><thead><tr>' + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr></thead><tbody>")
                for row in rows:
                    parts.append("<tr>" + "".join(f"<td>{esc(str(cell))}</td>" for cell in row) + "</tr>")
                parts.append("</tbody></table></div>")
        parts.append("</section>")
        sections.append("".join(parts))
    return HTML.format(title=TITLE, nav="".join(nav), sections="\n".join(sections))


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
* {{ box-sizing: border-box }}
:root {{ font-family: system-ui, -apple-system, sans-serif; color: #202e39; line-height: 1.65 }}
body {{ margin: 0; background: #fff }}
header {{ background: #173949; color: white; padding: 24px max(20px, 5vw) }}
h1 {{ font-size: clamp(1.6rem, 3vw, 2.25rem); margin: 0 }}
main {{ display: grid; grid-template-columns: 208px minmax(0, 1fr); gap: 36px; max-width: 1560px; padding: 28px 4vw; margin: auto }}
aside {{ position: sticky; top: 10px; max-height: 95vh; overflow: auto; align-self: start }}
nav a {{ display: block; padding: 3px 1px; font-size: .9rem; color: #175769 }}
label {{ font-weight: 600 }}
input {{ display: block; width: 100%; padding: 7px; margin-bottom: 5px }}
section {{ scroll-margin: 18px; border-bottom: 1px solid #c6d3db; padding-bottom: 18px }}
h2 {{ color: #125165; margin: 12px 0 }}
h3 {{ font-size: 1.05rem; margin: 24px 0 8px }}
.scroll {{ overflow-x: auto; margin: 8px 0 18px }}
table {{ width: 100%; border-collapse: collapse; font-size: .88rem }}
td, th {{ padding: 8px 10px; text-align: left; vertical-align: top; border: 1px solid #cad6db }}
th {{ background: #eef4f4; white-space: nowrap }}
td {{ min-width: 5em }}
p {{ margin: .6em 0 }}
li {{ margin: .4em 0 }}
[hidden] {{ display: none !important }}
@media (max-width: 760px) {{
  main {{ display: block; padding: 15px }}
  aside {{ position: static; max-height: none }}
  nav {{ display: flex; flex-wrap: wrap; gap: 4px 10px }}
}}
@media print {{
  aside {{ display: none }}
  main {{ display: block; padding: 0 }}
  .scroll {{ overflow: visible }}
  tr {{ break-inside: avoid }}
  h3 {{ break-after: avoid }}
  th {{ white-space: normal }}
}}
</style>
</head>
<body>
<header><h1>{title}</h1></header>
<main>
  <aside>
    <label for="q">检索手册</label>
    <input id="q" type="search" placeholder="输入页面、字段或 SOP 编号">
    <p id="hits" role="status"></p>
    <nav aria-label="章节目录">{nav}</nav>
  </aside>
  <article>{sections}</article>
</main>
<script>
const searchBox = document.getElementById('q');
const entries = Array.from(document.querySelectorAll('article section'), section => ({{
  section,
  text: section.textContent.toLowerCase(),
  link: document.querySelector('nav [data-anchor="' + section.id + '"]'),
}}));
searchBox.addEventListener('input', () => {{
  const terms = searchBox.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  let matches = 0;
  for (const entry of entries) {{
    const found = terms.every(term => entry.text.includes(term));
    entry.section.hidden = !found;
    entry.link.hidden = !found;
    if (found) matches++;
  }}
  document.getElementById('hits').textContent = terms.length ? '匹配 ' + matches + ' 个章节' : '';
}});
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="仅核对已提交手册是否与当前定义一致")
    args = parser.parse_args()
    pages = load_pages()
    catalog = json.loads((FRONT / "catalog.json").read_text(encoding="utf-8"))
    chapters = [quick_start(), intake_and_handoff(), lifecycle(), calculations(pages)]
    chapters += [operation_page(pages[page_id]) for group in GROUPS.values() for page_id in group]
    chapters.append(sop_chapter(pages, catalog))
    coverage = Chapter("validation", "验收记录与环境")
    coverage.text("各页验收表是待复核的情景与预期。实际运行过的用例、测试环境、结果和未验证项目，请按仓库本次实测文档逐项查证。真实店铺授权、平台写入、外部模型和生产部署的结论须由对应环境提供证据。")
    coverage.text("店铺级单据已在 PostgreSQL 测试环境核验两个店铺的范围区分及业务单办理闭环。该项验证针对测试账号和数据库；浏览器操作、真店与生产接入各依对应记录核对。")
    coverage.text("仓库文档路径　docs/workbench-validation.md")
    chapters.append(coverage)
    md = markdown(chapters) + "\n[本次实际测试记录](workbench-validation.md)\n"
    artifacts = {DOCS / "运营工作台执行手册.md": md, DOCS / "workbench" / "运营工作台使用手册.html": html_document(chapters)}
    for path, content in artifacts.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"手册需要重新生成：{path.relative_to(ROOT)}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print("手册校验完成" if args.check else "已生成 Markdown 与离线 HTML 手册")


if __name__ == "__main__":
    main()
