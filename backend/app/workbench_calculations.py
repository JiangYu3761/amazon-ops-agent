"""Scoped calculations over the declared form inputs; no persistence or external action."""
from decimal import Decimal, DecimalException, ROUND_CEILING
import re

from .workbench_rules import blank, display_number, typed


def finding(label, level, detail, fields, value=None):
    result = {"label": label, "level": level, "detail": detail, "fields": list(fields)}
    if value is not None:
        result["value"] = display_number(value)
    return result


def numeric(values, keys, *, counts=False, signed=()):
    numbers = []
    for key in keys:
        value = values.get(key)
        if blank(value) or isinstance(value, bool):
            return None
        try:
            n = typed(value, "number")
        except (ValueError, TypeError, DecimalException):
            return None
        if (key not in signed and n < 0) or (counts and n != n.to_integral_value()):
            return None
        numbers.append(n)
    return numbers


def currency(values):
    code = values.get("currency")
    return code if isinstance(code, str) and re.fullmatch("[A-Z]{3}", code) else None


def replenishment(v):
    keys = ["sold_qty", "closed_days", "available_qty", "eligible_inbound", "lead_days", "safety_days",
            "review_days", "min_order", "pack_size"]
    basis = ["scenario_basis", *keys]
    if all(blank(v.get(k)) for k in basis):
        return []
    n = numeric(v, keys, counts=True)
    if v.get("scenario_basis") != "完整销售日及新鲜快照已核实" or n is None:
        return [finding("补货情景", "missing", "完整销售日、快照、确认在途、交期、安全天数、复查周期、MOQ 和整箱参数须以同一 SKU 核实。", basis)]
    sold, days, available, incoming, lead, safety, review, moq, pack = n
    if not days or not pack or not moq:
        return [finding("补货情景", "missing", "完整天数、MOQ 和每箱件数必须为正整数。", ["closed_days", "min_order", "pack_size"])]
    rate = sold / days
    # Keep integer demand numerators until rounding, especially at exact pack multiples.
    demand_numerator = sold * (lead + safety + review) - (available + incoming) * days
    qty = Decimal(0)
    if demand_numerator > 0:
        min_numerator = max(demand_numerator, moq * days)
        denominator = days * pack
        qty = (min_numerator / denominator).to_integral_value(rounding=ROUND_CEILING) * pack
    findings = [
        finding("手工情景日均销量", "info", "已核实完整日销量 ÷ 已结束天数，单位为件/天。", keys[:2], rate),
        finding("手工情景补货触发线", "info", "日均销量 ×（提前期＋安全天数），单位为件。", keys, rate * (lead + safety)),
        finding("手工情景补货量", "info", "目标周期需求减可售及周期内确认在途，正缺口按 MOQ、箱规向上取整。单位为件；供审批前复核。", basis, qty),
    ]
    if rate == 0:
        findings.append(finding("现货覆盖天数", "missing", "已确认销量为零，无法用均速推算售罄日期。", keys[:3]))
    else:
        findings.append(finding("现货覆盖天数", "warning" if available / rate < lead else "info",
                                "可售 ÷ 日均销量；早于常规补货到货时核查加急或调拨。", keys[:3] + ["lead_days"], available / rate))
    return findings


def receiving(v):
    keys = ["shipped_qty", "received_qty"]
    n = numeric(v, keys, counts=True)
    if n is None or not n[0]:
        return [finding("货件短收率", "missing", "须有同一货件的发运及实收整件数，发运量须大于零。", keys)]
    sent, received = n
    signed = received - sent
    return [finding("货件收发核对", "warning" if signed else "ok",
                    f"实收减发运为 {display_number(signed)} 件，按同一货件回执累计去重；超收另行核查。", keys, signed),
            finding("货件短收率", "info", "短收件数 ÷ 发运件数 × 100%；超收时短收为零，保留收发差额。", keys, max(-signed, 0) * 100 / sent)]


def cash(v):
    keys = ["opening_cash", "incoming_cash", "outgoing_cash"]
    if all(blank(v.get(k)) for k in keys):
        return []
    n = numeric(v, keys, signed={"opening_cash"})
    unit = currency(v)
    if n is None or not unit:
        return [finding("期末预测现金", "missing", "需填写同币种、同一预测期间的期初现金、流入与流出。", ["currency", *keys])]
    balance = n[0] + n[1] - n[2]
    return [finding("期末预测现金", "warning" if balance < 0 else "info",
                    f"期初现金＋预计回款−计划付款，单位 {unit}；负值交财务复核资金缺口与时间点。", keys, balance)]


def contribution(v):
    keys = ["ordered_revenue", "matched_cost", "sold_units", "matched_units"]
    if all(blank(v.get(k)) for k in ["currency", *keys[:2], "ad_spend"]):
        return []
    n = numeric(v, keys[:2])
    units = numeric(v, keys[2:], counts=True)
    unit = currency(v)
    if not n or not units or not unit or units[0] != units[1]:
        return [finding("同期间已知成本贡献", "missing", "同币种收入、逐销售日成本及完整销量匹配缺一不可；未匹配成本不按零计算。", ["currency", *keys])]
    revenue, cost = n
    amount = revenue - cost
    output = [finding("广告前已知成本贡献", "info", f"订购销售额减已匹配成本，单位 {unit}，不含未覆盖费用。", keys, amount)]
    if revenue > 0:
        output.append(finding("已知成本盈亏平衡 ACOS", "warning" if amount < 0 else "info",
                              "广告前贡献 ÷ 订购销售额 × 100%，负值表示广告前已亏损，仅供预算调查。", keys, amount * 100 / revenue))
    else:
        output.append(finding("已知成本盈亏平衡 ACOS", "missing", "销售额为零，比例无定义。", keys))
    spend = numeric(v, ["ad_spend"])
    if spend is None or blank(v.get("expense_scope")):
        output.append(finding("扣已核广告花费贡献", "missing", "补齐同币种花费及渠道/期间覆盖；已确认为零请填 0。", ["ad_spend", "expense_scope"]))
    else:
        output.append(finding("扣已核广告花费贡献", "info", f"广告前贡献减已核广告花费，单位 {unit}。退款、税费、仓储、期间费用及实际结算须独立核对。", [*keys, "ad_spend", "expense_scope"], amount - spend[0]))
    return output


def cohort_returns(v):
    keys = ["cohort_orders", "refund_orders", "cohort_units", "returned_units"]
    if all(blank(v.get(k)) for k in ["cohort_match", *keys]):
        return []
    if v.get("cohort_match") != "已核实同批次":
        return [finding("售后批次比率", "missing", "须核实店铺、站点、SKU、销售批次及截止日匹配，按订单与按件两种分母分别处理。", ["cohort_match", *keys])]
    output = []
    for label, unit, fields in [("批次退款订单率", "去重的已退款订单数 ÷ 批次订单数", keys[:2]),
                                ("批次退货件数率", "去重的确认退货件数 ÷ 批次销售件数", keys[2:])]:
        n = numeric(v, fields, counts=True)
        if n is None or not n[0] or n[1] > n[0]:
            output.append(finding(label, "missing", "需要非零同批次分母及不超过该分母的有效去重数量。", fields))
        else:
            output.append(finding(label, "info", unit + " × 100%，不把工单量或退款笔数用作订单数。", fields, n[1] * 100 / n[0]))
    return output


def reasons(v):
    keys = ["reason_count", "classified_count"]
    if all(blank(v.get(k)) for k in ["reason_basis", *keys]):
        return []
    n = numeric(v, keys, counts=True)
    if v.get("reason_basis") != "同批去重且单一主因" or n is None or not n[1] or n[0] > n[1]:
        return [finding("售后主因占比", "missing", "先统一批次、去重且每条只计一个主因；已归类总数须大于零。", ["reason_basis", *keys])]
    return [finding("售后主因占比", "info", "该主因事项数 ÷ 已归类总数 × 100%，描述已归类样本构成，不能推为全体订单故障率。", keys, n[0] * 100 / n[1])]


def rank(v):
    fields = ["before_series", "after_series", "pair_state", "old_rank", "new_rank"]
    positions = numeric(v, fields[-2:], counts=True)
    if (blank(v.get("before_series")) or v.get("before_series") != v.get("after_series")
            or v.get("pair_state") != "连续有效观测" or not positions or min(positions) < 1):
        return [finding("可比排名变化", "missing", "请核对同一对象、类型及条件版本的两个连续有效点。缺测保留断点，范围改变重建基线。", fields)]
    diff = positions[0] - positions[1]
    return [finding("可比排名变化", "info", "前次位次减本次位次；正为上升、负为下降，归因前须按相同条件复测。", fields, diff)]


def advertising(v):
    fields = ["maturity", "spend", "attributed_sales", "target_acos"]
    n = numeric(v, fields[1:])
    if v.get("maturity") != "已成熟并刷新":
        return [finding("广告目标调查", "missing", "归因窗仍开放或刷新证据不足；记录复查日，暂不评判目标达成。", fields)]
    if n is None:
        return [finding("广告目标调查", "missing", "需有同一报表/币种/粒度的花费、归因销售额及获批目标百分比。", fields)]
    spend, sales, target = n
    if not sales:
        return [finding("广告目标调查", "warning" if spend else "missing", "归因销售额为零，ACOS 无定义；若发生花费，请核查转化与数据覆盖。", fields)]
    deviation = spend * 100 / sales - target
    return [finding("成熟 ACOS 与目标差额", "warning" if deviation > 0 else "ok",
                    "同报表 ACOS 减获批目标，单位为百分点。超标时调查搜索词、价格、库存和样本，无自动调价或停投。", fields, deviation)]


def event_readiness(v):
    keys = ["forecast_units", "available_units", "promo_price", "unit_cost_total"]
    units = numeric(v, keys[:2], counts=True)
    money = numeric(v, keys[2:])
    if units is None or money is None or blank(v.get("floor_ref")):
        return [finding("活动可承接贡献", "missing", "需有预测与可售件数、同币种同费用口径的活动价/成本，以及已批底线依据。", [*keys, "floor_ref"])]
    forecast, available = units
    margin = money[0] - money[1]
    serviceable = min(forecast, available)
    return [finding("现货可承接预测贡献", "warning" if margin < 0 or available < forecast else "info",
                    "取预测与现货较小件数 ×（活动单价−单件已知成本）。单位采用表单同一原币；未计未提供费用，分摊与底线需财务复核。", keys, serviceable * margin)]


def price_floor(v):
    keys = ["base_price", "effective_price", "cost_floor"]
    n = numeric(v, keys)
    if n is None:
        return [finding("叠加价格底线", "missing", "应提供同一税价和币种下的参考售价、叠加后到手价与已批底价。", keys)]
    base, effective, floor = n
    spare = effective - floor
    result = [finding("距已批底价余量", "warning" if spare < 0 else "info",
                      "叠加后售价减已审批的最低售价，单位为同一商品原币。负数应核查优惠叠加并提交重新审批。", keys, spare)]
    if not base:
        result.append(finding("参考售价下可折让空间", "missing", "参考售价为零，折让比例无定义。", keys))
    elif base < floor:
        result.append(finding("参考售价下可折让空间", "warning", "参考售价已低于底线，应先复核价格依据。", keys))
    else:
        result.append(finding("参考售价下可折让空间", "info", "（参考售价−已批底价）÷参考售价 × 100%，仅为商品价格约束。", keys,
                              (base - floor) * 100 / base))
    return result


CALCULATORS = {("inventory", "replenish"): replenishment, ("inventory", "receiving"): receiving,
               ("finance", "budget_cash"): cash, ("finance", "contribution"): contribution,
               ("service", "returns"): cohort_returns, ("service", "voc"): reasons,
               ("ranking", "rank_review"): rank, ("ads", "investigation"): advertising,
               ("calendar", "readiness"): event_readiness, ("calendar", "pricing_track"): price_floor}


def calculate_section(page_id, section_id, values, today):
    """Calculate on the current form's scope. today is the product's business date."""
    calculator = CALCULATORS.get((page_id, section_id))
    return calculator(values) if calculator else []
