"""Closed-period store sales from explicitly scoped daily reports; never infer a shop from product-level CSV."""
import csv
import hashlib
import io
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder

from .data_automation import now_utc
from .db import connection
from .erp_comparison import period_bounds
from .erp_refunds import check_amount
from .workflow_auth import require_admin, require_user


router = APIRouter(prefix="/api/erp-shop-sales", tags=["Multi-store sales"])
KPI = ("ordered_revenue", "units_ordered", "orders")
HEADER = ("account_ref", "site", "currency", "date", "report_grain", "coverage", *KPI)
ACCESS = """SELECT s.account_ref,s.site,array_agg(DISTINCT s.business_timezone ORDER BY s.business_timezone) AS zones
    FROM erp_receiving_scopes s WHERE (%(admin)s OR EXISTS (SELECT 1 FROM erp_receiving_members m
    WHERE m.scope_id=s.id AND m.user_id=%(actor)s)) GROUP BY s.account_ref,s.site"""


def permissions(actor):
    return {"admin":actor["is_admin"], "actor":actor["id"]}


def shop_zones(conn, actor):
    return {(r["account_ref"],r["site"]):r["zones"] for r in conn.execute(ACCESS,permissions(actor))}


def local_day(zones):
    instant=now_utc()
    return min(instant.astimezone(ZoneInfo(zone)).date() for zone in zones)


def daily_csv(raw):
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or set(HEADER)-set(reader.fieldnames):
            raise ValueError("请使用多店经营的完整日报模板")
        rows, seen = [], set()
        for index, source in enumerate(reader,2):
            try:
                if None in source or any(source[key] is None for key in HEADER):
                    raise ValueError("数据与表头的列数不一致")
                row = {k:source[k].strip() for k in HEADER}
                if row["report_grain"] != "shop_day" or row["coverage"] not in {"complete","partial"}:
                    raise ValueError("report_grain 须为 shop_day，coverage 须为 complete 或 partial")
                row["site"],row["currency"]=row["site"].upper(),row["currency"].upper()
                if not 1 <= len(row["account_ref"]) <= 120 or not row["account_ref"].isprintable() or not re.fullmatch("[A-Z]{2,4}",row["site"]):
                    raise ValueError("店铺或站点格式有误")
                if not re.fullmatch("[A-Z]{3}",row["currency"]) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",row["date"]):
                    raise ValueError("请填写三位币种和 YYYY-MM-DD 日期")
                row["day"]=date.fromisoformat(row.pop("date"))
                for field in KPI:
                    val=row[field]
                    if not val:
                        row[field]=None
                    elif field=="ordered_revenue":
                        if not re.fullmatch(r"\d{1,14}(?:\.\d{1,4})?",val): raise ValueError("订购销售额格式有误")
                        row[field]=Decimal(val)
                        check_amount(row[field],row["currency"])
                    elif re.fullmatch(r"\d{1,10}",val) and int(val)<=2147483647:
                        row[field]=int(val)
                    else:
                        raise ValueError("件数及订单数须为非负整数，未知可留空")
                if all(row[k] is None for k in KPI):raise ValueError("请提供至少一个有来源的指标")
                key=(row["account_ref"],row["site"],row["currency"],row["day"])
                if key in seen:raise ValueError("文件内重复出现同店铺、站点、币种和业务日期")
                seen.add(key);rows.append(dict(row,line=index))
                if len(rows)>10000:raise ValueError("单次最多 10000 行")
            except ValueError as err:
                raise ValueError(f"第 {index} 行：{err}") from None
        if not rows:raise ValueError("日报未包含数据行")
        return rows
    except (UnicodeError,csv.Error,ValueError) as err:
        raise HTTPException(422,str(err)) from None


@router.post("/imports", status_code=201)
async def import_days(file: UploadFile=File(...),actor=Depends(require_admin)):
    raw=await file.read(3*1024*1024+1)
    if len(raw)>3*1024*1024:raise HTTPException(413,"日报最大 3 MiB")
    rows=daily_csv(raw)
    digest=hashlib.sha256(raw).hexdigest()
    with connection() as conn:
        allowed=shop_zones(conn,actor)
        for r in rows:
            zones=allowed.get((r["account_ref"],r["site"]))
            if not zones:raise HTTPException(404,"日报内存在未登记或不可见的店铺站点")
            if len(zones)!=1:raise HTTPException(422,"该店铺站点的仓库业务时区不一致，请先核对")
            today=local_day(zones)
            if r["day"]>today or (r["coverage"]=="complete" and r["day"]==today):
                raise HTTPException(422,f"第 {r['line']} 行：完整日报须为已结束的业务日期")
            r["zone"]=zones[0]
        batch=conn.execute("""INSERT INTO erp_shop_sales_imports(content_sha256,filename,imported_by)
            VALUES(%s,%s,%s) ON CONFLICT(content_sha256) DO NOTHING RETURNING id""",
            (digest,Path(file.filename or "sales.csv").name[:240],actor["id"])).fetchone()
        if not batch:
            return {"duplicate":True,"rows":len(rows)}
        for r in sorted(rows,key=lambda v:(v["account_ref"],v["site"],v["currency"],v["day"])):
            r["batch"]=batch["id"]
            written=conn.execute("""INSERT INTO erp_shop_sales_rows(import_id,source_line,account_ref,site,currency,
                business_timezone,business_day,coverage,ordered_revenue,units_ordered,orders)
                VALUES(%(batch)s,%(line)s,%(account_ref)s,%(site)s,%(currency)s,%(zone)s,%(day)s,%(coverage)s,
                %(ordered_revenue)s,%(units_ordered)s,%(orders)s) RETURNING id""",r).fetchone()
            conn.execute("""INSERT INTO erp_shop_sales_days(account_ref,site,currency,business_day,row_id)
               VALUES(%(account_ref)s,%(site)s,%(currency)s,%(day)s,%(id)s)
               ON CONFLICT(account_ref,site,currency,business_day) DO UPDATE SET row_id=excluded.row_id""",dict(r,id=written["id"]))
        return {"duplicate":False,"rows":len(rows),"import_id":batch["id"]}


def period_value(group, key, day_count):
    confirmed = group.get(key + "_days", 0)
    return {"value": group.get(key), "complete": confirmed == day_count, "confirmed_days": confirmed}


def comparison(current, previous):
    complete=current["complete"] and previous["complete"]
    delta=rate=None
    if complete:
        delta=current["value"]-previous["value"]
        if previous["value"]!=0:
            rate=(Decimal(delta)*100/abs(Decimal(previous["value"]))).quantize(Decimal(".0001"),rounding=ROUND_HALF_UP)
    return dict(current=current,previous=previous,difference=delta,change_percent=rate,
                status="partial" if not complete else "zero_base" if rate is None else "ready")


@router.get("")
def overview(days:int=Query(7,ge=1,le=366),end_date:date|None=None,actor=Depends(require_user)):
    with connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        allowed=shop_zones(conn,actor)
        today=local_day({z for zones in allowed.values() for z in zones} or {"UTC"})
        previous,current=period_bounds(today,days,end_date)
        # Keep known zero-activity stores even if only older currency history exists.
        params=dict(permissions(actor),start=previous[0],current_start=current[0],end=current[1])
        known=conn.execute(f"""WITH stores AS ({ACCESS}) SELECT DISTINCT d.account_ref,d.site,d.currency
            FROM erp_shop_sales_days d JOIN stores s USING(account_ref,site) WHERE d.business_day<=%(end)s
            ORDER BY d.account_ref,d.site,d.currency""",params).fetchall()
        # Aggregate in PostgreSQL before returning data for arbitrary numbers of shops and days.
        columns = ", ".join(
            f"sum(r.{k}) AS {k}, count(r.{k}) FILTER (WHERE r.coverage='complete') AS {k}_days" for k in KPI
        )
        facts = conn.execute(f"""WITH stores AS ({ACCESS})
            SELECT d.account_ref,d.site,d.currency,d.business_day>=%(current_start)s AS current_period,{columns}
            FROM erp_shop_sales_days d JOIN stores s USING(account_ref,site) JOIN erp_shop_sales_rows r ON r.id=d.row_id
            WHERE d.business_day BETWEEN %(start)s AND %(end)s AND cardinality(s.zones)=1
            AND r.business_timezone=s.zones[1]
            GROUP BY d.account_ref,d.site,d.currency,current_period""", params).fetchall()
    currencies = defaultdict(list)
    for cell in known:
        currencies[cell["account_ref"],cell["site"]].append(cell["currency"])
    grouped = {(r["account_ref"],r["site"],r["currency"],r["current_period"]):r for r in facts}
    stores=[]; groups=defaultdict(list)
    for (account,site),zones in sorted(allowed.items()):
        for currency in currencies[account,site] or [None]:
            periods = [grouped.get((account, site, currency, current_period), {}) for current_period in (True, False)]
            metrics={k:comparison(*(period_value(p,k,days) for p in periods)) for k in KPI}
            stores.append(dict(account_ref=account,site=site,currency=currency,zones=zones,metrics=metrics))
            if currency:groups[currency].append(metrics)
    subtotals=[]
    for currency, group in sorted(groups.items()):
        metrics={}
        for k in KPI:
            periods=[]
            for period in ("current","previous"):
                observations=[r[k][period] for r in group]
                values=[o["value"] for o in observations if o["value"] is not None]
                periods.append(dict(value=sum(values) if values else None,complete=all(o["complete"] for o in observations)))
            metrics[k]=comparison(*periods)
        subtotals.append(dict(currency=currency,stores=len(group),metrics=metrics))
    return jsonable_encoder(dict(days=days,previous=previous,current=current,stores=stores,subtotals=subtotals),
                            custom_encoder={Decimal:lambda d:format(d,"f")})


@router.get("/source")
def source(account_ref:str=Query(min_length=1,max_length=120),site:str=Query(min_length=2,max_length=4),
           currency:str=Query(pattern=r"^[A-Z]{3}$"),start_date:date=Query(),end_date:date=Query(),
           offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),actor=Depends(require_user)):
    if not 0<=(end_date-start_date).days<732:raise HTTPException(422,"来源日期范围须为 1 至 732 天")
    with connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        zones=shop_zones(conn,actor).get((account_ref,site))
        if not zones:raise HTTPException(404,"店铺不存在或不可见")
        rows=conn.execute("""SELECT r.*,b.filename,b.created_at FROM erp_shop_sales_days d JOIN erp_shop_sales_rows r ON r.id=d.row_id
            JOIN erp_shop_sales_imports b ON b.id=r.import_id WHERE d.account_ref=%s AND d.site=%s AND d.currency=%s
            AND d.business_day BETWEEN %s AND %s ORDER BY d.business_day DESC LIMIT %s OFFSET %s""",
            (account_ref,site,currency,start_date,end_date,limit+1,offset)).fetchall()
    for row in rows:
        row["included"]=zones==[row["business_timezone"]]
    return jsonable_encoder(dict(rows=rows[:limit],next_offset=offset+limit if len(rows)>limit else None),
                            custom_encoder={Decimal:lambda d:format(d,"f")})
