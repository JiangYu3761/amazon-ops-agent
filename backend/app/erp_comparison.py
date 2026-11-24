"""Adjacent closed periods from the existing single-product analytics, in one database snapshot."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request



router = APIRouter(prefix="/api")
METRICS = (
    ("shop", "ordered_revenue", "订购销售额"),
    ("shop", "units_ordered", "订购件数"),
    ("shop", "orders", "订单数"),
    ("listing", "sessions", "会话数"),
    ("listing", "page_views", "页面浏览量"),
    ("ads", "spend", "SP 商品广告花费"),
    ("ads", "clicks", "SP 商品广告点击量"),
    ("finance", "contribution_before_ads", "广告前已知成本贡献额"),
)
SOURCES = {"sales": "销售及流量日报", "advertising": "SP 商品广告日报", "costs": "销售日生效 SKU 成本卡"}


def period_bounds(today: date, days: int, end_date: date | None):
    end = end_date or today - timedelta(days=1)
    if not 1 <= days <= 366 or end >= today:
        raise HTTPException(422, "请选择产品业务时区内已结束的 1 至 366 个连续日期")
    try:
        start = end - timedelta(days=days - 1)
        return (start - timedelta(days=days), start - timedelta(days=1)), (start, end)
    except OverflowError:
        raise HTTPException(422, "所选日期无法生成等长上期") from None


def observation(report, module, key):
    metric = next(m for m in report["modules"][module]["metrics"] if m["key"] == key)
    info = {k: metric[k] for k in ("value", "unit", "status", "reason", "formula")}
    if module == "ads":
        groups = metric["inputs"]["groups"]
        declared = report["modules"]["ads"]["scope"]["ad_types_complete"]
        # Only the supported SP advertised-product daily report uses this basis.
        standard_rows = all(g["ad_type"] == "SP" and g["report_grain"] == "advertised_product"
                            and g["attribution_days"] == 7 for g in groups)
        if not standard_rows or (not groups and (declared != ["SP"] or info["value"] is None)):
            info.update(value=None, status="missing", reason="未取得可核对的 SP 商品日报（7 天归因口径）")
    info["sources"] = []
    for dataset in metric["source_datasets"]:
        coverage = report["data_quality"]["coverage"][dataset]
        info["sources"].append({
            "dataset": dataset, "label": SOURCES[dataset], "coverage_source": coverage["source"],
            "complete_days": len(coverage["complete_days"]), "observed_days": len(coverage["observed_days"]),
            "last_synced_at": coverage["last_synced_at"],
        })
    return info


def compare_reports(previous, current):
    scope = current["product"]
    if any(previous["product"].get(k) != scope.get(k) for k in ("id", "currency", "business_timezone")):
        raise HTTPException(409, "两期产品、币种或业务时区不一致，无法合并比较")
    rows = []
    for module, key, label in METRICS:
        current_value, previous_value = observation(current, module, key), observation(previous, module, key)
        observations = (current_value, previous_value)
        row = {"key": key, "label": label, "current": current_value, "previous": previous_value,
               "unit": current_value["unit"], "difference": None, "change_percent": None}
        if any(v["value"] is None for v in observations):
            row.update(status="missing", reason="至少一期缺少该指标的可核对数值")
        elif any(v["status"] not in ("ready", "estimated") for v in observations):
            row.update(status="partial", reason="覆盖或报表口径待完善，当前仅展示两期已观测值")
        else:
            new, old = (Decimal(str(v["value"])) for v in observations)
            change = new - old
            row["difference"] = float(change)
            if old == 0:
                row.update(status="zero_base", reason="上期为零，保留差额，环比无定义")
            else:
                with localcontext() as math:
                    math.prec = 80
                    percent = (change / abs(old) * 100).quantize(Decimal(".0001"), rounding=ROUND_HALF_UP)
                estimated = any(v["status"] == "estimated" for v in observations)
                row.update(change_percent=float(percent), status="estimated" if estimated else "ready",
                           reason="按销售日已知成本估算，仍需核对退款、税费及期间费用" if estimated else "")
        rows.append(row)
    return {"product": scope, "periods": {"previous": previous["period"], "current": current["period"]},
            "rows": rows, "formula": "（本期－上期）÷ |上期| × 100%",
            "reference": "https://www.lingxing.com/help/article/manageBoard",
            "comparison_policy": "HOMA 按产品业务时区取已结束的相邻等长期间。覆盖不完整时保留观测值，"
                                 "上期为零时仅显示差额。商品贡献只纳入已匹配成本。"}


@router.get("/products/{product_id}/analytics/comparison")
def product_comparison(request: Request, product_id: UUID, days: int = Query(7, ge=1, le=366),
                       end_date: date | None = None):
    from .data_automation import _product, load_product_analytics, now_utc
    from .db import connection
    from .workflow_auth import require_user

    require_user(request)
    with connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        product = _product(conn, product_id)
        today = now_utc().astimezone(ZoneInfo(product["business_timezone"])).date()
        previous, current = period_bounds(today, days, end_date)
        return compare_reports(load_product_analytics(conn, product_id, *previous),
                               load_product_analytics(conn, product_id, *current))
