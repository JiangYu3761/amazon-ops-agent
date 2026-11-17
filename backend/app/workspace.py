"""Product-scoped workspace and the shared operating SOP catalog."""
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from .db import connection
from .workflow_auth import require_admin, require_user
from .process import reject_legacy_process_write

router = APIRouter(prefix='/api')


def catalog():
    path = Path(os.getenv('CATALOG_FILE', str(Path(__file__).resolve().parents[2] / 'frontend' / 'catalog.json')))
    return json.loads(path.read_text())


def initialize_sop(conn, product_id):
    product = conn.execute('SELECT * FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone()
    if not product:
        raise HTTPException(404, 'Product not found')
    template = conn.execute("""INSERT INTO workflow_templates(code,name,product_type,version)
        VALUES('operations-v2','全周期运营SOP','general',2)
        ON CONFLICT(code) DO UPDATE SET is_active=true RETURNING *""").fetchone()
    workflow = conn.execute("""INSERT INTO product_workflows(product_id,workflow_template_id,template_version)
        VALUES(%s,%s,2) ON CONFLICT(product_id,workflow_template_id) DO UPDATE SET template_version=2 RETURNING id""",
        (product_id, template['id'])).fetchone()
    data = catalog()
    modules = {m['id']:m for m in data['modules']}
    ids = {}
    legacy=conn.execute("SELECT t.* FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE pw.product_id=%s AND t.sop_key IS NULL",(product_id,)).fetchall()
    aliases={
        '确认正式产品名称、型号和能力边界':'capability',
        '确认FCC、危险品及标签状态':'fcc-certification',
        '确认生产、头程及入仓周期':'supply-lead',
        '完成SKU成本卡和预算边界':'unit-cost',
        '完成标题、五点和关键词库':'copy',
        '完成主图、附图、A+和视频':'images',
        '首批货件入仓并达到可售状态':'receiving',
        '完成首发广告结构、预算与否词库':'ad-launch',
        '完成页面、库存、价格、促销和广告验收':'launch-gate',
        '完成首轮搜索词、广告位与竞品复盘':'weekly-review',
    }
    old_by_key={aliases.get(t['title'].replace(' ','')):t for t in legacy if t['title'].replace(' ','') in aliases}
    added = 0
    for item in data['tasks']:
        existing = conn.execute("""SELECT t.id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id
            WHERE pw.product_id=%s AND t.sop_key=%s ORDER BY t.sequence,t.id LIMIT 1""", (product_id,item['key'])).fetchone()
        if existing:
            ids[item['key']] = existing['id']
            continue
        old=old_by_key.get(item['key'])
        # A renamed matter keeps its original task ID, evidence and approvals.
        # The migration also handles this path for products that never run SOP refresh.
        previous_keys={'fcc-certification':'certification','dangerous-goods':'dangerous','label-confirmation':'label'}
        if not old and item['key'] in previous_keys:
            old=conn.execute('''SELECT t.* FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id
                WHERE pw.product_id=%s AND t.sop_key=%s ORDER BY t.sequence LIMIT 1''',
                (product_id,previous_keys[item['key']])).fetchone()
        if old:
            conn.execute("""UPDATE tasks SET product_workflow_id=%s,sop_key=%s,module_code=%s,phase=%s,
                department=COALESCE(department,%s),acceptance_criteria=COALESCE(acceptance_criteria,%s)
                WHERE id=%s""",(workflow['id'],item['key'],item['module'],item['phase'],item['department'],item['acceptance_criteria'],old['id']))
            ids[item['key']]=old['id']
            continue
        task = conn.execute("""INSERT INTO tasks(product_workflow_id,stage_name,title,sequence,weight,due_date,
              evidence_required,approval_required,acceptance_criteria,observation_days,module_code,phase,department,sop_key)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
              (workflow['id'], modules[item['module']]['name'], item['title'], item['sequence'], item['weight'],
               product['target_launch_date']+timedelta(days=item['offset']) if product['target_launch_date'] else None,
               item['evidence_required'], item['approval_required'], item['acceptance_criteria'], item['observation_days'],
               item['module'],item['phase'],item['department'],item['key'])).fetchone()
        ids[item['key']] = task['id']
        added += 1
    for item in data['tasks']:
        for prerequisite in item['depends_on']:
            conn.execute('INSERT INTO task_dependencies VALUES(%s,%s) ON CONFLICT DO NOTHING', (ids[item['key']],ids[prerequisite]))
    return added


@router.get('/workspace')
def read_workspace():
    with connection() as conn:
        products=conn.execute('SELECT p.*,pp.completion_percent FROM products p JOIN product_progress pp ON pp.product_id=p.id ORDER BY p.created_at').fetchall()
        tasks=conn.execute("""SELECT t.*,pw.product_id,u.display_name owner_name,
          EXISTS(SELECT 1 FROM process_configs pc WHERE pc.task_id=t.id) process_managed,
          (SELECT COUNT(*) FROM task_evidence e WHERE e.task_id=t.id) evidence_count,
          (SELECT a.decision FROM approvals a WHERE a.task_id=t.id AND a.task_revision=t.revision ORDER BY a.decided_at DESC NULLS LAST LIMIT 1) approval_status
          FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id LEFT JOIN app_users u ON u.id=t.owner_id ORDER BY t.sequence""").fetchall()
        from .approvals import summaries
        approval_summaries=summaries(conn,[t['id'] for t in tasks])
        for task in tasks:
            task['approval_summary']=approval_summaries.get(str(task['id']))
        dependencies=conn.execute('SELECT * FROM task_dependencies').fetchall()
        people=conn.execute('SELECT u.id,u.display_name,u.email,u.role_code,d.name department FROM app_users u LEFT JOIN departments d ON d.id=u.department_id WHERE u.is_active=true ORDER BY u.display_name').fetchall()
        events=conn.execute('SELECT * FROM operating_events ORDER BY start_date').fetchall()
        queries=conn.execute('SELECT * FROM tracking_queries ORDER BY keyword').fetchall()
        rankings=conn.execute('SELECT ro.*,tq.keyword FROM rank_observations ro LEFT JOIN tracking_queries tq ON tq.id=ro.tracking_query_id ORDER BY observed_at').fetchall()
        imports=conn.execute('SELECT * FROM data_imports ORDER BY created_at DESC LIMIT 100').fetchall()
        documents=conn.execute('SELECT * FROM product_documents ORDER BY created_at DESC').fetchall()
        evidence_links=conn.execute("SELECT e.*,pw.product_id FROM task_evidence e JOIN tasks t ON t.id=e.task_id JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE e.uri ~* '^https?://' ORDER BY e.created_at DESC").fetchall()
    return dict(products=products,tasks=tasks,dependencies=dependencies,people=people,events=events,queries=queries,rankings=rankings,imports=imports,documents=documents,evidence_links=evidence_links)


@router.post('/products/{product_id}/workflow')
def add_sop(product_id: UUID, actor=Depends(require_admin)):
    with connection() as conn:
        added=initialize_sop(conn,product_id)
        conn.commit()
    return {'added':added}


class TaskBody(BaseModel):
    product_id: UUID
    title: str=Field(min_length=1,max_length=300)
    module_code: str='review'
    phase: str='上线前'
    owner_id: UUID|None=None
    due_date: date|None=None
    description: str|None=None
    acceptance_criteria: str|None=None
    evidence_required: bool=True
    approval_required: bool=False
    department: str|None=None


@router.post('/tasks',status_code=201)
def create_task(body:TaskBody, actor=Depends(require_admin)):
    if body.module_code not in {m['id'] for m in catalog()['modules']}:
        raise HTTPException(422,'Unknown module')
    with connection() as conn:
        workflow=conn.execute('SELECT id FROM product_workflows WHERE product_id=%s ORDER BY started_at DESC LIMIT 1',(body.product_id,)).fetchone()
        if not workflow:
            initialize_sop(conn,body.product_id)
            workflow=conn.execute('SELECT id FROM product_workflows WHERE product_id=%s ORDER BY started_at DESC LIMIT 1',(body.product_id,)).fetchone()
        module=next(m for m in catalog()['modules'] if m['id']==body.module_code)
        row=conn.execute("""INSERT INTO tasks(product_workflow_id,stage_name,title,sequence,owner_id,due_date,description,
             acceptance_criteria,evidence_required,approval_required,module_code,phase,department)
             VALUES(%s,%s,%s,1000,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
             (workflow['id'],module['name'],body.title.strip(),body.owner_id,body.due_date,body.description,
              body.acceptance_criteria,body.evidence_required,body.approval_required,body.module_code,body.phase,body.department)).fetchone()
        conn.execute("INSERT INTO audit_logs(entity_type,entity_id,action,after_data) VALUES('task',%s,'created',%s)",(str(row['id']),Jsonb({'title':row['title']})))
        conn.commit()
    return row


class TaskEdit(BaseModel):
    title: str=Field(min_length=1,max_length=300)
    owner_id: UUID|None=None
    due_date: date|None=None
    acceptance_criteria: str|None=None
    description: str|None=None
    department: str|None=None
    dependencies: list[UUID]=Field(default_factory=list)


@router.patch('/tasks/{task_id}')
def edit_task(task_id:UUID,body:TaskEdit, actor=Depends(require_admin)):
    with connection() as conn:
        task=conn.execute('SELECT t.*,pw.product_id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE t.id=%s FOR UPDATE OF t',(task_id,)).fetchone()
        if not task:
            raise HTTPException(404,'Task not found')
        reject_legacy_process_write(conn, task_id)
        # Serialize dependency edits per product before evaluating cycle constraints.
        conn.execute('SELECT id FROM products WHERE id=%s FOR UPDATE',(task['product_id'],))
        for prerequisite in body.dependencies:
            other=conn.execute('SELECT pw.product_id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE t.id=%s',(prerequisite,)).fetchone()
            if not other or other['product_id']!=task['product_id'] or prerequisite==task_id:
                raise HTTPException(422,'Dependency must be another task of this product')
            cycle=conn.execute("""WITH RECURSIVE chain(id) AS (
              SELECT depends_on_task_id FROM task_dependencies WHERE task_id=%s
              UNION SELECT d.depends_on_task_id FROM task_dependencies d JOIN chain c ON d.task_id=c.id
              ) SELECT 1 FROM chain WHERE id=%s LIMIT 1""",(prerequisite,task_id)).fetchone()
            if cycle:
                raise HTTPException(409,'Dependency cycle rejected')
        conn.execute('DELETE FROM task_dependencies WHERE task_id=%s',(task_id,))
        for prerequisite in body.dependencies:
            conn.execute('INSERT INTO task_dependencies VALUES(%s,%s)',(task_id,prerequisite))
        row=conn.execute("""UPDATE tasks SET title=%s,owner_id=%s,due_date=%s,acceptance_criteria=%s,description=%s,department=%s,
              revision=revision+1,status=CASE WHEN status='completed' THEN 'in_progress'::task_status ELSE status END,
              completed_at=NULL,updated_at=now() WHERE id=%s RETURNING *""",
              (body.title.strip(),body.owner_id,body.due_date,body.acceptance_criteria,body.description,body.department,task_id)).fetchone()
        conn.execute("INSERT INTO audit_logs(entity_type,entity_id,action,before_data,after_data) VALUES('task',%s,'edited',%s,%s)",(str(task_id),Jsonb({'title':task['title'],'revision':task['revision']}),Jsonb({'title':row['title'],'revision':row['revision']})))
        conn.commit()
    return row


class PersonBody(BaseModel):
    display_name:str=Field(min_length=1,max_length=100)
    email:str=Field(min_length=3,max_length=200)
    department:str=Field(min_length=1,max_length=100)
    role_code:Literal['executor','project_owner','reviewer']='executor'


@router.post('/people',status_code=201)
def add_person(body:PersonBody, actor=Depends(require_admin)):
    with connection() as conn:
        dept=conn.execute('INSERT INTO departments(name) VALUES(%s) ON CONFLICT(name) DO UPDATE SET name=EXCLUDED.name RETURNING id',(body.department,)).fetchone()
        row=conn.execute('INSERT INTO app_users(display_name,email,department_id,role_code) VALUES(%s,%s,%s,%s) RETURNING id,display_name,email,role_code',(body.display_name,body.email,dept['id'],body.role_code)).fetchone()
        conn.commit()
    return {**row,'department':body.department}


class EventBody(BaseModel):
    title:str=Field(min_length=1,max_length=200)
    product_id:UUID
    event_type:Literal['launch','promotion','shipment','review','deadline']='promotion'
    start_date:date
    end_date:date|None=None
    owner_id:UUID|None=None
    notes:str|None=None


@router.post('/events',status_code=201)
def add_event(body:EventBody):
    if body.end_date and body.end_date<body.start_date:
        raise HTTPException(422,'End date must not precede start date')
    with connection() as conn:
        row=conn.execute('INSERT INTO operating_events(product_id,title,event_type,start_date,end_date,owner_id,notes) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',(body.product_id,body.title,body.event_type,body.start_date,body.end_date,body.owner_id,body.notes)).fetchone()
        conn.commit()
    return row

@router.get('/products/{product_id}/records')
def operating_records(product_id:UUID,kind:Literal['ads','inventory','finance']):
    table,sort={'ads':('ad_performance_daily','report_date'),'inventory':('inventory_snapshots','snapshot_date'),'finance':('cost_records','effective_date')}[kind]
    with connection() as conn:
        return conn.execute(f'SELECT * FROM {table} WHERE product_id=%s ORDER BY {sort} DESC LIMIT 100',(product_id,)).fetchall()

class ProductDocumentBody(BaseModel):
    task_id:UUID
    title:str=Field(min_length=1,max_length=240)
    url:str=Field(min_length=8,max_length=4000)
    version:str=Field(min_length=1,max_length=80)
    preparation_status:Literal['preparing','prepared','needs_update']='preparing'
    note:str|None=None
    supersedes_id:UUID|None=None


def validate_document_url(url:str)->str:
    from urllib.parse import urlsplit
    value=url.strip()
    try:
        parts=urlsplit(value)
    except ValueError:
        raise HTTPException(422,'Invalid file or folder link')
    if parts.scheme.lower() not in {'http','https'} or not parts.hostname or parts.username or parts.password or any(c.isspace() for c in value):
        raise HTTPException(422,'Use an HTTP(S) file or folder link without embedded credentials')
    return value


@router.post('/products/{product_id}/documents',status_code=201)
def register_product_document(product_id:UUID,body:ProductDocumentBody, actor=Depends(require_user)):
    url=validate_document_url(body.url)
    if not body.title.strip() or not body.version.strip():
        raise HTTPException(422,'Document title and version cannot be blank')
    with connection() as conn:
        task=conn.execute('SELECT t.id,t.owner_id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE t.id=%s AND pw.product_id=%s FOR UPDATE OF t',(body.task_id,product_id)).fetchone()
        if not task:
            raise HTTPException(404,'Task does not belong to this product')
        reject_legacy_process_write(conn, body.task_id)
        if not actor['is_admin'] and task['owner_id']!=actor['id']:
            raise HTTPException(403, '只能为分配给自己的任务登记资料')
        if body.supersedes_id:
            old=conn.execute('SELECT * FROM product_documents WHERE id=%s AND product_id=%s AND task_id=%s FOR UPDATE',(body.supersedes_id,product_id,body.task_id)).fetchone()
            if not old or not old['is_current']:
                raise HTTPException(409,'This document version has changed; reload the checklist')
            conn.execute('UPDATE product_documents SET is_current=false,updated_at=now() WHERE id=%s',(old['id'],))
        else:
            duplicate=conn.execute('SELECT id FROM product_documents WHERE product_id=%s AND task_id=%s AND url=%s AND is_current=true',(product_id,body.task_id,url)).fetchone()
            if duplicate:
                raise HTTPException(409,'This link is already attached; update its existing version')
        evidence=conn.execute("INSERT INTO task_evidence(task_id,evidence_type,uri,note) VALUES(%s,'url',%s,%s) RETURNING id",(body.task_id,url,f'{body.title.strip()} / {body.version.strip()}')).fetchone()
        row=conn.execute('''INSERT INTO product_documents(product_id,task_id,title,url,version,preparation_status,supersedes_id,evidence_id,note)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(product_id,body.task_id,body.title.strip(),url,body.version.strip(),body.preparation_status,body.supersedes_id,evidence['id'],body.note)).fetchone()
        # A changed deliverable must be rechecked; historical approvals remain in the audit trail.
        conn.execute("UPDATE tasks SET revision=revision+1,status=CASE WHEN status='completed' THEN 'in_progress'::task_status ELSE status END,completed_at=NULL,updated_at=now() WHERE id=%s",(body.task_id,))
        conn.execute("INSERT INTO audit_logs(entity_type,entity_id,action,after_data) VALUES('product_document',%s,'version_registered',%s)",(str(row['id']),Jsonb({'task_id':str(body.task_id),'product_id':str(product_id),'title':row['title'],'version':row['version'],'preparation_status':row['preparation_status']})))
        conn.commit()
    return row
