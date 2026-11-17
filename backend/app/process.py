"""Versioned forms, individual assignments and approvals for SOP nodes."""
import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb

from .db import connection
from .process_rules import (Decision, LEGACY_DEPENDENCIES, LEGACY_KEYS, NodeConfig,
                            WorkAction, default_fields, validate_answers)
from .workflow_auth import optional_user, require_admin, require_user

router = APIRouter(prefix='/api/process', tags=['Process collaboration'])


def audit(conn, actor, task_id, action, data=None):
    conn.execute('''INSERT INTO audit_logs(actor_id,entity_type,entity_id,action,after_data)
        VALUES(%s,'process',%s,%s,%s)''', (actor['id'],str(task_id),action,Jsonb(jsonable_encoder(data or {}))))


def task_row(conn, task_id, lock=False):
    if lock:
        product = conn.execute('SELECT pw.product_id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE t.id=%s', (task_id,)).fetchone()
        if not product:
            raise HTTPException(404, '流程节点不存在')
        conn.execute('SELECT id FROM products WHERE id=%s FOR UPDATE', (product['product_id'],))
    row = conn.execute('''SELECT t.*,pw.product_id FROM tasks t JOIN product_workflows pw
        ON pw.id=t.product_workflow_id WHERE t.id=%s''' + (' FOR UPDATE OF t' if lock else ''), (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, '流程节点不存在')
    if lock:
        from .approvals import reject_approval_write
        reject_approval_write(conn, task_id)
    return row


def config_row(conn, task_id):
    return conn.execute('SELECT * FROM process_configs WHERE task_id=%s', (task_id,)).fetchone()


def state_row(conn, task_id):
    return conn.execute('SELECT * FROM process_states WHERE task_id=%s', (task_id,)).fetchone()


def check_version(state, expected):
    if (state['version'] if state else 0) != expected:
        raise HTTPException(409, '节点已被其他人更新，请刷新后重试；本次操作未保存')


def blockers(conn, task_id):
    return conn.execute('''SELECT t.id,t.title,t.status FROM task_dependencies d JOIN tasks t
        ON t.id=d.depends_on_task_id WHERE d.task_id=%s AND t.status NOT IN ('completed','not_applicable')''', (task_id,)).fetchall()


def may_fill(actor, task, config):
    return bool(actor and config and (actor['is_admin'] or actor['id']==task['owner_id'] or actor['id'] in config['collaborator_ids']))


def reject_legacy_process_write(conn, task_id):
    from .approvals import reject_approval_write
    reject_approval_write(conn, task_id)
    if config_row(conn, task_id):
        raise HTTPException(409, '该任务已启用流程协作，请在流程图节点中填写、配置或审核')


def node_data(conn, task, actor):
    config = config_row(conn, task['id'])
    state = state_row(conn, task['id'])
    dependencies = conn.execute('SELECT depends_on_task_id FROM task_dependencies WHERE task_id=%s', (task['id'],)).fetchall()
    config_data = {key:config[key] for key in ('version','fields','reviewer_ids','collaborator_ids','review_mode','location','planned_start')} if config else {
        'version':0,'fields':default_fields(task),'reviewer_ids':[],'collaborator_ids':[],
        'review_mode':'all','location':'','planned_start':None,
    }
    from .approvals import summaries
    approval_summary=summaries(conn,[task['id']]).get(str(task['id']))
    return {**task,'approval_summary':approval_summary,'configured':bool(config),'config':config_data,'state_version':state['version'] if state else 0,
        'dependencies':[x['depends_on_task_id'] for x in dependencies], 'blockers':blockers(conn,task['id']),
        'permissions':{'configure':bool(actor and actor['is_admin']), 'fill':may_fill(actor,task,config),
                       'review':bool(actor and config and actor['id'] in config['reviewer_ids'])}}


@router.get('/products/{product_id}')
def graph(product_id: UUID, actor=Depends(optional_user)):
    with connection() as conn:
        product = conn.execute('SELECT id,model,name,target_launch_date,data_source FROM products WHERE id=%s', (product_id,)).fetchone()
        if not product:
            raise HTTPException(404, '产品不存在')
        tasks = conn.execute('''SELECT t.*,pw.product_id FROM tasks t JOIN product_workflows pw
            ON pw.id=t.product_workflow_id WHERE pw.product_id=%s ORDER BY t.sequence,t.id''', (product_id,)).fetchall()
        nodes = [node_data(conn,t,actor) for t in tasks]
        people = conn.execute('''SELECT u.id,u.display_name,COALESCE(d.name,'项目管理') department,a.is_admin
            FROM workflow_accounts a JOIN app_users u ON u.id=a.user_id LEFT JOIN departments d ON d.id=u.department_id
            WHERE u.is_active ORDER BY a.is_admin DESC,u.display_name''').fetchall()
    return {'product':product,'nodes':nodes,'people':people}


@router.get('/nodes/{task_id}')
def detail(task_id: UUID, actor=Depends(optional_user)):
    with connection() as conn:
        node = node_data(conn,task_row(conn,task_id),actor)
        state = state_row(conn,task_id)
        submissions,activity,history = [],[],[]
        if actor:
            submissions = conn.execute('''SELECT s.*,u.display_name submitted_name FROM process_submissions s
                JOIN app_users u ON u.id=s.submitted_by WHERE task_id=%s ORDER BY s.created_at DESC LIMIT 30''', (task_id,)).fetchall()
            for submission in submissions:
                submission['decisions'] = conn.execute('''SELECT d.*,u.display_name reviewer_name FROM process_decisions d
                    JOIN app_users u ON u.id=d.reviewer_id WHERE submission_id=%s ORDER BY d.created_at''', (submission['id'],)).fetchall()
            activity = conn.execute('''SELECT a.action,a.after_data,a.created_at,u.display_name FROM audit_logs a
                LEFT JOIN app_users u ON u.id=a.actor_id WHERE entity_type='process' AND entity_id=%s
                ORDER BY a.created_at DESC,a.id DESC LIMIT 50''', (str(task_id),)).fetchall()
            history = conn.execute('SELECT version,snapshot,created_at FROM process_config_history WHERE task_id=%s ORDER BY version DESC LIMIT 30', (task_id,)).fetchall()
    return {'node':node,'answers':state['draft'] if actor and state else {},'submissions':submissions,'activity':activity,'config_history':history}


def write_config(conn, task, body, actor):
    from .approvals import reject_approval_write
    reject_approval_write(conn, task['id'])
    state = state_row(conn,task['id'])
    check_version(state,body.expected_version)
    if task['status'] in {'completed','not_applicable'}:
        raise HTTPException(409, '已完成节点保留当时记录，请先重新打开再修改配置')
    if task['status']=='pending_approval' and not body.recall_pending:
        raise HTTPException(409, '该节点正在审核，请确认撤回本次提交后再修改配置')
    ids = {body.owner_id,*body.collaborator_ids,*body.reviewer_ids}
    active = {r['user_id'] for r in conn.execute('''SELECT a.user_id FROM workflow_accounts a
        JOIN app_users u ON u.id=a.user_id WHERE u.is_active AND a.user_id=ANY(%s)''', (list(ids),)).fetchall()}
    if ids-active:
        raise HTTPException(422, '负责人、协作人和审核人必须具有有效的登录账户')
    tasks = conn.execute('''SELECT t.id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id
        WHERE pw.product_id=%s''', (task['product_id'],)).fetchall()
    if set(body.dependencies)-{t['id'] for t in tasks} or task['id'] in body.dependencies:
        raise HTTPException(422, '前置节点必须来自当前产品，且不能选择自身')
    for prerequisite in body.dependencies:
        cycle = conn.execute('''WITH RECURSIVE chain(id) AS (
            SELECT depends_on_task_id FROM task_dependencies WHERE task_id=%s
            UNION SELECT d.depends_on_task_id FROM task_dependencies d JOIN chain c ON d.task_id=c.id
            ) SELECT 1 FROM chain WHERE id=%s''', (prerequisite,task['id'])).fetchone()
        if cycle:
            raise HTTPException(409, '前置关系会形成循环，请调整连接')
    previous = config_row(conn,task['id'])
    version = previous['version']+1 if previous else 1
    fields = [f.model_dump() for f in body.fields]
    conn.execute('''INSERT INTO process_configs(task_id,version,fields,reviewer_ids,collaborator_ids,review_mode,location,planned_start,updated_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(task_id) DO UPDATE SET version=EXCLUDED.version,
        fields=EXCLUDED.fields,reviewer_ids=EXCLUDED.reviewer_ids,collaborator_ids=EXCLUDED.collaborator_ids,
        review_mode=EXCLUDED.review_mode,location=EXCLUDED.location,planned_start=EXCLUDED.planned_start,
        updated_by=EXCLUDED.updated_by,updated_at=now()''',
        (task['id'],version,Jsonb(fields),body.reviewer_ids,body.collaborator_ids,body.review_mode,body.location,body.planned_start,actor['id']))
    # Retain draft values only for still-existing fields; old submissions keep a full snapshot.
    draft = {k:v for k,v in (state['draft'] if state else {}).items() if k in {f.id for f in body.fields}}
    conn.execute('''INSERT INTO process_states(task_id,version,draft) VALUES(%s,1,%s)
        ON CONFLICT(task_id) DO UPDATE SET version=process_states.version+1,draft=EXCLUDED.draft''', (task['id'],Jsonb(draft)))
    conn.execute('DELETE FROM task_dependencies WHERE task_id=%s', (task['id'],))
    for prerequisite in body.dependencies:
        conn.execute('INSERT INTO task_dependencies VALUES(%s,%s)', (task['id'],prerequisite))
    conn.execute('''UPDATE tasks SET owner_id=%s,due_date=%s,approval_required=true,revision=revision+1,
        status=CASE WHEN status='pending_approval' THEN 'in_progress'::task_status ELSE status END,updated_at=now() WHERE id=%s''', (body.owner_id,body.due_date,task['id']))
    snapshot = body.model_dump(exclude={'expected_version','recall_pending'})
    conn.execute('INSERT INTO process_config_history(task_id,version,snapshot,actor_id) VALUES(%s,%s,%s,%s)', (task['id'],version,Jsonb(jsonable_encoder(snapshot)),actor['id']))
    audit(conn,actor,task['id'],'configured',{'config_version':version,'recalled_submission':task['status']=='pending_approval'})


@router.put('/nodes/{task_id}/config')
def configure(task_id: UUID, body: NodeConfig, actor=Depends(require_admin)):
    with connection() as conn:
        task = task_row(conn,task_id,lock=True)
        write_config(conn,task,body,actor)
        conn.commit()
    return {'ok':True}


@router.post('/products/{product_id}/enable')
def enable(product_id: UUID, actor=Depends(require_admin)):
    with connection() as conn:
        if not conn.execute('SELECT id FROM products WHERE id=%s FOR UPDATE', (product_id,)).fetchone():
            raise HTTPException(404, '产品不存在')
        tasks = conn.execute('''SELECT t.*,pw.product_id FROM tasks t JOIN product_workflows pw
            ON pw.id=t.product_workflow_id WHERE pw.product_id=%s ORDER BY sequence FOR UPDATE OF t''', (product_id,)).fetchall()
        # Legacy seed tasks had no edges. Establish the explicitly documented launch dependencies once.
        by_key = {LEGACY_KEYS.get(t['title']):t['id'] for t in tasks if t['title'] in LEGACY_KEYS}
        any_edges = conn.execute('''SELECT 1 FROM task_dependencies d JOIN tasks t ON t.id=d.task_id
            JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE pw.product_id=%s LIMIT 1''', (product_id,)).fetchone()
        if not any_edges:
            for key,prerequisites in LEGACY_DEPENDENCIES.items():
                for prerequisite in prerequisites:
                    if key in by_key and prerequisite in by_key:
                        conn.execute('INSERT INTO task_dependencies VALUES(%s,%s) ON CONFLICT DO NOTHING', (by_key[key],by_key[prerequisite]))
        accounts = {x['user_id'] for x in conn.execute('SELECT user_id FROM workflow_accounts').fetchall()}
        enabled=0
        for task in tasks:
            if config_row(conn,task['id']) or task['status'] in {'completed','not_applicable'}:
                continue
            deps = conn.execute('SELECT depends_on_task_id FROM task_dependencies WHERE task_id=%s', (task['id'],)).fetchall()
            write_config(conn,task,NodeConfig(expected_version=0,fields=default_fields(task),
                owner_id=task['owner_id'] if task['owner_id'] in accounts else actor['id'],reviewer_ids=[actor['id']],
                due_date=task['due_date'],location={'inventory':'供应商 / FBA 仓库','listing':'线上协作 · 设计与运营','compliance':'产品与合规资料室'}.get(task['module_code'],'线上协作'),
                dependencies=[x['depends_on_task_id'] for x in deps],recall_pending=True),actor)
            enabled+=1
        conn.commit()
    return {'enabled':enabled}


def reopen(conn, task, actor, reason):
    if not actor['is_admin']:
        raise HTTPException(403, '只有流程管理员可以重新打开节点')
    if not reason.strip():
        raise HTTPException(422, '请填写重新打开的原因')
    downstream = conn.execute('''WITH RECURSIVE chain(id) AS (
        SELECT task_id FROM task_dependencies WHERE depends_on_task_id=%s
        UNION SELECT d.task_id FROM task_dependencies d JOIN chain c ON d.depends_on_task_id=c.id)
        SELECT t.id FROM tasks t JOIN chain c ON c.id=t.id WHERE t.status IN ('in_progress','pending_approval','completed')''', (task['id'],)).fetchall()
    from .approvals import reject_approval_write
    for item in downstream:
        reject_approval_write(conn,item['id'])
    ids = [task['id']]+[x['id'] for x in downstream]
    for node_id in ids:
        conn.execute("UPDATE tasks SET revision=revision+1,status='in_progress',completed_at=NULL,updated_at=now() WHERE id=%s", (node_id,))
        conn.execute('UPDATE process_states SET version=version+1 WHERE task_id=%s', (node_id,))
        audit(conn,actor,node_id,'reopened',{'reason':reason,'source_task_id':str(task['id'])})
    return len(ids)


@router.post('/nodes/{task_id}/work')
def work(task_id: UUID, body: WorkAction, actor=Depends(require_user)):
    with connection() as conn:
        task = task_row(conn,task_id,lock=True)
        config = config_row(conn,task_id)
        state = state_row(conn,task_id)
        if body.action=='reopen':
            check_version(state,body.expected_version)
            affected = reopen(conn,task,actor,body.reason)
            conn.commit()
            return {'ok':True,'affected':affected}
        if not config:
            raise HTTPException(409, '请先由管理员配置该节点')
        check_version(state,body.expected_version)
        if not may_fill(actor,task,config):
            raise HTTPException(403, '只有负责人、协作人或流程管理员可以填写该节点')
        if task['status'] in {'pending_approval','completed','not_applicable'}:
            raise HTTPException(409, '审核中或已完成的内容不可覆盖，请撤回或重新打开后填写')
        member_ids={str(r['user_id']) for r in conn.execute('SELECT user_id FROM workflow_accounts').fetchall()}
        files={str(r['id']) for r in conn.execute('SELECT id FROM process_attachments WHERE task_id=%s', (task_id,)).fetchall()}
        submitting=body.action=='submit'
        validate_answers(config['fields'],body.answers,submitting,member_ids,files)
        blocked = blockers(conn,task_id)
        if submitting and blocked:
            raise HTTPException(409, '前置节点尚未完成：'+'、'.join(x['title'] for x in blocked))
        if submitting:
            active_reviewers=conn.execute('''SELECT a.user_id FROM workflow_accounts a JOIN app_users u ON u.id=a.user_id
                WHERE a.user_id=ANY(%s) AND u.is_active''',(config['reviewer_ids'],)).fetchall()
            if len(active_reviewers)!=len(config['reviewer_ids']):
                raise HTTPException(409, '审核人员账户已失效，请管理员重新配置')
        next_status='pending_approval' if submitting else ('not_started' if blocked and task['status']=='not_started' else 'in_progress')
        task=conn.execute('''UPDATE tasks SET revision=revision+1,status=%s,
            started_at=CASE WHEN %s THEN started_at ELSE COALESCE(started_at,now()) END,updated_at=now() WHERE id=%s RETURNING *''', (next_status,bool(blocked),task_id)).fetchone()
        conn.execute('UPDATE process_states SET version=version+1,draft=%s WHERE task_id=%s', (Jsonb(body.answers),task_id))
        if submitting:
            snapshot={**config,'title':task['title'],'owner_id':task['owner_id'],'due_date':task['due_date']}
            conn.execute('''INSERT INTO process_submissions(task_id,task_revision,config_version,snapshot,answers,submitted_by)
                VALUES(%s,%s,%s,%s,%s,%s)''', (task_id,task['revision'],config['version'],Jsonb(jsonable_encoder(snapshot)),Jsonb(body.answers),actor['id']))
        audit(conn,actor,task_id,'submitted' if submitting else 'draft_saved',{'task_revision':task['revision']})
        conn.commit()
    return {'ok':True}


@router.post('/nodes/{task_id}/review')
def review(task_id: UUID, body: Decision, actor=Depends(require_user)):
    with connection() as conn:
        task=task_row(conn,task_id,lock=True)
        config=config_row(conn,task_id)
        if not config or actor['id'] not in config['reviewer_ids']:
            raise HTTPException(403, '只有本节点指定的审核人可以审核')
        check_version(state_row(conn,task_id),body.expected_version)
        submission=conn.execute('SELECT * FROM process_submissions WHERE id=%s AND task_id=%s', (body.submission_id,task_id)).fetchone()
        if not submission or submission['task_revision']!=task['revision'] or submission['config_version']!=config['version'] or task['status']!='pending_approval':
            raise HTTPException(409, '提交版本已变化或不在待审核状态，请重新读取')
        decisions=conn.execute('SELECT * FROM process_decisions WHERE submission_id=%s', (submission['id'],)).fetchall()
        decided={d['reviewer_id'] for d in decisions}
        if actor['id'] in decided:
            raise HTTPException(409, '你已审核过本次提交')
        if config['review_mode']=='sequential':
            next_reviewer=next((x for x in config['reviewer_ids'] if x not in decided),None)
            if actor['id']!=next_reviewer:
                raise HTTPException(409, '尚未轮到你审核，请等待前一位审核人')
        if body.decision=='rejected' and not body.comment.strip():
            raise HTTPException(422, '退回时必须填写需要补充或修改的内容')
        if body.decision=='approved' and blockers(conn,task_id):
            raise HTTPException(409, '前置节点尚未完成，不能通过验收')
        conn.execute('INSERT INTO process_decisions(submission_id,reviewer_id,decision,comment) VALUES(%s,%s,%s,%s)', (submission['id'],actor['id'],body.decision,body.comment.strip()))
        approved=body.decision=='approved' and (config['review_mode']=='any' or len(decided)+1==len(config['reviewer_ids']))
        if approved or body.decision=='rejected':
            conn.execute("UPDATE tasks SET status=%s,completed_at=CASE WHEN %s THEN now() ELSE NULL END,updated_at=now() WHERE id=%s", ('completed' if approved else 'in_progress',approved,task_id))
            conn.execute('''INSERT INTO approvals(task_id,approver_id,decision,comment,decided_at,task_revision)
                VALUES(%s,%s,%s,%s,now(),%s)''', (task_id,actor['id'],body.decision,body.comment,task['revision']))
        conn.execute('UPDATE process_states SET version=version+1 WHERE task_id=%s', (task_id,))
        audit(conn,actor,task_id,'approved' if body.decision=='approved' else 'rejected',{'submission_id':str(submission['id']),'comment':body.comment,'completed':approved})
        conn.commit()
    return {'ok':True,'completed':approved}


def upload_dir():
    return Path(os.getenv('WORKFLOW_UPLOAD_DIR','/var/run/amazon-agent/workflow-uploads'))


@router.post('/nodes/{task_id}/attachments',status_code=201)
def upload(task_id: UUID, file: UploadFile = File(...), actor=Depends(require_user)):
    file_id=uuid4()
    target=upload_dir()/str(file_id)
    try:
        with connection() as conn:
            task=task_row(conn,task_id,lock=True)
            config=config_row(conn,task_id)
            if not may_fill(actor,task,config):
                raise HTTPException(403, '你没有此节点的附件提交权限')
            if task['status'] in {'pending_approval','completed','not_applicable'}:
                raise HTTPException(409, '当前节点不可修改附件')
            name=(file.filename or '附件').replace('\\','/').rsplit('/',1)[-1]
            name=''.join(c for c in name if c.isprintable())[:200] or '附件'
            upload_dir().mkdir(parents=True,exist_ok=True)
            size=0
            digest=hashlib.sha256()
            with target.open('xb') as output:
                os.chmod(target,0o600)
                while chunk := file.file.read(65536):
                    size+=len(chunk)
                    if size>10*1024*1024:
                        raise HTTPException(413, '附件不能超过10 MB')
                    digest.update(chunk)
                    output.write(chunk)
            if size==0:
                raise HTTPException(422, '不能上传空文件')
            conn.execute('INSERT INTO process_attachments(id,task_id,filename,size,sha256,uploaded_by) VALUES(%s,%s,%s,%s,%s,%s)', (file_id,task_id,name,size,digest.hexdigest(),actor['id']))
            audit(conn,actor,task_id,'attachment_uploaded',{'filename':name,'size':size})
            conn.commit()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()
    return {'kind':'file','id':file_id,'title':name,'version':'v1'}


@router.get('/attachments/{file_id}')
def download(file_id: UUID, actor=Depends(require_user)):
    with connection() as conn:
        file=conn.execute('SELECT * FROM process_attachments WHERE id=%s', (file_id,)).fetchone()
    if not file or not (upload_dir()/str(file_id)).is_file():
        raise HTTPException(404, '附件不存在')
    return FileResponse(upload_dir()/str(file_id),filename=file['filename'],media_type='application/octet-stream',headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})
