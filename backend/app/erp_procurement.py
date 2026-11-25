"""Independent approval of warehouse purchase requests and atomic issuance of their purchase orders."""
import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import Field

from .db import connection
from .erp_receiving import Command, Name, Note, OrderLine, _scope, conflict, create_purchase_order, invalid, validate_lines
from .workflow_auth import require_user

router = APIRouter(prefix='/api/erp-procurement', tags=['Procurement requests'])
Status = Literal['draft', 'pending', 'rejected', 'approved', 'issued']
Lines = Annotated[list[OrderLine], Field(min_length=1, max_length=200)]


class NewRequest(Command):
    scope_id: UUID
    supplier: Name
    reason: Note
    lines: Lines


class Update(Command):
    expected_version: int = Field(ge=1)
    supplier: Name | None = None
    reason: Note | None = None
    lines: Lines | None = None


class Action(Command):
    expected_version: int = Field(ge=1)
    action: Literal['submit','approve','reject','issue']
    note: str = Field(default='', max_length=2000)
    order_number: Name | None = None


HEAD = '''SELECT r.*, s.account_ref,s.site,s.warehouse,u.display_name AS owner_name,o.number AS order_number
    FROM erp_procurement_requests r JOIN erp_receiving_scopes s ON s.id=r.scope_id
    JOIN app_users u ON u.id=r.owner_id LEFT JOIN erp_receiving_orders o ON o.id=r.order_id'''
ACCESS = '''(%s OR EXISTS (SELECT 1 FROM erp_receiving_members m WHERE m.scope_id=s.id AND m.user_id=%s))'''


def head(conn, identifier, actor, lock=False):
    row = conn.execute(HEAD+' WHERE r.id=%s'+(' FOR UPDATE OF r' if lock else ''), (identifier,)).fetchone()
    if not row:
        raise HTTPException(404, '采购申请不存在或不可见')
    _scope(conn, row['scope_id'], actor, lock)
    return row


def read_lines(conn, identifier):
    return conn.execute('SELECT sku,qty FROM erp_procurement_lines WHERE request_id=%s ORDER BY position',
                        (identifier,)).fetchall()


def detail(conn, identifier, actor):
    row = head(conn,identifier,actor)
    row['lines'] = read_lines(conn,identifier)
    row['events'] = conn.execute('''SELECT e.version,e.action,e.actor_id,u.display_name AS actor_name,e.note,e.created_at,e.snapshot
        FROM erp_procurement_events e JOIN app_users u ON u.id=e.actor_id WHERE e.request_id=%s ORDER BY e.version''',
        (identifier,)).fetchall()
    return row


def set_lines(conn, identifier, lines):
    validate_lines(lines, 'sku')
    conn.execute('DELETE FROM erp_procurement_lines WHERE request_id=%s', (identifier,))
    for position, line in enumerate(lines):
        conn.execute('INSERT INTO erp_procurement_lines(request_id,position,sku,qty) VALUES(%s,%s,%s,%s)',
                     (identifier, position, line.sku, line.qty))


def event(conn, identifier, actor, action, note=''):
    row = head(conn,identifier,actor)
    snapshot={key:row[key] for key in ('number','supplier','reason','status','order_number')}
    snapshot.update(lines=read_lines(conn,identifier),order_id=str(row['order_id']) if row['order_id'] else None)
    conn.execute('''INSERT INTO erp_procurement_events(request_id,version,action,actor_id,note,snapshot)
                    VALUES(%s,%s,%s,%s,%s,%s)''',(identifier,row['version'],action,actor['id'],note,Jsonb(snapshot)))


def mutate(actor, body, operation, target, apply):
    fingerprint=hashlib.sha256(json.dumps([operation,str(target),body.model_dump(mode='json',exclude_unset=True)],
                                           sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with connection() as conn:
        key='procurement:'+str(actor['id'])+':'+str(body.request_id)
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
        prior=conn.execute('SELECT digest,request_id FROM erp_procurement_retries WHERE actor_id=%s AND request_key=%s',
                           (actor['id'],body.request_id)).fetchone()
        if prior:
            if prior['digest']!=fingerprint:
                conflict('请求编号已用于不同的采购申请操作')
            identifier=prior['request_id']
            head(conn,identifier,actor,True)
        else:
            identifier=apply(conn)
            conn.execute('INSERT INTO erp_procurement_retries(actor_id,request_key,digest,request_id) VALUES(%s,%s,%s,%s)',
                         (actor['id'],body.request_id,fingerprint,identifier))
        return detail(conn,identifier,actor)


@router.get('/requests')
def requests(scope_id: UUID | None=None, status: Status | None=None, limit: int=Query(100,ge=1,le=500),
             offset: int=Query(0,ge=0), actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if scope_id:
            _scope(conn,scope_id,actor)
        rows=conn.execute(HEAD+' WHERE '+ACCESS+''' AND (%s::uuid IS NULL OR r.scope_id=%s)
            AND (%s::text IS NULL OR r.status=%s) ORDER BY r.updated_at DESC,r.id LIMIT %s OFFSET %s''',
            (actor['is_admin'],actor['id'],scope_id,scope_id,status,status,limit+1,offset)).fetchall()
        return {'items':rows[:limit], 'has_more':len(rows)>limit, 'next_offset':offset+limit if len(rows)>limit else None}


@router.get('/requests/{identifier}')
def get_request(identifier: UUID, actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return detail(conn,identifier,actor)


@router.post('/requests',status_code=201)
def new_request(body: NewRequest,actor=Depends(require_user)):
    def apply(conn):
        _scope(conn,body.scope_id,actor,True)
        row=conn.execute('''INSERT INTO erp_procurement_requests(scope_id,owner_id,supplier,reason) VALUES(%s,%s,%s,%s)
                           RETURNING id''',(body.scope_id,actor['id'],body.supplier,body.reason)).fetchone()
        set_lines(conn,row['id'],body.lines)
        event(conn,row['id'],actor,'create')
        return row['id']
    return mutate(actor,body,'create',None,apply)


def require_owner(row,actor):
    if row['owner_id']!=actor['id']:
        raise HTTPException(403,'此操作只能由申请经办人办理')


def require_current(row,version):
    if row['version']!=version:
        conflict('申请已更新，请读取最新版本再操作')


@router.patch('/requests/{identifier}')
def edit_request(identifier: UUID, body: Update, actor=Depends(require_user)):
    def apply(conn):
        row=head(conn,identifier,actor,True)
        require_owner(row,actor)
        require_current(row,body.expected_version)
        if row['status'] not in ('draft','rejected'):
            conflict('仅草稿或已驳回的申请可修改')
        fields=body.model_fields_set & {'supplier','reason','lines'}
        if not fields or any(getattr(body,key) is None for key in fields):
            invalid('请选择要更新的供应商、原因或完整采购行')
        if body.lines is not None:
            set_lines(conn,identifier,body.lines)
        conn.execute('''UPDATE erp_procurement_requests SET supplier=%s,reason=%s,version=version+1,updated_at=clock_timestamp()
                        WHERE id=%s''',(body.supplier or row['supplier'],body.reason or row['reason'],identifier))
        event(conn,identifier,actor,'edit')
        return identifier
    return mutate(actor,body,'edit',identifier,apply)


@router.post('/requests/{identifier}/actions')
def act(identifier: UUID,body: Action,actor=Depends(require_user)):
    def apply(conn):
        row=head(conn,identifier,actor,True)
        action=body.action
        if action!='issue' and body.order_number is not None:
            invalid('只有签发动作可以指定采购单号')
        if action=='submit':
            require_owner(row,actor)
        elif not actor['is_admin']:
            raise HTTPException(403,'采购审批与签发需工作空间管理员权限')
        if action in ('approve','reject') and row['owner_id']==actor['id']:
            raise HTTPException(403,'申请经办人不可审批或驳回自己的申请')
        if action=='issue' and row['status']=='issued':
            if body.order_number and row['order_number']!=body.order_number:
                conflict('申请已签发且采购单号不同')
            return identifier
        require_current(row,body.expected_version)
        valid={
            'submit': (('draft','rejected'),'pending'),
            'approve': (('pending',),'approved'),
            'reject': (('pending',),'rejected'),
            'issue': (('approved',),'issued'),
        }
        allowed,status=valid[action]
        if row['status'] not in allowed:
            conflict('当前采购环节不支持此操作')
        note=body.note.strip()
        if action=='reject' and not note:
            invalid('请填写驳回理由，便于经办人修改后重提')
        order_id=None
        if action=='issue':
            lines=[OrderLine(**line) for line in read_lines(conn,identifier)]
            number=body.order_number or 'PO-'+row['number']
            order_id=create_purchase_order(conn,row['scope_id'],number,row['supplier'],lines,actor['id'])
        conn.execute('''UPDATE erp_procurement_requests SET status=%s,order_id=%s,version=version+1,updated_at=clock_timestamp()
                        WHERE id=%s''',(status,order_id,identifier))
        event(conn,identifier,actor,action,note)
        return identifier
    return mutate(actor,body,'action',identifier,apply)
