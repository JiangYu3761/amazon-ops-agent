"""A scoped work queue and evidence-based draft assistant. Neither changes work records."""
from datetime import date, datetime, timedelta, timezone
from heapq import nsmallest
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .data_automation import _product, load_product_analytics
from .db import connection
from .llm import LLMNotConfigured, ask_external_llm
from .workbench_definitions import page_definition, section_definition, validate_values
from .workbench_rules import blank, evaluate_checks
from .workbench_calculations import calculate_section
from .workflow_auth import require_user
from .workbench_scope import resolve_shop_scope

router = APIRouter(prefix="/api/workbench", tags=["Work queue and decision support"])
METRICS = {
    "inventory": ("daily_sales_rate", "days_of_supply", "recommended_replenishment"),
    "ads": ("acos", "ctr", "roas"), "finance": ("contribution_before_ads", "contribution_after_ads"),
    "listing": ("sessions", "unit_session_percentage", "order_conversion_rate"),
    "shop": ("ordered_revenue", "units_ordered"),
    "calendar": ("events_in_period",), "ranking": ("rank_observations",),
}
STATUS_NEXT = {"draft": "经办人补齐资料", "pending_review": "指定审核人审批",
               "approved": "经办人执行并登记凭证", "executed": "指定审核人复核结果"}


class AssistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: UUID | None = None
    connection_id: UUID | None = None
    account_ref: str = Field(default="", max_length=160)
    business_timezone: str = Field(default="", max_length=80)
    page_id: str = Field(min_length=1, max_length=60)
    section_id: str = Field(min_length=1, max_length=80)
    values: dict = Field(default_factory=dict, max_length=100)
    site: str = Field(default="", max_length=120)
    object_ref: str = Field(default="", max_length=300)
    start_date: date | None = None
    end_date: date | None = None
    use_model: bool = False


def business_today(product):
    return datetime.now(timezone.utc).astimezone(ZoneInfo(product["business_timezone"])).date()


def build_brief(page, section, values, site, object_ref, today, analytics=None):
    labels = {f["key"]: f["label"] for f in section["fields"]}
    missing = [f["label"] for f in section["fields"] if f.get("required") and blank(values.get(f["key"]))]
    if not site.strip():
        missing.append("站点")
    if not object_ref.strip():
        missing.append("办理对象")
    checks = evaluate_checks(section, values, today)
    issues = [r["detail"] for r in checks if r["level"] == "block"]
    calculations = calculate_section(page["id"], section["id"], values, today)
    for result in calculations:
        if "value" in result:
            result["detail"] = f"核算结果 {result['value']}。{result['detail']}"
    findings = checks + calculations
    metrics = []
    if analytics:
        module = "review" if page["id"] == "ranking" else page["id"]
        for m in analytics["modules"].get(module, {}).get("metrics", []):
            if m["key"] in METRICS.get(page["id"], ()):
                metrics.append({k: m[k] for k in ("label", "key", "value", "status", "unit", "reason", "formula")})
    scope = " / ".join(x for x in [site.strip(), object_ref.strip()] if x) or "待指定范围"
    facts = [f"{labels[k]}：{str(v)[:220]}" for k, v in values.items() if k in labels and not blank(v)]
    pending = list(dict.fromkeys(missing + issues))
    summary = f"{scope} · {section['title']}：" + ("有待补或需修正的依据" if pending else "已整理当前填写依据，待岗位人员确认")
    lines = [summary, "已有事实：" + ("；".join(facts) if facts else "尚未录入业务事实")]
    if findings:
        lines.append("核算与检查：" + "；".join(c["label"] + "，" + c["detail"] for c in findings))
    if pending:
        lines.append("办理前需补：" + "、".join(pending))
    lines += ["本单交付：" + section["output"], "人工决定：" + page["ai"]["human"]]
    analysis_status = "needs_evidence" if any(f["level"] in ("block", "missing") for f in findings) else (
        "review_warnings" if any(f["level"] in ("warning", "overdue") for f in findings) else "evaluated")
    return {"status": "incomplete" if pending else "ready", "analysis_status": analysis_status, "summary": summary, "findings": findings,
            "missing": pending, "suggestion": "\n".join(lines), "requires_human": True,
            "source": "当前表单与版本化业务规则", "metrics": metrics,
            "metric_scope": "所选产品及期间汇总，表单中的对象编号不会改变经营报表粒度" if metrics else "",
            "period": analytics["period"] if analytics else None, "as_of": today.isoformat(),
            "model_status": "not_requested", "model_draft": None}


@router.post("/assist")
async def assist(body: AssistRequest, actor=Depends(require_user)):
    page = page_definition(body.page_id)
    section = section_definition(body.page_id, body.section_id)
    validate_values(section["fields"], body.values)
    analytics = None
    if body.product_id is None:
        if body.page_id != "shop":
            raise HTTPException(422, "店铺级事项须指定店铺账号及站点，其他板块须指定产品")
        with connection() as conn:
            account, site, zone = resolve_shop_scope(conn, body.account_ref, body.site, body.business_timezone, body.connection_id)
        body = body.model_copy(update={"account_ref": account, "site": site, "business_timezone": zone})
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).date()
    else:
        with connection() as conn:
            product = _product(conn, body.product_id)
            today = business_today(product)
            if body.site.strip() and body.site.strip().casefold() != product["marketplace"].casefold():
                raise HTTPException(422, "所选站点与产品范围不符")
            if body.page_id in METRICS:
                end = body.end_date or (today if body.page_id == "inventory" else today - timedelta(days=1))
                start = body.start_date or end - timedelta(days=27)
                analytics = load_product_analytics(conn, body.product_id, start, end)
    result = build_brief(page, section, body.values, body.site, body.object_ref, today, analytics)
    if body.account_ref and body.product_id is None:
        result["summary"] = body.account_ref + " · " + result["summary"]
    if not body.use_model:
        return result
    context = {"page": page["title"], "business_form": section["title"], "scope": [body.site, body.object_ref],
               "facts_and_checks": result["suggestion"], "calculated_metrics": result["metrics"],
               "metric_scope": result["metric_scope"], "period": result["period"]}
    try:
        draft, model = await ask_external_llm(page["ai"]["example"] + "。按已填表单起草，缺失事实列为待补，不虚构执行结果。", context, page["id"])
        if not isinstance(draft, str) or not draft.strip():
            raise ValueError("empty response")
        result.update(model_status="generated", model_draft=draft, model=model)
    except LLMNotConfigured:
        result["model_status"] = "not_configured"
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        result["model_status"] = "unavailable"
    return result


def ranked_item(row, now):
    local_day = now.astimezone(ZoneInfo(row["business_timezone"])).date()
    deadline = None
    for check in row["definition"].get("checks", []):
        if check["op"] == "deadline":
            raw = row["values"].get(check["keys"][0])
            if raw:
                try:
                    day = date.fromisoformat(raw)
                    deadline = min(deadline, day) if deadline else day
                except (ValueError, TypeError):
                    continue
    days = (deadline - local_day).days if deadline else None
    if days is not None and days < 0:
        priority, reason = 0, f"已超期 {-days} 天"
    elif days == 0:
        priority, reason = 1, "今天到期"
    elif row["status"] in ("pending_review", "executed"):
        priority, reason = 2, STATUS_NEXT[row["status"]]
    else:
        priority = 3
        reason = f"距期限 {days} 天" if days is not None else STATUS_NEXT[row["status"]]
    result = {k: v for k, v in row.items() if k not in ("definition", "values", "business_timezone")}
    result.update(priority=priority, due_date=deadline, reason=reason)
    return result


def operating_signals(analytics, product):
    signals = []
    def add(page, section, obj, code, title, reason, priority, values=None):
        signals.append({"page_id": page, "section_id": section, "site": product["marketplace"],
                        "product_id": product["id"], "object_ref": obj, "signal_key": f"{page}/{code}/{obj}",
                        "title": title, "reason": reason, "priority": priority, "values": values or {},
                        "source_note": f"{reason} 经营期间 {analytics['period']['start_date']} 至 {analytics['period']['end_date']}，"
                        f"业务时区 {analytics['period']['timezone']}，参数版本 {analytics.get('policy_version', 0)}。"})
    inv = analytics["modules"]["inventory"]
    rec = next((m for m in inv["metrics"] if m["key"] == "recommended_replenishment"), {})
    if rec.get("status") in ("ready", "estimated"):
        for sku in rec["inputs"]["per_sku"]:
            if sku["recommended_units"] > 0:
                add("inventory", "replenish", sku["sku"], "replenish",
                    "核实补货需求", f"可售 {sku['fulfillable']} 件，周期内有 ETA 的在途 {sku['reliable_inbound']} 件，建议量 {sku['recommended_units']} 件。", 1,
                    {"suggested_qty": sku["recommended_units"], "requested_qty": sku["recommended_units"], "adjustment": "按建议量"})
    obj = product.get("model") or str(product["id"])
    for finding in analytics["modules"]["ads"].get("findings", []):
        if finding["type"] == "mature_acos_above_target":
            add("ads", "investigation", obj, "acos", finding["title"], finding["action"], 2)
    for issue in analytics["data_quality"]["issues"]:
        if issue["code"] in ("missing_effective_cost", "stale_inventory", "incomplete_days", "missing_inventory_skus"):
            add("imports", "source_coverage", issue["dataset"], issue["code"],
                "核查数据来源", issue["message"], 3)
    return sorted(signals, key=lambda x: (x["priority"], x["object_ref"], x["signal_key"]))


@router.get("/inbox")
def inbox(product_id: UUID | None = None, mine: bool = False, limit: int = Query(default=100, ge=1, le=500),
          actor=Depends(require_user)):
    now = datetime.now(timezone.utc)
    clauses = ["w.status <> 'closed'"]
    params = []
    if product_id:
        clauses.append("w.product_id = %s")
        params.append(product_id)
    if mine:
        clauses.append("CASE WHEN w.status IN ('pending_review','executed') THEN w.reviewer_id ELSE w.owner_id END = %s")
        params.append(actor["id"])
    sql = """SELECT w.id,w.number,w.product_id,w.page_id,w.section_id,w.site,w.object_ref,w.title,
        w.owner_id,w.reviewer_id,w.task_id,w.status,w.version,w.updated_at,w.signal_key,
        w.values,w.definition,w.account_ref,COALESCE(w.business_timezone,p.business_timezone) business_timezone
        FROM workbench_items w LEFT JOIN products p ON p.id=w.product_id
        WHERE """ + " AND ".join(clauses)
    signals = []
    with connection() as conn:
        if product_id:
            product = _product(conn, product_id)
            analytics = load_product_analytics(conn, product_id)
            signals = operating_signals(analytics, product)
            def scope_key(record):
                return tuple(record[k] for k in ("signal_key", "page_id", "section_id", "object_ref"))
            open_keys = {scope_key(r) for r in conn.execute(
                "SELECT signal_key,page_id,section_id,object_ref FROM workbench_items WHERE product_id=%s AND signal_key IS NOT NULL AND status<>'closed'", (product_id,))}
            signals = [s for s in signals if scope_key(s) not in open_keys]
        with conn.cursor(name="workbench_inbox") as cur:
            cur.execute(sql, params)
            ranked = (ranked_item(r, now) for r in cur)
            first = nsmallest(limit + 1, ranked, key=lambda r: (r["priority"], r["due_date"] or date.max, r["updated_at"], str(r["id"])))
    return {"items": first[:limit], "truncated": len(first) > limit, "signals": signals,
            "as_of": now, "signal_scope": "所选产品" if product_id else "选择产品后显示对应的经营线索"}
