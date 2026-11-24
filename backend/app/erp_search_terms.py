"""Keyword search-term report facts remain separate from product and campaign-level advertising totals."""
from datetime import date, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from .data_automation import _product, now_utc
from .db import connection
from .workflow_auth import require_user


router = APIRouter(prefix="/api/products/{product_id}/erp/search-terms")
SCOPE = """r.product_id=%(product)s AND r.currency=%(currency)s AND r.report_date BETWEEN %(start)s AND %(end)s"""
GROUPS = "r.profile_id,r.currency,r.attribution_days,r.search_term"


def store_source(conn, product_id, row, import_id):
    params = dict(row, product=product_id, batch=import_id)
    previous = conn.execute("""SELECT * FROM erp_search_term_rows WHERE product_id=%(product)s
                            AND business_key=%(business_key)s FOR UPDATE""", params).fetchone()
    conn.execute("""INSERT INTO erp_search_term_rows(product_id,business_key,report_date,profile_id,currency,
        attribution_days,ad_type,campaign_id,campaign_name,ad_group_id,keyword_id,match_type,search_term,
        impressions,clicks,spend,attributed_sales,attributed_orders,import_id,source_line)
        VALUES(%(product)s,%(business_key)s,%(date)s,%(profile_id)s,%(currency)s,%(attribution_days)s,
        %(ad_type)s,%(campaign_id)s,%(campaign_name)s,%(ad_group_id)s,%(keyword_id)s,%(match_type)s,%(search_term)s,
        %(impressions)s,%(clicks)s,%(spend)s,%(sales)s,%(orders)s,%(batch)s,%(source_line)s)
        ON CONFLICT(product_id,business_key) DO UPDATE SET campaign_name=excluded.campaign_name,
        impressions=excluded.impressions,clicks=excluded.clicks,spend=excluded.spend,attributed_sales=excluded.attributed_sales,
        attributed_orders=excluded.attributed_orders,import_id=excluded.import_id,source_line=excluded.source_line""", params)
    return previous


def read_scope(conn, product_id, start_date, end_date):
    product = _product(conn, product_id)
    today = now_utc().astimezone(ZoneInfo(product["business_timezone"])).date()
    end = end_date or today
    try:
        start = start_date or end - timedelta(days=27)
    except OverflowError:
        raise HTTPException(422, "查询日期太早") from None
    if end > today or not 0 <= (end - start).days < 366:
        raise HTTPException(422, "请选择产品时区内过去或今天的 1 至 366 天")
    return dict(product=product_id, currency=product["currency"], start=start, end=end)


@router.get("")
def search_terms(product_id: UUID, start_date: date | None = None, end_date: date | None = None,
                 offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), actor=Depends(require_user)):
    with connection() as conn:
        params = dict(read_scope(conn, product_id, start_date, end_date), limit=limit+1, offset=offset)
        rows = conn.execute(f"""SELECT {GROUPS}, array_agg(DISTINCT ad_type ORDER BY ad_type) AS ad_types,
            count(*) AS source_count, sum(impressions) AS impressions, sum(clicks) AS clicks,
            sum(spend) AS spend, sum(attributed_sales) AS sales, sum(attributed_orders) AS orders,
            round(sum(spend)*100/NULLIF(sum(attributed_sales),0),4) AS acos,
            round(sum(spend)/NULLIF(sum(clicks),0),4) AS cpc,
            round(sum(clicks)*100.0/NULLIF(sum(impressions),0),4) AS ctr
            FROM erp_search_term_rows r WHERE {SCOPE} GROUP BY {GROUPS}
            ORDER BY sum(spend) DESC,{GROUPS} LIMIT %(limit)s OFFSET %(offset)s""", params).fetchall()
    return dict(rows=rows[:limit], next_offset=offset+limit if len(rows)>limit else None,
                start_date=params["start"], end_date=params["end"], currency=params["currency"])


@router.get("/source")
def source_rows(product_id: UUID, search_term: str = Query(min_length=1, max_length=512),
                profile_id: str = Query(min_length=1, max_length=120), attribution_days: int = Query(ge=1, le=90),
                start_date: date | None = None, end_date: date | None = None,
                offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), actor=Depends(require_user)):
    with connection() as conn:
        params = dict(read_scope(conn, product_id, start_date, end_date), term=search_term, profile=profile_id,
                      window=attribution_days, offset=offset, limit=limit+1)
        rows = conn.execute(f"""SELECT r.report_date,r.ad_type,r.campaign_name,r.campaign_id,r.ad_group_id,r.keyword_id,
            r.match_type,r.impressions,r.clicks,r.spend,r.attributed_sales AS sales,r.attributed_orders AS orders,
            r.import_id,r.source_line,i.filename,i.completed_at FROM erp_search_term_rows r
            JOIN data_imports i ON i.id=r.import_id WHERE {SCOPE} AND r.profile_id=%(profile)s
            AND r.attribution_days=%(window)s AND r.search_term=%(term)s
            ORDER BY r.report_date,r.business_key LIMIT %(limit)s OFFSET %(offset)s""", params).fetchall()
    return dict(rows=rows[:limit], next_offset=offset+limit if len(rows)>limit else None)
