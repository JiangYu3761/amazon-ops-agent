import json
from datetime import date
from typing import Literal
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from psycopg.types.json import Jsonb

from .agent import analyze_product
from .context import PageId, build_context, resolve_scope
from .db import connection
from .llm import LLMNotConfigured, ask_external_llm
from .importer import SCHEMAS, parse_csv
from .workspace import router as workspace_router, initialize_sop
from .updater import read_state, start_update, version_info
from .workflow_auth import router as auth_router, require_admin, require_user
from .process import router as process_router, reject_legacy_process_write
from .approvals import router as approvals_router


app = FastAPI(title="Amazon Operations Agent API", version="0.4.0")
app.include_router(workspace_router)
app.include_router(auth_router)
app.include_router(process_router)
app.include_router(approvals_router)


@app.middleware('http')
async def private_process_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(('/api/process', '/api/workflow-auth', '/api/approvals')):
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
                """INSERT INTO products(model,name,marketplace,asin,sku,brand,product_type,lifecycle_stage,target_launch_date,current_stage,currency)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'档案建立',%s) RETURNING *""",
                (body.model.strip(), body.name.strip(), body.marketplace, body.asin, body.sku, body.brand,
                 body.product_type, body.lifecycle_stage, body.target_launch_date,body.currency),
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
async def import_operating_data(product_id: UUID, import_type: str = Form(...), file: UploadFile = File(...)):
    if import_type not in SCHEMAS:
        raise HTTPException(422, "Unsupported import type")
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "CSV file is limited to 10 MB")
    try:
        rows, errors = parse_csv(import_type, raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if not rows:
        raise HTTPException(422, {"message": "No valid rows", "errors": errors[:20]})
    with connection() as conn:
        product=conn.execute("SELECT sku,asin,currency FROM products WHERE id=%s", (product_id,)).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        for row in rows:
            if row.get('sku') and product.get('sku') and row['sku']!=product['sku']:
                raise HTTPException(422, 'SKU belongs to a different product')
            if row.get('asin') and product.get('asin') and row['asin']!=product['asin']:
                raise HTTPException(422, 'ASIN belongs to a different product')
            if import_type!='inventory' and row.get('currency')!=product['currency']:
                raise HTTPException(422, 'Currency must match product currency')
        import_row = conn.execute(
            """INSERT INTO data_imports(product_id,import_type,filename,status,row_count,error_count,errors)
               VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (product_id, import_type, file.filename or "upload.csv", "completed_with_errors" if errors else "completed",
             len(rows), len(errors), Jsonb(errors[:100])),
        ).fetchone()
        for row in rows:
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
        conn.execute("UPDATE data_imports SET completed_at=now() WHERE id=%s", (import_row["id"],))
        conn.commit()
    return {**import_row, "errors": errors[:20]}


@app.get("/api/products/{product_id}/imports")
def list_imports(product_id: UUID):
    with connection() as conn:
        return conn.execute("SELECT * FROM data_imports WHERE product_id=%s ORDER BY created_at DESC LIMIT 50", (product_id,)).fetchall()


@app.get("/api/products/{product_id}/operating-summary")
def operating_summary(product_id: UUID):
    with connection() as conn:
        sales = conn.execute("""SELECT COUNT(*) rows,SUM(units_ordered) units,SUM(ordered_revenue) revenue
            FROM sales_daily WHERE product_id=%s AND sale_date BETWEEN CURRENT_DATE-29 AND CURRENT_DATE""", (product_id,)).fetchone()
        ads = conn.execute("""SELECT COUNT(*) rows,SUM(spend) spend,SUM(attributed_sales) sales,
            SUM(clicks) clicks,SUM(impressions) impressions
            FROM ad_performance_daily WHERE product_id=%s AND report_date BETWEEN CURRENT_DATE-29 AND CURRENT_DATE""", (product_id,)).fetchone()
        inventory = conn.execute("""SELECT SUM(fulfillable) fulfillable,SUM(reserved) reserved,SUM(inbound) inbound,SUM(unsellable) unsellable,MAX(snapshot_date) snapshot_date FROM inventory_snapshots
            WHERE product_id=%s AND snapshot_date=(SELECT MAX(snapshot_date) FROM inventory_snapshots WHERE product_id=%s)""", (product_id,product_id)).fetchone()
        if inventory and inventory["snapshot_date"] is None:
            inventory=None
        costs=conn.execute("SELECT COUNT(*) n FROM cost_records WHERE product_id=%s",(product_id,)).fetchone()["n"]
        imports = conn.execute("SELECT import_type,MAX(created_at) latest FROM data_imports WHERE product_id=%s GROUP BY import_type", (product_id,)).fetchall()
    return {"period_days": 30, "sales": sales, "advertising": ads, "inventory": inventory, "latest_imports": imports, "cost_count": costs}


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
async def agent_chat(body: AgentChatRequest):
    scope, question = resolve_scope(body.message, body.page_id)
    if not question:
        raise HTTPException(422, "Message is required")
    with connection() as conn:
        product = conn.execute("SELECT id FROM products WHERE id=%s", (body.product_id,)).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        context = build_context(conn, body.product_id, scope, body.filters)
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

