"""Product SOP approval cases with immutable plans, stage handoffs and audit events."""
import copy
import hashlib
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from psycopg.types.json import Jsonb

from .db import connection
from .process_rules import FormField, validate_answers
from .workflow_auth import optional_user, require_admin, require_user
from .approval_templates import template_for

router=APIRouter(prefix='/api/approvals',tags=['SOP approval cases'])


def enc(value):
    return jsonable_encoder(value)


def now():
    return datetime.now(timezone.utc).isoformat()


class StageConfig(BaseModel):
    model_config=ConfigDict(extra='forbid')
    key:str=Field(pattern=r'^[a-z][a-z0-9_\-]{0,63}$')
    name:str=Field(min_length=1,max_length=150)
    type:Literal['application','approval','handling','cc','end']
    fields:list[FormField]=Field(default_factory=list,max_length=30)
    assignee_ids:list[UUID]=Field(default_factory=list,max_length=30)
    reviewer_ids:list[UUID]=Field(default_factory=list,max_length=20)
    collaborator_ids:list[UUID]=Field(default_factory=list,max_length=30)
    review_mode:Literal['all','any','sequential']='all'
    planned_start:date|None=None
    planned_end:date|None=None
    return_to:str|None=None

    @model_validator(mode='after')
    def valid(self):
        if not self.name.strip():raise ValueError('阶段名称不能为空')
        if self.planned_start and self.planned_end and self.planned_start>self.planned_end:raise ValueError('阶段开始时间不能晚于完成时间')
        for ids in (self.assignee_ids,self.reviewer_ids,self.collaborator_ids):
            if len(ids)!=len(set(ids)):raise ValueError('同一阶段人员不能重复')
        if len({f.id for f in self.fields})!=len(self.fields):raise ValueError('阶段填写项目标识不能重复')
        return self


class Configure(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version:int=Field(ge=0)
    fields:list[FormField]=Field(default_factory=list,max_length=40)
    stages:list[StageConfig]=Field(min_length=2,max_length=30)
    recall_pending:bool=False
    reason:str=Field(default='',max_length=2000)
    dependencies:list[UUID]|None=Field(default=None,max_length=100)

    @model_validator(mode='after')
    def valid(self):
        keys=[s.key for s in self.stages]
        if len(keys)!=len(set(keys)):raise ValueError('阶段标识不能重复')
        if self.stages[0].type!='application':raise ValueError('首个阶段必须为申请阶段')
        if not any(s.type=='approval' for s in self.stages):raise ValueError('流程至少包含一个审核阶段')
        if next((s.type for s in reversed(self.stages) if s.type not in {'cc','end'}),None)!='approval':raise ValueError('最后一个业务阶段必须是最终审核')
        for index,s in enumerate(self.stages):
            if s.return_to and (s.return_to not in keys[:index] or next(x for x in self.stages if x.key==s.return_to).type not in {'application','handling'}):raise ValueError('退回目标必须是前面的申请或办理阶段')
            if s.type=='end' and index!=len(keys)-1:raise ValueError('结束节点只能放在最后')
        if len({f.id for f in self.fields})!=len(self.fields):raise ValueError('申请表项目标识不能重复')
        return self


class Action(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version:int=Field(ge=0)
    stage_key:str|None=None
    action:Literal['save','submit','accept','complete','approve','reject','reopen']
    answers:dict[str,Any]=Field(default_factory=dict)
    application_answers:dict[str,Any]=Field(default_factory=dict)
    forecast_end:date|None=None
    delay_reason:str=Field(default='',max_length=2000)
    next_action:str=Field(default='',max_length=2000)
    external_event_at:datetime|None=None
    comment:str=Field(default='',max_length=4000)
    return_to:str|None=None


def _task(conn,task_id,lock=False):
    product=conn.execute('SELECT pw.product_id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE t.id=%s',(task_id,)).fetchone()
    if not product:raise HTTPException(404,'SOP 事项不存在')
    if lock:conn.execute('SELECT id FROM products WHERE id=%s FOR UPDATE',(product['product_id'],))
    return conn.execute('SELECT t.*,pw.product_id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE t.id=%s'+(' FOR UPDATE OF t' if lock else ''),(task_id,)).fetchone()


def _config(conn,task):
    stored=conn.execute('SELECT * FROM approval_configs WHERE task_id=%s',(task['id'],)).fetchone()
    dependencies=[str(x['depends_on_task_id']) for x in conn.execute('SELECT depends_on_task_id FROM task_dependencies WHERE task_id=%s',(task['id'],)).fetchall()]
    if stored:return {**stored['definition'],'version':stored['version'],'dependencies':dependencies}
    template=copy.deepcopy(template_for(task))
    legacy=conn.execute('SELECT * FROM process_configs WHERE task_id=%s',(task['id'],)).fetchone()
    for step in template['stages']:
        step.setdefault('fields',[])
        step.setdefault('assignee_ids',[str(task['owner_id'])] if task.get('owner_id') else [])
        step.setdefault('reviewer_ids',[str(x) for x in legacy['reviewer_ids']] if legacy and step['type']=='approval' else [])
        step.setdefault('collaborator_ids',[])
        step.setdefault('review_mode','all')
        step.setdefault('planned_start',None)
        step.setdefault('planned_end',None)
        step.setdefault('return_to',None)
    if task.get('due_date'):template['stages'][-1]['planned_end']=str(task['due_date'])
    template['version']=0
    template['dependencies']=dependencies
    return template


def _case(conn,task_id):
    return conn.execute('SELECT * FROM approval_cases WHERE task_id=%s',(task_id,)).fetchone()


def _event(conn,task_id,case,actor,action,stage_key=None,payload=None):
    conn.execute('INSERT INTO approval_events(task_id,case_id,stage_key,action,actor_id,payload) VALUES(%s,%s,%s,%s,%s,%s)',
                 (task_id,case['id'] if case else None,stage_key,action,actor['id'],Jsonb(enc(payload or {}))))


def _check_version(actual,expected):
    if actual!=expected:raise HTTPException(409,'审批记录已被其他人更新，请刷新后重试；本次操作未保存')


def _runtime(config,previous=None):
    old={s['key']:s for s in previous or []}
    steps=[]
    for index,stage in enumerate(config['stages']):
        old_step=old.get(stage['key'],{})
        steps.append({**copy.deepcopy(stage),'status':'pending' if index==0 else 'waiting',
            'answers':{k:copy.deepcopy(v) for k,v in old_step.get('answers',{}).items() if k in {f['id'] for f in stage['fields']}},
            'baseline_start':old_step.get('baseline_start') or stage.get('baseline_start') or stage.get('planned_start'),
            'baseline_end':old_step.get('baseline_end') or stage.get('baseline_end') or stage.get('planned_end'),
            'forecast_end':old_step.get('forecast_end',stage.get('planned_end')),
            'received_at':now() if index==0 else None,'started_at':None,'completed_at':None,
            'delay_reason':old_step.get('delay_reason',''),'next_action':old_step.get('next_action',''),
            'external_event_at':None,'decisions':[]})
    return steps


def _create(conn,task,config,actor):
    case_id=uuid4()
    return conn.execute('''INSERT INTO approval_cases(id,task_id,number,config_version,definition,stages,current_stage_key,initiator_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(case_id,task['id'],'SOP-'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d')+'-'+case_id.hex[:10].upper(),
        config['version'],Jsonb(enc(config)),Jsonb(enc(_runtime(config))),config['stages'][0]['key'],actor['id'])).fetchone()


def _save_case(conn,case):
    conn.execute('''UPDATE approval_cases SET version=version+1,status=%s,stages=%s,current_stage_key=%s,
        application_answers=%s,definition=%s,config_version=%s,submitted_at=%s,completed_at=%s WHERE id=%s''',
        (case['status'],Jsonb(enc(case['stages'])),case['current_stage_key'],Jsonb(enc(case['application_answers'])),
         Jsonb(enc(case['definition'])),case['config_version'],case.get('submitted_at'),case.get('completed_at'),case['id']))


def _ids(step,review=False):
    return set(step.get('reviewer_ids',[]) if review else step.get('assignee_ids',[])+step.get('collaborator_ids',[]))


def _assigned(step,actor,review=False):
    if str(actor['id']) not in _ids(step,review):
        raise HTTPException(403,'只有本环节指定的'+('审核人' if review else '办理人或协作人')+'可以处理；管理员配置身份不提供代办或审核权')


def _blocked(conn,task_id):
    return conn.execute("SELECT t.id,t.title FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_task_id WHERE d.task_id=%s AND t.status NOT IN ('completed','not_applicable')",(task_id,)).fetchall()


def _validate_all_assignees(conn,steps):
    required=set()
    for s in steps:
        if s.get('status')=='completed':continue
        ids=_ids(s,s['type']=='approval')
        if s['type'] not in {'cc','end'} and not ids:raise HTTPException(409,'请管理员先指定该阶段人员：'+s['name'])
        required.update(ids)
    active={str(r['user_id']) for r in conn.execute('SELECT a.user_id FROM workflow_accounts a JOIN app_users u ON u.id=a.user_id WHERE u.is_active').fetchall()}
    if required-active:raise HTTPException(409,'流程中存在无有效登录账户的人员，请管理员调整')


def _validate_form(conn,case,step,fields,answers,submitting):
    member_ids={str(r['user_id']) for r in conn.execute('SELECT a.user_id FROM workflow_accounts a JOIN app_users u ON u.id=a.user_id WHERE u.is_active').fetchall()}
    files={str(r['id']) for r in conn.execute('SELECT id FROM approval_files WHERE case_id=%s AND stage_key=%s',(case['id'],step['key'])).fetchall()}
    validate_answers(fields,answers,submitting,member_ids,files)


def _advance(conn,task,case,actor):
    current=next(i for i,s in enumerate(case['stages']) if s['key']==case['current_stage_key'])
    index=current+1
    while index<len(case['stages']):
        step=case['stages'][index]
        step['received_at']=now()
        if step['type'] in {'cc','end'}:
            step.update(status='completed',started_at=now(),completed_at=now())
            _event(conn,task['id'],case,actor,'notified' if step['type']=='cc' else 'ended',step['key'],{'recipient_ids':step.get('assignee_ids',[])})
            index+=1
        else:
            step['status']='pending';case['current_stage_key']=step['key'];return
    case.update(status='completed',current_stage_key=None,completed_at=now())
    conn.execute("UPDATE tasks SET status='completed',completed_at=now(),updated_at=now(),revision=revision+1 WHERE id=%s",(task['id'],))


def reject_approval_write(conn,task_id):
    if conn.execute('SELECT 1 FROM tasks WHERE id=%s',(task_id,)).fetchone():
        raise HTTPException(409,'该事项已使用逐阶段审批，请在全流程 SOP 审批单内处理')


def summaries(conn,task_ids):
    if not task_ids:return {}
    tasks=conn.execute('SELECT t.*,pw.product_id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE t.id=ANY(%s::uuid[])',(task_ids,)).fetchall()
    rows=conn.execute('SELECT * FROM approval_cases WHERE task_id=ANY(%s::uuid[])',(task_ids,)).fetchall()
    result={}
    for task in tasks:
        config=_config(conn,task);first=config['stages'][0];completed=task['status'] in {'completed','not_applicable'}
        result[str(task['id'])]={'case_id':None,'number':None,'status':'legacy_completed' if completed else 'not_started','current_stage':'旧版已完成 · 可查看历史' if completed else first['name']+' · 待发起','current_stage_key':None if completed else first['key'],'current_assignees':[] if completed else list(_ids(first)), 'original_due':config['stages'][-1].get('baseline_end',config['stages'][-1].get('planned_end')),'forecast_end':None,'overdue':bool(not completed and task.get('due_date') and task['due_date']<datetime.now(ZoneInfo('Asia/Shanghai')).date()),'version':0}
    for c in rows:
        current=next((s for s in c['stages'] if s['key']==c['current_stage_key']),None)
        baseline=c['stages'][-1].get('baseline_end')
        forecast=c['stages'][-1].get('forecast_end')
        result[str(c['task_id'])]={'case_id':str(c['id']),'number':c['number'],'status':c['status'],
            'current_stage':current['name'] if current else '已完成归档','current_stage_key':c['current_stage_key'],
            'current_assignees':list(_ids(current,current['type']=='approval')) if current else [],
            'original_due':baseline,'forecast_end':forecast,
            'overdue':bool(c['status']!='completed' and any(
                s.get('baseline_end') and (s.get('forecast_end') and s['forecast_end']>s['baseline_end']
                or s.get('status')!='completed' and s['baseline_end']<datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
                for s in c['stages'])),'version':c['version']}
    return result


@router.get('/tasks/{task_id}')
def detail(task_id:UUID,actor=Depends(optional_user)):
    with connection() as conn:
        task=_task(conn,task_id);product=conn.execute('SELECT * FROM products WHERE id=%s',(task['product_id'],)).fetchone()
        config=_config(conn,task);case=_case(conn,task_id)
        people=conn.execute('''SELECT u.id,u.display_name,u.email,COALESCE(d.name,'项目管理') department,a.is_admin
            FROM workflow_accounts a JOIN app_users u ON u.id=a.user_id LEFT JOIN departments d ON d.id=u.department_id WHERE u.is_active ORDER BY a.is_admin DESC,u.display_name''').fetchall()
        history=[];legacy={}
        if actor:
            history=conn.execute('SELECT e.*,u.display_name actor_name FROM approval_events e JOIN app_users u ON u.id=e.actor_id WHERE task_id=%s ORDER BY e.id DESC LIMIT 150',(task_id,)).fetchall()
            legacy['submissions']=conn.execute('SELECT * FROM process_submissions WHERE task_id=%s ORDER BY created_at DESC',(task_id,)).fetchall()
            for s in legacy['submissions']:
                s['decisions']=conn.execute('SELECT * FROM process_decisions WHERE submission_id=%s',(s['id'],)).fetchall()
            legacy['config']=conn.execute('SELECT * FROM process_configs WHERE task_id=%s',(task_id,)).fetchone()
            legacy['state']=conn.execute('SELECT * FROM process_states WHERE task_id=%s',(task_id,)).fetchone()
            legacy['config_history']=conn.execute('SELECT * FROM process_config_history WHERE task_id=%s ORDER BY version DESC',(task_id,)).fetchall()
            legacy['documents']=conn.execute('SELECT * FROM product_documents WHERE task_id=%s ORDER BY created_at DESC',(task_id,)).fetchall()
            legacy['product_documents']=legacy['documents']
            legacy['evidence']=conn.execute('SELECT * FROM task_evidence WHERE task_id=%s ORDER BY created_at DESC',(task_id,)).fetchall()
            legacy['approvals']=conn.execute('SELECT * FROM approvals WHERE task_id=%s ORDER BY decided_at DESC NULLS LAST',(task_id,)).fetchall()
        has_case=bool(case)
        if not actor:
            case=None
            people=[{k:v for k,v in p.items() if k!='email'} for p in people]
        return {'task':task,'product':product,'template':template_for(task),'config':config,'case':case,'has_case':has_case,
                'people':people,'user':actor,'permissions':{'configure':bool(actor and actor['is_admin'])},'history':history,'legacy':legacy,
                'blockers':_blocked(conn,task_id),'dependencies':config.get('dependencies',[])}


@router.put('/tasks/{task_id}/config')
def configure(task_id:UUID,body:Configure,actor=Depends(require_admin)):
    with connection() as conn:
        task=_task(conn,task_id,True);previous=_config(conn,task);case=_case(conn,task_id)
        _check_version(previous['version'],body.expected_version)
        if task['status'] in {'completed','not_applicable'}:raise HTTPException(409,'已完成事项须先说明原因重新打开')
        if case and case['status']=='in_progress' and (not body.recall_pending or not body.reason.strip()):
            raise HTTPException(409,'流程正在运行；变更配置需明确撤回并填写原因，旧审批和提交会保留')
        ids={str(x) for s in body.stages for x in s.assignee_ids+s.collaborator_ids+s.reviewer_ids}
        active={str(r['user_id']) for r in conn.execute('SELECT a.user_id FROM workflow_accounts a JOIN app_users u ON u.id=a.user_id WHERE u.is_active').fetchall()}
        if ids-active:raise HTTPException(422,'所配置人员必须具有有效登录账户')
        definition={**copy.deepcopy(template_for(task)),'fields':[f.model_dump() for f in body.fields],
                    'stages':[enc(s.model_dump()) for s in body.stages],'version':previous['version']+1}
        previous_steps={s['key']:s for s in previous['stages']}
        if previous['version'] and any((step.get('planned_start'),step.get('planned_end'))!=(previous_steps.get(step['key'],{}).get('planned_start'),previous_steps.get(step['key'],{}).get('planned_end')) for step in definition['stages']) and not body.reason.strip():
            raise HTTPException(422,'调整计划时间请填写修改原因，原始计划会保留')
        dependencies=body.dependencies if body.dependencies is not None else [UUID(x) for x in previous.get('dependencies',[])]
        for dependency in dependencies:
            target=_task(conn,dependency)
            if target['product_id']!=task['product_id'] or dependency==task_id:raise HTTPException(422,'前置事项必须为同一产品的其他事项')
            if conn.execute('WITH RECURSIVE chain(id) AS (SELECT depends_on_task_id FROM task_dependencies WHERE task_id=%s UNION SELECT d.depends_on_task_id FROM task_dependencies d JOIN chain c ON c.id=d.task_id) SELECT 1 FROM chain WHERE id=%s',(dependency,task_id)).fetchone():raise HTTPException(409,'前置事项会形成循环')
        definition['dependencies']=[str(x) for x in dependencies]
        conn.execute('DELETE FROM task_dependencies WHERE task_id=%s',(task_id,))
        for dependency in dependencies:conn.execute('INSERT INTO task_dependencies VALUES(%s,%s) ON CONFLICT DO NOTHING',(task_id,dependency))
        for step in definition['stages']:
            old=previous_steps.get(step['key'],{}) if previous['version'] else {}
            step['baseline_start']=old.get('baseline_start') or old.get('planned_start') or step.get('planned_start')
            step['baseline_end']=old.get('baseline_end') or old.get('planned_end') or step.get('planned_end')
        conn.execute('''INSERT INTO approval_configs(task_id,version,definition,updated_by) VALUES(%s,%s,%s,%s)
            ON CONFLICT(task_id) DO UPDATE SET version=EXCLUDED.version,definition=EXCLUDED.definition,updated_by=EXCLUDED.updated_by,updated_at=now()''',
            (task_id,definition['version'],Jsonb(enc(definition)),actor['id']))
        conn.execute('INSERT INTO approval_config_history(task_id,version,definition,actor_id) VALUES(%s,%s,%s,%s)',(task_id,definition['version'],Jsonb(enc(definition)),actor['id']))
        _event(conn,task_id,case,actor,'configured',payload={'previous_config':previous,'config':definition,'reason':body.reason,'previous_case':case})
        if case:
            case.update(definition=definition,config_version=definition['version'],stages=_runtime(definition,case['stages']),current_stage_key=definition['stages'][0]['key'],status='draft',completed_at=None)
            case['application_answers']={k:v for k,v in case['application_answers'].items() if k in {f['id'] for f in definition['fields']}}
            _save_case(conn,case)
            conn.execute("UPDATE tasks SET status='in_progress',completed_at=NULL,revision=revision+1,updated_at=now() WHERE id=%s",(task_id,))
        conn.commit()
    return {'ok':True,'version':definition['version']}


@router.post('/tasks/{task_id}/actions')
def act(task_id:UUID,body:Action,actor=Depends(require_user)):
    with connection() as conn:
        task=_task(conn,task_id,True);config=_config(conn,task);case=_case(conn,task_id)
        _check_version(case['version'] if case else 0,body.expected_version)
        if body.action=='reopen':
            if not actor['is_admin']:raise HTTPException(403,'只有流程管理员可以重新打开审批单')
            if not body.comment.strip():raise HTTPException(422,'重新打开需填写原因')
            descendants=conn.execute('''WITH RECURSIVE chain(id) AS (SELECT task_id FROM task_dependencies WHERE depends_on_task_id=%s
                UNION SELECT d.task_id FROM task_dependencies d JOIN chain c ON d.depends_on_task_id=c.id)
                SELECT t.title FROM tasks t JOIN chain c ON c.id=t.id LEFT JOIN approval_cases ac ON ac.task_id=t.id WHERE t.status IN ('in_progress','completed','pending_approval') AND COALESCE(ac.status,'')<>%s''',(task_id,'draft')).fetchall()
            if descendants:raise HTTPException(409,'请先重新打开已进入验收的后续事项：'+'、'.join(x['title'] for x in descendants))
            old=copy.deepcopy(case)
            if not case:case=_create(conn,task,config,actor)
            case.update(stages=_runtime(case['definition'],case['stages']),status='draft',current_stage_key=case['definition']['stages'][0]['key'],completed_at=None)
            _event(conn,task_id,case,actor,'reopened',payload={'reason':body.comment,'previous_case':old})
            _save_case(conn,case)
            conn.execute("UPDATE tasks SET status='in_progress',completed_at=NULL,revision=revision+1,updated_at=now() WHERE id=%s",(task_id,))
            conn.commit();return {'ok':True}
        if task['status'] in {'completed','not_applicable'}:raise HTTPException(409,'已完成事项保留验收快照，请管理员重新打开后处理')
        if not case:
            first=config['stages'][0];_assigned(first,actor)
            if body.stage_key and body.stage_key!=first['key']:raise HTTPException(409,'请从申请阶段开始')
            case=_create(conn,task,config,actor)
        if case['status']=='completed':raise HTTPException(409,'审批单已经完成')
        stage=next((s for s in case['stages'] if s['key']==case['current_stage_key']),None)
        if not stage or body.stage_key!=stage['key']:raise HTTPException(409,'该环节不是当前待处理阶段，请刷新流程')
        review=stage['type']=='approval';_assigned(stage,actor,review)
        if review and body.action not in {'save','approve','reject'}:raise HTTPException(422,'审核阶段请使用通过或退回')
        if not review and body.action in {'approve','reject'}:raise HTTPException(422,'该阶段为办理环节')
        if stage['type']=='application' and body.action=='complete':raise HTTPException(422,'申请阶段请提交申请')
        if stage['type']=='handling' and body.action=='submit':raise HTTPException(422,'办理环节请提交办理结果')
        if body.action=='accept' and stage['type']!='handling':raise HTTPException(422,'仅办理环节需要确认接单')
        if body.action=='accept' and stage.get('started_at'):raise HTTPException(409,'该环节已经接单')
        if body.action=='reject' and not body.comment.strip():raise HTTPException(422,'退回必须说明修改意见')
        if body.external_event_at and body.external_event_at.replace(tzinfo=body.external_event_at.tzinfo or timezone.utc)>datetime.now(timezone.utc):raise HTTPException(422,'外部事件实际时间不能晚于现在')
        if stage['type']!='application' and body.application_answers and body.application_answers!=case['application_answers']:
            raise HTTPException(403,'申请内容已送审，不允许后续环节覆盖')
        if review and body.action!='reject' and stage.get('decisions') and body.answers!=stage['answers']:
            raise HTTPException(409,'已有审核意见的内容不能覆盖，请退回修订后重新送审')
        if body.action not in {'accept','reject'}:
            _validate_form(conn,case,stage,stage['fields'],body.answers,body.action in {'submit','complete','approve'})
            stage['answers']=copy.deepcopy(body.answers)
        if stage['type']=='application':
            application=body.application_answers if 'application_answers' in body.model_fields_set else case['application_answers']
            _validate_form(conn,case,stage,case['definition'].get('fields',[]),application,body.action=='submit')
            case['application_answers']=copy.deepcopy(application)
        if 'forecast_end' in body.model_fields_set:stage['forecast_end']=str(body.forecast_end) if body.forecast_end else None
        if 'delay_reason' in body.model_fields_set:stage['delay_reason']=body.delay_reason
        if 'next_action' in body.model_fields_set:stage['next_action']=body.next_action
        if stage.get('baseline_end') and stage.get('forecast_end') and stage['forecast_end']>stage['baseline_end'] and not stage['delay_reason'].strip():
            raise HTTPException(422,'预计完成晚于原计划时，请填写延误原因')
        if 'external_event_at' in body.model_fields_set:stage['external_event_at']=enc(body.external_event_at)
        if body.action in {'submit','complete','approve'}:
            blocked=_blocked(conn,task_id)
            if blocked:raise HTTPException(409,'前置事项尚未完成：'+'、'.join(x['title'] for x in blocked))
            _validate_all_assignees(conn,case['stages'])
        if body.action in {'accept','submit','complete','approve','reject'}:
            stage['started_at']=stage.get('started_at') or now()
        if body.action=='reject':
            target=stage.get('return_to') or next((s['key'] for s in reversed(case['stages'][:case['stages'].index(stage)]) if s['type'] in {'application','handling'}),None)
            if not target or body.return_to and body.return_to!=target:raise HTTPException(422,'请退回流程配置指定的补充或修订阶段')
            _event(conn,task_id,case,actor,'rejected',stage['key'],{'comment':body.comment,'return_to':target,'stage_snapshot':copy.deepcopy(stage)})
            start=next(i for i,s in enumerate(case['stages']) if s['key']==target)
            for index in range(start,len(case['stages'])):
                s=case['stages'][index];s.update(status='returned' if index==start else 'waiting',received_at=now() if index==start else None,started_at=None,completed_at=None,decisions=[])
            case['current_stage_key']=target;case['status']='in_progress'
        elif body.action=='approve':
            decided={d['user_id'] for d in stage['decisions']}
            if str(actor['id']) in decided:raise HTTPException(409,'你已审核本轮提交')
            if stage.get('review_mode')=='sequential' and next((x for x in stage['reviewer_ids'] if x not in decided),None)!=str(actor['id']):raise HTTPException(409,'请等待前一位审核人')
            stage['decisions'].append({'user_id':str(actor['id']),'decision':'approved','comment':body.comment,'at':now()})
            if stage.get('review_mode')=='any' or len(stage['decisions'])==len(stage['reviewer_ids']):
                stage.update(status='completed',completed_at=now());_advance(conn,task,case,actor)
            _event(conn,task_id,case,actor,'approved',stage['key'],{'comment':body.comment,'stage_snapshot':copy.deepcopy(stage)})
        elif body.action in {'submit','complete'}:
            if body.action=='submit':case['submitted_at']=case.get('submitted_at') or now()
            case['status']='in_progress';stage.update(status='completed',completed_at=now())
            _event(conn,task_id,case,actor,'submitted' if body.action=='submit' else 'handled',stage['key'],{'comment':body.comment,'stage_snapshot':copy.deepcopy(stage),'application_answers':case['application_answers']})
            _advance(conn,task,case,actor)
        else:
            if body.action=='accept':stage['status']='in_progress';case['status']='in_progress'
            _event(conn,task_id,case,actor,'accepted' if body.action=='accept' else 'draft_saved',stage['key'],{'stage_snapshot':copy.deepcopy(stage),'application_answers':case['application_answers']})
        _save_case(conn,case)
        if case['status']!='completed':
            conn.execute("UPDATE tasks SET status=%s,started_at=COALESCE(started_at,now()),completed_at=NULL,revision=revision+1,updated_at=now() WHERE id=%s",('pending_approval' if next(s for s in case['stages'] if s['key']==case['current_stage_key'])['type']=='approval' else 'in_progress',task_id))
        conn.commit()
    return {'ok':True,'status':case['status'],'current_stage_key':case['current_stage_key']}


def _directory():
    return Path(os.getenv('WORKFLOW_UPLOAD_DIR','/var/run/amazon-agent/workflow-uploads'))/'approval-cases'


@router.post('/tasks/{task_id}/attachments',status_code=201)
def upload(task_id:UUID,stage_key:str=Form(...),file:UploadFile=File(...),actor=Depends(require_user)):
    file_id=uuid4();path=_directory()/str(file_id)
    try:
        with connection() as conn:
            task=_task(conn,task_id,True);config=_config(conn,task);case=_case(conn,task_id)
            if task['status'] in {'completed','not_applicable'}:raise HTTPException(409,'已完成事项不可追加附件')
            if not case:
                _assigned(config['stages'][0],actor)
                if stage_key!=config['stages'][0]['key']:raise HTTPException(409,'请先发起审批单')
                case=_create(conn,task,config,actor)
            if case['current_stage_key']!=stage_key:raise HTTPException(409,'只能给当前环节上传附件')
            stage=next(s for s in case['stages'] if s['key']==stage_key)
            _assigned(stage,actor,stage['type']=='approval')
            filename=''.join(c for c in (file.filename or '附件').replace('\\','/').rsplit('/',1)[-1] if c.isprintable())[:200] or '附件'
            _directory().mkdir(parents=True,exist_ok=True);size=0;digest=hashlib.sha256()
            with path.open('xb') as out:
                os.chmod(path,0o600)
                while chunk:=file.file.read(65536):
                    size+=len(chunk)
                    if size>10485760:raise HTTPException(413,'附件不能超过10 MB')
                    digest.update(chunk);out.write(chunk)
            if not size:raise HTTPException(422,'不能上传空文件')
            conn.execute('INSERT INTO approval_files(id,task_id,case_id,stage_key,filename,size,sha256,uploaded_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(file_id,task_id,case['id'],stage_key,filename,size,digest.hexdigest(),actor['id']))
            _event(conn,task_id,case,actor,'attachment_uploaded',stage_key,{'file_id':str(file_id),'filename':filename})
            conn.commit()
    except Exception:
        path.unlink(missing_ok=True);raise
    finally:file.file.close()
    return {'kind':'file','id':str(file_id),'title':filename,'version':'v1'}


@router.get('/attachments/{file_id}')
def download(file_id:UUID,actor=Depends(require_user)):
    with connection() as conn:file=conn.execute('SELECT * FROM approval_files WHERE id=%s',(file_id,)).fetchone()
    if not file or not (_directory()/str(file_id)).is_file():raise HTTPException(404,'附件不存在')
    return FileResponse(_directory()/str(file_id),filename=file['filename'],media_type='application/octet-stream',headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})
