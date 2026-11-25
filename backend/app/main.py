import json
import hashlib
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, field_validator
from psycopg.types.json import Jsonb

from .erp_shop_sales import router as erp_shop_sales_router
from .erp_queue import router as erp_queue_router
from .erp_search_terms import router as erp_search_terms_router
from .erp_refunds import router as erp_refunds_router
from .erp_returns import router as erp_returns_router
from .erp_settlements import router as erp_settlements_router
from .erp_fee_allocations import router as erp_fee_allocation_router
from .agent import analyze_product
from .context import PageId, build_context, resolve_scope
from .db import connection
from .llm import LLMNotConfigured, ask_external_llm
from .importer import SCHEMAS, parse_csv
from .workspace import router as workspace_router, initialize_sop
from .updater import read_state, start_update, version_info
from .workflow_auth import router as auth_router, require_admin, require_user
from .process import router as process_router, reject_legacy_process_write
from .erp_fulfillment import router as erp_fulfillment_router
from .erp_stock import router as erp_stock_router
from .erp_comparison import router as erp_comparison_router
from .erp_payables import router as payables_router
from .erp_procurement import router as procurement_router
from .erp_receiving import router as receiving_router
from .ads_reviews import router as ads_reviews_router
from .ad_review_periods import router as ad_review_periods_router
from .ad_review_feedback import router as ad_review_feedback_router
from .feishu_sync import router as feishu_sync_router
from .triage_drafts import router as triage_drafts_router
from .workbench import router as workbench_router
from .workbench_insights import router as workbench_insights_router
from .approvals import router as approvals_router
from .data_automation import router as data_router, load_product_analytics, _audit, _json


@asynccontextmanager
async def lifespan(app):
    from .sync_worker import SyncWorker
    worker=SyncWorker()
    worker.start()
    try:yield
    finally:worker.stop()


app = FastAPI(title="Amazon Operations Agent API", version="0.5.0",lifespan=lifespan)
app.include_router(erp_shop_sales_router)
app.include_router(erp_queue_router)
app.include_router(erp_search_terms_router)
app.include_router(erp_refunds_router)
app.include_router(erp_returns_router)
app.include_router(erp_settlements_router)
app.include_router(erp_fee_allocation_router)
app.include_router(workspace_router)
app.include_router(auth_router)
app.include_router(process_router)
app.include_router(approvals_router)
app.include_router(erp_fulfillment_router)
app.include_router(erp_stock_router)
app.include_router(erp_comparison_router)
app.include_router(receiving_router)
app.include_router(procurement_router)
app.include_router(payables_router)
app.include_router(ads_reviews_router)
app.include_router(ad_review_periods_router)
app.include_router(ad_review_feedback_router)
app.include_router(feishu_sync_router)
app.include_router(triage_drafts_router)
app.include_router(workbench_router)
app.include_router(workbench_insights_router)
app.include_router(data_router)


@app.middleware('http')
async def private_process_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(('/api/erp-', '/api/workspace', '/api/workbench', '/api/process', '/api/workflow-auth', '/api/approvals','/api/integrations')) or any(part in request.url.path for part in ('/ads-review','/analytics','/erp/','/imports','/operating-summary','/records')):
        response.headers['Cache-Control'] = 'no-store'
    return response
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskStatusUpdate(BaseModel):
    status: Literal["not_started", "in_progress", "pending_approval", "completed", "blocked", "not_applicable"]


class ProductCreate(BaseModel):
    model: str
    name: str
    marketplace: str = "US"
    asin: str | None = None
    sku: str | None = None
    brand: str | None = None
    product_type: str | None = None
    lifecycle_stage: str = "pre_launch"
    target_launch_date: date | None = None
    currency: str = "USD"
    business_timezone: str = 'America/Los_Angeles'

    @field_validator('business_timezone')
    @classmethod
    def valid_zone(cls,value):
        try:ZoneInfo(value)
        except (ZoneInfoNotFoundError,ValueError):raise ValueError('需要有效的 IANA 业务时区') from None
        return value


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str | None = None
    approver_id: UUID | None = None


class TrackingQueryCreate(BaseModel):
    keyword: str
    marketplace: str = "US"
    postal_code: str = "10001"
    locale: str = "en-US"
    device: str = "desktop"
    max_pages: int = 3
    runs_per_day: int = 4


class EvidenceCreate(BaseModel):
    evidence_type: Literal["file", "url", "note"]
    uri: str
    note: str | None = None


class AgentChatRequest(BaseModel):
    product_id: UUID
    page_id: PageId
    message: str
    conversation_id: UUID | None = None
    filters: dict = {}


@app.get("/api/workspace-app/manifest.webmanifest", include_in_schema=False)
def workspace_app_manifest():
    # Static previews have no API route advertising an installable operational workspace.
    return RedirectResponse("/manifest.webmanifest", headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    with connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/system/version")
def system_version(response: Response):
    response.headers['Cache-Control']='no-store'
    return version_info()


@app.get("/api/system/update-status")
def system_update_status(response: Response):
    response.headers['Cache-Control']='no-store'
    return read_state()


@app.post("/api/system/update", status_code=202)
def system_update(actor=Depends(require_admin)):
    return start_update(actor)


@app.get("/api/products")
def list_products():
    sql = """
    SELECT p.*, pp.completion_percent, pp.overdue_count, pp.blocked_count
    FROM products p JOIN product_progress pp ON pp.product_id = p.id
    ORDER BY p.created_at DESC
    """
    with connection() as conn:
        return conn.execute(sql).fetchall()


@app.post("/api/products", status_code=201)
def create_product(body: ProductCreate, actor=Depends(require_admin)):
    if not body.model.strip() or not body.name.strip():
        raise HTTPException(422, "Model and name are required")
    with connection() as conn:
        try:
            product = conn.execute(
                """INSERT INTO products(model,name,marketplace,asin,sku,brand,product_type,lifecycle_stage,target_launch_date,current_stage,currency,business_timezone)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'档案建立',%s,%s) RETURNING *""",
                (body.model.strip(), body.name.strip(), body.marketplace, body.asin, body.sku, body.brand,
                 body.product_type, body.lifecycle_stage, body.target_launch_date,body.currency,body.business_timezone),
            ).fetchone()
            initialize_sop(conn, product["id"])
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise HTTPException(409, "Product model already exists or data is invalid") from exc
    return product


@app.get("/api/products/{product_id}/dashboard")
def product_dashboard(product_id: UUID):
    with connection() as conn:
        product = conn.execute(
            "SELECT p.*, pp.completion_percent, pp.overdue_count, pp.blocked_count FROM products p JOIN product_progress pp ON pp.product_id=p.id WHERE p.id=%s",
            (product_id,),
        ).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        tasks = conn.execute(
            """SELECT t.*,u.display_name AS owner_name,
                      (SELECT COUNT(*) FROM task_evidence e WHERE e.task_id=t.id) AS evidence_count,
                      (SELECT a.decision FROM approvals a WHERE a.task_id=t.id ORDER BY a.decided_at DESC NULLS LAST LIMIT 1) AS approval_status,
                      (SELECT COUNT(*) FROM task_dependencies d JOIN tasks prerequisite ON prerequisite.id=d.depends_on_task_id
                       WHERE d.task_id=t.id AND prerequisite.status NOT IN ('completed','not_applicable')) AS open_dependency_count
               FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id
               LEFT JOIN app_users u ON u.id=t.owner_id WHERE pw.product_id=%s ORDER BY t.sequence""",
            (product_id,),
        ).fetchall()
        rankings = conn.execute(
            """SELECT tq.keyword, ro.observation_type, ro.observed_at, ro.rank_value, ro.page_number, ro.visible_position
               FROM rank_observations ro LEFT JOIN tracking_queries tq ON tq.id=ro.tracking_query_id
               WHERE ro.product_id=%s ORDER BY ro.observed_at""",
            (product_id,),
        ).fetchall()
        findings = conn.execute(
            "SELECT * FROM agent_findings WHERE product_id=%s AND status='open' ORDER BY risk DESC, created_at DESC",
            (product_id,),
        ).fetchall()
        queries = conn.execute(
            "SELECT * FROM tracking_queries WHERE product_id=%s ORDER BY keyword",
            (product_id,),
        ).fetchall()
    return {"product": product, "tasks": tasks, "rankings": rankings, "findings": findings, "tracking_queries": queries}


@app.patch("/api/tasks/{task_id}/status")
def update_task_status(task_id: UUID, body: TaskStatusUpdate, actor=Depends(require_user)):
    with connection() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        reject_legacy_process_write(conn, task_id)
        if not actor['is_admin'] and task['owner_id'] != actor['id']:
            raise HTTPException(403, '只能处理分配给自己的任务')
        if body.status == "completed" and task["evidence_required"]:
            evidence_count = conn.execute("SELECT COUNT(*) AS n FROM task_evidence WHERE task_id=%s", (task_id,)).fetchone()["n"]
            if evidence_count == 0:
                raise HTTPException(409, "This task requires evidence before completion")
        if body.status == "completed":
            blockers = conn.execute(
                """SELECT COUNT(*) AS n FROM task_dependencies d JOIN tasks prerequisite ON prerequisite.id=d.depends_on_task_id
                   WHERE d.task_id=%s AND prerequisite.status NOT IN ('completed','not_applicable')""",
                (task_id,),
            ).fetchone()["n"]
            if blockers:
                raise HTTPException(409, "Complete prerequisite tasks first")
        if body.status == "completed" and task["approval_required"]:
            approved = conn.execute(
                "SELECT decision FROM approvals WHERE task_id=%s AND task_revision=%s ORDER BY decided_at DESC NULLS LAST LIMIT 1", (task_id, task['revision'])
            ).fetchone()
            if not approved or approved["decision"] != "approved":
                raise HTTPException(409, "This task requires approval before completion")
        completed_at = "now()" if body.status == "completed" else "NULL"
        updated = conn.execute(
            f"UPDATE tasks SET status=%s, completed_at={completed_at}, updated_at=now() WHERE id=%s RETURNING *",
            (body.status, task_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO audit_logs(entity_type,entity_id,action,before_data,after_data) VALUES('task',%s,'status_changed',%s,%s)",
            (str(task_id), f'{{"status":"{task["status"]}"}}', f'{{"status":"{body.status}"}}'),
        )
        conn.commit()
        return updated


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: UUID):
    with connection() as conn:
        task = conn.execute(
            """SELECT t.*,u.display_name AS owner_name FROM tasks t LEFT JOIN app_users u ON u.id=t.owner_id WHERE t.id=%s""",
            (task_id,),
        ).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        evidence = conn.execute("SELECT * FROM task_evidence WHERE task_id=%s ORDER BY created_at DESC", (task_id,)).fetchall()
        approvals = conn.execute("SELECT * FROM approvals WHERE task_id=%s ORDER BY decided_at DESC NULLS LAST", (task_id,)).fetchall()
        dependencies = conn.execute(
            """SELECT prerequisite.id,prerequisite.title,prerequisite.status FROM task_dependencies d
               JOIN tasks prerequisite ON prerequisite.id=d.depends_on_task_id WHERE d.task_id=%s""", (task_id,)
        ).fetchall()
    return {"task": task, "evidence": evidence, "approvals": approvals, "dependencies": dependencies}


@app.post("/api/tasks/{task_id}/evidence", status_code=201)
def add_task_evidence(task_id: UUID, body: EvidenceCreate, actor=Depends(require_user)):
    if not body.uri.strip():
        raise HTTPException(422, "Evidence URI or note is required")
    with connection() as conn:
        exists = conn.execute("SELECT owner_id FROM tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
        if not exists:
            raise HTTPException(404, "Task not found")
        reject_legacy_process_write(conn, task_id)
        if not actor['is_admin'] and exists['owner_id'] != actor['id']:
            raise HTTPException(403, '只能为分配给自己的任务登记证据')
        row = conn.execute(
            "INSERT INTO task_evidence(task_id,evidence_type,uri,note) VALUES(%s,%s,%s,%s) RETURNING *",
            (task_id, body.evidence_type, body.uri.strip(), body.note),
        ).fetchone()
        conn.commit()
        return row


@app.post("/api/tasks/{task_id}/approval", status_code=201)
def decide_task_approval(task_id: UUID, body: ApprovalDecision, actor=Depends(require_admin)):
    with connection() as conn:
        task = conn.execute("SELECT approval_required,revision FROM tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        reject_legacy_process_write(conn, task_id)
        if body.decision == 'approved':
            record=conn.execute('SELECT COUNT(*) n FROM task_evidence WHERE task_id=%s',(task_id,)).fetchone()
            if not record['n']:
                raise HTTPException(409, 'Approval requires delivery evidence')
            blocked=conn.execute("SELECT COUNT(*) n FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on_task_id WHERE d.task_id=%s AND p.status NOT IN ('completed','not_applicable')",(task_id,)).fetchone()
            if blocked['n']:
                raise HTTPException(409, 'Approval requires completed prerequisites')
        if not task["approval_required"]:
            raise HTTPException(409, "Task does not require approval")
        row = conn.execute(
            """INSERT INTO approvals(task_id,approver_id,decision,comment,decided_at,task_revision)
               VALUES(%s,%s,%s,%s,now(),%s) RETURNING *""",
            (task_id, actor['id'], body.decision, body.comment,task["revision"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO audit_logs(actor_id,entity_type,entity_id,action,after_data) VALUES(%s,'task',%s,'approval_decided',%s)",
            (actor['id'], str(task_id), Jsonb({"decision": body.decision, "comment": body.comment})),
        )
        conn.commit()
        return row


TEMPLATES = {
    "advertised_product": "date,campaign_name,entity_name,impressions,clicks,spend,sales,orders,currency,report_grain,profile_id,ad_type,campaign_id,ad_group_id,ad_id,sku,asin,attribution_days,account_key,marketplace\n2026-11-14,SP sample,group A,100,10,10.00,50.00,2,USD,advertised_product,123456,SP,201,301,401,EXAMPLE-SKU,,7,example-store,US\n",
    "sales": "date,sku,asin,units_ordered,ordered_revenue,currency\n2026-11-14,G11-US,B0GXZ848Q1,3,299.97,USD\n",
    "advertising": "date,campaign_name,entity_name,impressions,clicks,spend,sales,orders,currency\n2026-11-14,G11-SP-AUTO,close-match,1200,18,22.50,99.99,1,USD\n",
    "inventory": "date,sku,asin,fulfillable,reserved,inbound,unsellable\n2026-11-14,G11-US,B0GXZ848Q1,240,12,480,2\n",
    "costs": "date,sku,product_cost,first_mile_cost,fba_fee,referral_fee,other_cost,currency\n2026-11-14,G11-US,28.50,3.20,8.40,15.00,1.20,USD\n",
}


@app.get("/api/imports/template/{import_type}", response_class=PlainTextResponse)
def import_template(import_type: str):
    if import_type not in TEMPLATES:
        raise HTTPException(404, "Unknown import type")
    return PlainTextResponse(TEMPLATES[import_type], headers={"Content-Disposition": f'attachment; filename="{import_type}-template.csv"'})


@app.post("/api/products/{product_id}/imports", status_code=201)
async def import_operating_data(product_id: UUID, response: Response, import_type: str = Form(...), file: UploadFile = File(...),
                                actor=Depends(require_admin), required_grain: Literal["search_term", "advertised_product"] | None = Form(None),
                                validate_only: bool = Form(False)):
    if validate_only and required_grain != "advertised_product":
        raise HTTPException(422, "商品广告预检须指定 advertised_product")
    if import_type not in SCHEMAS:
        raise HTTPException(422, "Unsupported import type")
    raw = await file.read(10 * 1024 * 1024+1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "CSV file is limited to 10 MB")
    content_hash = hashlib.sha256(raw).hexdigest()
    with connection() as conn:
        product=conn.execute("SELECT sku,asin,currency,marketplace,data_account_key FROM products WHERE id=%s FOR SHARE", (product_id,)).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        try:
            rows, errors = parse_csv(import_type, raw,default_currency=product['currency'])
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if not rows:
            raise HTTPException(422, {"message": "No valid rows", "errors": errors[:20]})
        if required_grain and (import_type != "advertising" or any(row.get("report_grain") != required_grain for row in rows)):
            raise HTTPException(422, f"此入口仅接收明确标注 report_grain={required_grain} 的完整日报")
        mappings=conn.execute('SELECT sku,asin FROM operating_product_mappings WHERE product_id=%s',(product_id,)).fetchall()
        allowed_skus={p['sku'] for p in mappings if p['sku']}|({product['sku']} if product['sku'] else set())
        allowed_asins={p['asin'] for p in mappings if p['asin']}|({product['asin']} if product['asin'] else set())
        for row in rows:
            if row.get('sku') and allowed_skus and row['sku'] not in allowed_skus:
                raise HTTPException(422, 'SKU belongs to a different product')
            if row.get('asin') and allowed_asins and row['asin'] not in allowed_asins:
                raise HTTPException(422, 'ASIN belongs to a different product')
            if import_type!='inventory' and row.get('currency')!=product['currency']:
                raise HTTPException(422, 'Currency must match product currency')
        if import_type == "advertising" and any(row.get("report_grain") == "search_term" for row in rows):
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("search_terms:" + str(product_id),))
        if import_type == "advertising" and rows[0].get("report_grain") == "advertised_product":
            from .advertised_product_csv import validate_scope
            if not validate_only:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("advertised_product:" + str(product_id),))
            validate_scope(conn,product_id,product,rows)
            if validate_only:
                response.status_code = 200
                columns = TEMPLATES["advertised_product"].splitlines()[0].split(",")
                sample = [{key: str(row[key]) if row.get(key) is not None else "" for key in columns} for row in rows[:5]]
                return {"row_count": len(rows), "headers": columns, "rows": sample, "errors": []}
            # Replaying an older file must not undo a later correction.
            receipt=conn.execute("""SELECT i.* FROM operating_csv_artifacts a JOIN data_imports i ON i.id=a.import_id
                WHERE a.product_id=%s AND a.content_sha256=%s AND i.import_type='advertising'
                AND a.normalized_rows->0->>'report_grain'='advertised_product'
                AND a.normalized_rows->0->>'account_key'=%s AND a.normalized_rows->0->>'marketplace'=%s
                ORDER BY i.created_at LIMIT 1""",(product_id,content_hash,product['data_account_key'],product['marketplace'])).fetchone()
            if receipt:
                return {**receipt, "replayed": True}
        import_row = conn.execute(
            """INSERT INTO data_imports(product_id,import_type,filename,status,row_count,error_count,errors,imported_by)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (product_id, import_type, file.filename or "upload.csv", "completed_with_errors" if errors else "completed",
             len(rows), len(errors), Jsonb(errors[:100]),actor['id']),
        ).fetchone()
        previous_rows=[]
        table,day_field={'sales':('sales_daily','sale_date'),'advertising':('ad_performance_daily','report_date'),
            'inventory':('inventory_snapshots','snapshot_date'),'costs':('cost_records','effective_date')}[import_type]
        for row in rows:
            if import_type == "advertising" and row.get("report_grain") in ("search_term", "advertised_product"):
                if row["report_grain"] == "search_term":
                    from .erp_search_terms import store_source
                else:
                    from .advertised_product_csv import store_source
                previous = store_source(conn, product_id, row, import_row['id'])
                if previous:
                    previous_rows.append(dict(previous))
                continue
            key_fields=('campaign_name','entity_name') if import_type=='advertising' else ('sku',)
            previous=conn.execute('SELECT * FROM '+table+' WHERE product_id=%s AND '+day_field+'=%s AND '+
                ' AND '.join(k+'=%s' for k in key_fields),(product_id,row['date'],*(row.get(k,'') for k in key_fields))).fetchone()
            if previous:previous_rows.append(dict(previous))
            if import_type == "sales":
                conn.execute("""INSERT INTO sales_daily VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(product_id,sale_date,sku) DO UPDATE SET asin=EXCLUDED.asin,units_ordered=EXCLUDED.units_ordered,
                    ordered_revenue=EXCLUDED.ordered_revenue,currency=EXCLUDED.currency,import_id=EXCLUDED.import_id""",
                    (product_id,row["date"],row["sku"],row["asin"],row["units_ordered"],row["ordered_revenue"],row["currency"],import_row["id"]))
            elif import_type == "advertising":
                conn.execute("""INSERT INTO ad_performance_daily VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(product_id,report_date,campaign_name,entity_name) DO UPDATE SET impressions=EXCLUDED.impressions,
                    clicks=EXCLUDED.clicks,spend=EXCLUDED.spend,attributed_sales=EXCLUDED.attributed_sales,
                    attributed_orders=EXCLUDED.attributed_orders,currency=EXCLUDED.currency,import_id=EXCLUDED.import_id""",
                    (product_id,row["date"],row["campaign_name"],row["entity_name"],row["impressions"],row["clicks"],row["spend"],row["sales"],row["orders"],row["currency"],import_row["id"]))
            elif import_type == "inventory":
                conn.execute("""INSERT INTO inventory_snapshots VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(product_id,snapshot_date,sku) DO UPDATE SET asin=EXCLUDED.asin,fulfillable=EXCLUDED.fulfillable,
                    reserved=EXCLUDED.reserved,inbound=EXCLUDED.inbound,unsellable=EXCLUDED.unsellable,import_id=EXCLUDED.import_id""",
                    (product_id,row["date"],row["sku"],row["asin"],row["fulfillable"],row["reserved"],row["inbound"],row["unsellable"],import_row["id"]))
            else:
                conn.execute("""INSERT INTO cost_records VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(product_id,effective_date,sku) DO UPDATE SET product_cost=EXCLUDED.product_cost,
                    first_mile_cost=EXCLUDED.first_mile_cost,fba_fee=EXCLUDED.fba_fee,referral_fee=EXCLUDED.referral_fee,
                    other_cost=EXCLUDED.other_cost,currency=EXCLUDED.currency,import_id=EXCLUDED.import_id""",
                    (product_id,row["date"],row["sku"],row["product_cost"],row["first_mile_cost"],row["fba_fee"],row["referral_fee"],row["other_cost"],row["currency"],import_row["id"]))
        conn.execute('INSERT INTO operating_csv_artifacts(import_id,product_id,content_sha256,normalized_rows,previous_rows,actor_id) VALUES(%s,%s,%s,%s,%s,%s)',
            (import_row['id'],product_id,content_hash,Jsonb(_json(rows)),Jsonb(_json(previous_rows)),actor['id']))
        _audit(conn,actor,'data_import',import_row['id'],'imported',after={'product_id':str(product_id),
            'dataset':import_type,'row_count':len(rows),'error_count':len(errors),
            'content_sha256':content_hash})
        import_row=conn.execute("UPDATE data_imports SET completed_at=now() WHERE id=%s RETURNING *", (import_row["id"],)).fetchone()
        conn.commit()
    return {**import_row, "errors": errors[:20]}


@app.get("/api/products/{product_id}/imports")
def list_imports(product_id: UUID,actor=Depends(require_user)):
    with connection() as conn:
        return conn.execute("SELECT * FROM data_imports WHERE product_id=%s ORDER BY created_at DESC LIMIT 50", (product_id,)).fetchall()


@app.get("/api/products/{product_id}/imports/{import_id}/rows")
def imported_product_ad_rows(product_id: UUID, import_id: UUID, offset: int = Query(0, ge=0, le=2147483000),
                             limit: int = Query(50, ge=1, le=100), actor=Depends(require_user)):
    with connection() as conn:
        batch = conn.execute("""SELECT i.filename, a.normalized_rows->0->>'report_grain' AS grain,
            jsonb_array_length(a.normalized_rows) AS total
            FROM data_imports i LEFT JOIN operating_csv_artifacts a ON a.import_id=i.id AND a.product_id=i.product_id
            WHERE i.id=%s AND i.product_id=%s""", (import_id,product_id)).fetchone()
        if not batch:
            raise HTTPException(404, "所选产品没有这笔导入记录")
        if batch['grain'] != 'advertised_product':
            return {"grain": batch['grain'], "rows": [], "total": 0, "next_offset": None}
        total = batch['total']
        rows = conn.execute("""SELECT a.normalized_rows->n AS payload FROM operating_csv_artifacts a
            CROSS JOIN generate_series(%s::integer,%s::integer) AS idx(n)
            WHERE a.import_id=%s AND a.product_id=%s ORDER BY n""",
            (offset, min(offset+limit,total)-1, import_id, product_id)).fetchall()
        return {"grain":batch['grain'], "total":total, "rows":[r['payload'] for r in rows],
                "next_offset":offset+limit if offset+limit < total else None}


@app.get("/api/products/{product_id}/operating-summary")
def operating_summary(product_id: UUID,actor=Depends(require_user)):
    # Backward-compatible fields, now derived from the same engine as the UI.
    from datetime import datetime,timedelta,timezone
    with connection() as conn:
        product=conn.execute('SELECT business_timezone FROM products WHERE id=%s',(product_id,)).fetchone()
        if not product:raise HTTPException(404,'Product not found')
        end=datetime.now(timezone.utc).astimezone(ZoneInfo(product['business_timezone'])).date()
        data=load_product_analytics(conn,product_id,end-timedelta(days=29),end)
        imports=conn.execute('SELECT import_type,MAX(created_at) latest FROM data_imports WHERE product_id=%s GROUP BY import_type',(product_id,)).fetchall()
    metrics={module:{m['key']:m for m in body['metrics']} for module,body in data['modules'].items()}
    def value(module,key):return metrics[module].get(key,{}).get('value')
    counts=data['record_counts']
    inventory=None
    if value('inventory','fulfillable') is not None:
        inventory={key:value('inventory',key) for key in ('fulfillable','reserved','inbound','unsellable')}
        dates=metrics['inventory']['fulfillable'].get('inputs',{}).get('snapshot_dates',{})
        inventory.update(snapshot_dates=dates,snapshot_date=max(dates.values()) if dates else None,
                         oldest_snapshot_date=min(dates.values()) if dates else None)
    return {'period_days':30,'period':data['period'],'engine_version':data['engine_version'],
        'data_quality':data['data_quality'],'calculation_source':'deterministic_engine',
        'sales':{'rows':counts['sales'],'units':value('shop','units_ordered'),'revenue':value('shop','ordered_revenue')},
        'advertising':{'rows':counts['advertising'],'spend':value('ads','spend'),'sales':value('ads','attributed_sales'),
            'clicks':value('ads','clicks'),'impressions':value('ads','impressions'),'scope':data['modules']['ads'].get('scope')},
        'inventory':inventory,'latest_imports':imports,'cost_count':counts['costs']}


@app.post("/api/products/{product_id}/tracking-queries", status_code=201)
def create_tracking_query(product_id: UUID, body: TrackingQueryCreate):
    if not body.keyword.strip():
        raise HTTPException(422, "Keyword is required")
    with connection() as conn:
        row = conn.execute(
            """INSERT INTO tracking_queries(product_id,keyword,marketplace,postal_code,locale,device,max_pages,runs_per_day)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (product_id, body.keyword.strip(), body.marketplace, body.postal_code, body.locale, body.device, body.max_pages, body.runs_per_day),
        ).fetchone()
        conn.commit()
        return row


@app.get("/api/products/{product_id}/rankings")
def ranking_history(product_id: UUID, start: date | None = None, keyword: str | None = None):
    clauses = ["ro.product_id=%s"]
    params: list[object] = [product_id]
    if start:
        clauses.append("ro.observed_at::date >= %s")
        params.append(start)
    if keyword:
        clauses.append("tq.keyword=%s")
        params.append(keyword)
    sql = f"""SELECT tq.keyword, ro.* FROM rank_observations ro
              LEFT JOIN tracking_queries tq ON tq.id=ro.tracking_query_id
              WHERE {' AND '.join(clauses)} ORDER BY ro.observed_at"""
    with connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.post("/api/products/{product_id}/agent/analyze")
def run_agent_analysis(product_id: UUID):
    with connection() as conn:
        tasks = conn.execute(
            "SELECT t.* FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE pw.product_id=%s",
            (product_id,),
        ).fetchall()
        rankings = conn.execute(
            """SELECT tq.keyword, ro.* FROM rank_observations ro
               LEFT JOIN tracking_queries tq ON tq.id=ro.tracking_query_id
               WHERE ro.product_id=%s ORDER BY ro.observed_at""",
            (product_id,),
        ).fetchall()
    return {"findings": analyze_product(tasks, rankings)}


@app.post("/api/agent/chat")
async def agent_chat(body: AgentChatRequest,actor=Depends(require_user)):
    scope, question = resolve_scope(body.message, body.page_id)
    if not question:
        raise HTTPException(422, "Message is required")
    with connection() as conn:
        product = conn.execute("SELECT id FROM products WHERE id=%s", (body.product_id,)).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        context = build_context(conn, body.product_id, scope, body.filters)
        if scope in {'dashboard','global','shop','listing','ads','inventory','calendar','finance','service','compliance','agent'}:
            try:
                start=date.fromisoformat(body.filters['start_date']) if body.filters.get('start_date') else None
                end=date.fromisoformat(body.filters['end_date']) if body.filters.get('end_date') else None
            except (ValueError,TypeError):raise HTTPException(422,'经营日期需要有效的 YYYY-MM-DD') from None
            analytics=load_product_analytics(conn,body.product_id,start,end)
            selected=analytics['modules'] if scope in {'dashboard','global','agent'} else {scope:analytics['modules'][scope]}
            context['calculated_metrics']={'period':analytics['period'],'generated_at':analytics['generated_at'],
                'currency':analytics['product']['currency'],'modules':selected,
                'data_quality':analytics['data_quality']}
            context['as_of']=analytics['period']['as_of']
            context['record_sample_warning']='原始明细有条数限制，仅作抽样；期间结果以 calculated_metrics 中相同范围的后端指标为准。'
        if scope=='settings':
            from .data_automation import _public_connection
            rows=conn.execute('''SELECT DISTINCT c.* FROM operating_connections c
                JOIN operating_product_mappings m ON m.connection_id=c.id WHERE m.product_id=%s''',(body.product_id,)).fetchall()
            states=[_public_connection(conn,row) for row in rows]
            context['system']={'version':'0.5.0','amazon_sources':[
                {k:state[k] for k in ('name','provider','status','last_success_at','last_error')} for state in states],
                'permissions':'管理员配置连接与算法参数，成员按现有登录体系查看结果；平台业务修改不自动执行'}
        context = json.loads(json.dumps(context, default=str))
        conversation_id = body.conversation_id
        if conversation_id is not None:
            same_product = conn.execute("SELECT 1 FROM agent_conversations WHERE id=%s AND product_id=%s", (conversation_id,body.product_id)).fetchone()
            if not same_product:
                raise HTTPException(409, "Conversation belongs to a different product")
        if conversation_id is None:
            conversation_id = conn.execute(
                "INSERT INTO agent_conversations(product_id,current_page,title) VALUES(%s,%s,%s) RETURNING id",
                (body.product_id, body.page_id, question[:80]),
            ).fetchone()["id"]
        conn.execute(
            """INSERT INTO agent_messages(conversation_id,role,content,page_context,requested_scope,context_snapshot)
               VALUES(%s,'user',%s,%s,%s,%s)""",
            (conversation_id, question, body.page_id, scope, Jsonb(context)),
        )
        conn.commit()
    try:
        answer, model = await ask_external_llm(question, context, scope)
    except LLMNotConfigured:
        answer = f"外部AI尚未配置。当前请求已正确限定为“{scope}”数据范围。配置 LLM_API_KEY 和 LLM_MODEL 后即可获得真实回答。"
        model = "not_configured"
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"External AI request failed: {exc.__class__.__name__}") from exc
    with connection() as conn:
        conn.execute(
            """INSERT INTO agent_messages(conversation_id,role,content,page_context,requested_scope,context_snapshot,model_name)
               VALUES(%s,'assistant',%s,%s,%s,%s,%s)""",
            (conversation_id, answer, body.page_id, scope, Jsonb(context), model),
        )
        conn.execute("UPDATE agent_conversations SET current_page=%s,updated_at=now() WHERE id=%s", (body.page_id, conversation_id))
        conn.commit()
    return {"conversation_id": conversation_id, "answer": answer, "scope": scope, "page_id": body.page_id, "model": model}

