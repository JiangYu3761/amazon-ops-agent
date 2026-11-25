"""Local FBM order, package and buyer-cancellation handling against warehouse evidence."""
import hashlib
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .db import connection
from .erp_receiving import _scope
from .workflow_auth import require_user

router = APIRouter(prefix='/api/erp-fulfillment', tags=['Seller fulfilled orders'])
Ref = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=140)]
Qty = Annotated[int, Field(strict=True, gt=0, le=1_000_000)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Command(Input):
    request_id: UUID


class OrderLine(Input):
    line_ref: Ref
    sku: Ref
    qty: Qty


class CreateOrder(Command):
    scope_id: UUID
    number: Ref
    source_ref: Ref
    origin: Literal['platform','manual']
    paid: bool
    lines: list[OrderLine] = Field(min_length=1, max_length=300)

    @model_validator(mode='after')
    def distinct_lines(self):
        if len({line.line_ref for line in self.lines}) != len(self.lines):
            raise ValueError('订单行号重复')
        return self


class Allocation(Input):
    line_id: UUID
    qty: Qty


class CreatePackage(Command):
    package_ref: Ref
    lines: list[Allocation] = Field(min_length=1, max_length=300)

    @model_validator(mode='after')
    def distinct_lines(self):
        if len({line.line_id for line in self.lines}) != len(self.lines):
            raise ValueError('同一订单行请合并填写数量')
        return self


class Evidence(Command):
    evidence: Ref


class PackageAction(Evidence):
    action: Literal['release','dispatch','buyer_cancel','withdraw_cancel','confirm_cancel','intercept_ok','intercept_failed','retry_intercept','delivered_after_cancel']
    expected_version: int = Field(ge=1)


# An outbound warehouse confirmation can arrive after a stop request.
TRANSITIONS = {
    'review': {'release':'released','buyer_cancel':'hold'},
    'hold': {'withdraw_cancel':'review','confirm_cancel':'cancelled'},
    'released': {'dispatch':'shipped','buyer_cancel':'intercepting'},
    'intercepting': {'intercept_ok':'cancelled','intercept_failed':'stop_failed','dispatch':'after_dispatch'},
    'stop_failed': {'retry_intercept':'intercepting','dispatch':'after_dispatch','withdraw_cancel':'released'},
    'shipped': {'buyer_cancel':'after_dispatch'},
    'after_dispatch': {'intercept_ok':'cancelled','delivered_after_cancel':'closed_shipped'},
}


def reject(text):
    raise HTTPException(409, text)


def order_head(conn, order_id, actor, lock=False):
    row = conn.execute('SELECT * FROM erp_fbm_orders WHERE id=%s'+(' FOR UPDATE' if lock else ''),(order_id,)).fetchone()
    if not row:
        raise HTTPException(404, '订单不可见')
    scope = _scope(conn,row['scope_id'],actor,lock=lock)
    return {**row,'warehouse':scope['warehouse'],'account_ref':scope['account_ref'],'site':scope['site']}


def detail(conn, order_id, actor):
    order = order_head(conn,order_id,actor)
    order['lines'] = conn.execute('''SELECT l.*, l.qty-COALESCE((SELECT sum(a.qty) FROM erp_fbm_package_lines a
        WHERE a.line_id=l.id),0) AS remaining FROM erp_fbm_lines l WHERE order_id=%s ORDER BY line_ref''',(order_id,)).fetchall()
    order['packages'] = conn.execute('SELECT * FROM erp_fbm_packages WHERE order_id=%s ORDER BY created_at,id',(order_id,)).fetchall()
    allocations = conn.execute('''SELECT a.*,l.line_ref,l.sku FROM erp_fbm_package_lines a
        JOIN erp_fbm_lines l ON l.id=a.line_id WHERE a.order_id=%s ORDER BY l.line_ref''',(order_id,)).fetchall()
    by_package = {}
    for line in allocations:
        by_package.setdefault(line['package_id'],[]).append(line)
    for package in order['packages']:
        package['lines'] = by_package.get(package['id'],[])
        package['actions'] = [a for a in TRANSITIONS.get(package['status'],{}) if a!='release' or order['paid']]
    order['events'] = conn.execute('''SELECT e.*,u.display_name actor_name,p.package_ref FROM erp_fbm_events e
        JOIN app_users u ON u.id=e.actor_id LEFT JOIN erp_fbm_packages p ON p.id=e.package_id
        WHERE e.order_id=%s ORDER BY e.id DESC''',(order_id,)).fetchall()
    return order


def log(conn, actor, order_id, event, evidence, package_id=None):
    conn.execute('INSERT INTO erp_fbm_events(order_id,package_id,event,evidence,actor_id) VALUES(%s,%s,%s,%s,%s)',
                 (order_id,package_id,event,evidence,actor['id']))


def execute(actor, body, operation, target, change):
    digest = hashlib.sha256(f'{operation}:{target}:{body.model_dump_json()}'.encode()).hexdigest()
    with connection() as conn:
        key = f"fbm:{actor['id']}:{body.request_id}"
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
        prior = conn.execute('SELECT * FROM erp_fbm_requests WHERE actor_id=%s AND request_id=%s',
                             (actor['id'],body.request_id)).fetchone()
        if prior:
            if prior['digest'] != digest:
                reject('该请求标识已用于不同操作')
            return detail(conn,prior['order_id'],actor)
        order_id = change(conn)
        conn.execute('INSERT INTO erp_fbm_requests(actor_id,request_id,digest,order_id) VALUES(%s,%s,%s,%s)',
                     (actor['id'],body.request_id,digest,order_id))
        return detail(conn,order_id,actor)


@router.get('/orders')
def list_orders(scope_id:UUID, search:str=Query('',max_length=140), offset:int=Query(0,ge=0), actor=Depends(require_user)):
    with connection() as conn:
        _scope(conn,scope_id,actor,lock=True)
        rows=conn.execute('''SELECT o.*, (SELECT count(*) FROM erp_fbm_packages p WHERE p.order_id=o.id) AS packages
            FROM erp_fbm_orders o WHERE o.scope_id=%s AND (strpos(lower(o.number),lower(%s))>0 OR EXISTS
            (SELECT 1 FROM erp_fbm_packages p WHERE p.order_id=o.id AND strpos(lower(p.package_ref),lower(%s))>0))
            ORDER BY o.created_at DESC,o.id LIMIT 31 OFFSET %s''',(scope_id,search,search,offset)).fetchall()
        return {'items':rows[:30],'has_more':len(rows)>30}


@router.get('/orders/{order_id}')
def get_order(order_id:UUID, actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return detail(conn,order_id,actor)


@router.post('/orders',status_code=201)
def create(body:CreateOrder, actor=Depends(require_user)):
    def change(conn):
        _scope(conn,body.scope_id,actor,lock=True)
        row=conn.execute('''INSERT INTO erp_fbm_orders(scope_id,number,origin,paid,source_ref,created_by)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(scope_id,number) DO NOTHING RETURNING id''',
            (body.scope_id,body.number,body.origin,body.paid,body.source_ref,actor['id'])).fetchone()
        if not row:
            reject('此仓库已存在相同订单编号')
        for line in body.lines:
            conn.execute('INSERT INTO erp_fbm_lines(order_id,line_ref,sku,qty) VALUES(%s,%s,%s,%s)',
                         (row['id'],line.line_ref,line.sku,line.qty))
        log(conn,actor,row['id'],'create',body.source_ref)
        return row['id']
    return execute(actor,body,'create',body.scope_id,change)


@router.post('/orders/{order_id}/payment')
def payment(order_id:UUID, body:Evidence, actor=Depends(require_user)):
    def change(conn):
        order=order_head(conn,order_id,actor,lock=True)
        if order['paid']:
            reject('已确认收款')
        conn.execute('UPDATE erp_fbm_orders SET paid=true WHERE id=%s',(order_id,))
        log(conn,actor,order_id,'payment',body.evidence)
        return order_id
    return execute(actor,body,'payment',order_id,change)


@router.post('/orders/{order_id}/packages',status_code=201)
def allocate(order_id:UUID, body:CreatePackage, actor=Depends(require_user)):
    def change(conn):
        order_head(conn,order_id,actor,lock=True)
        available={line['id']:line['remaining'] for line in detail(conn,order_id,actor)['lines']}
        for line in body.lines:
            if line.line_id not in available or line.qty>available[line.line_id]:
                reject('订单行不在本单或分配量超过剩余数量')
        row=conn.execute('''INSERT INTO erp_fbm_packages(order_id,package_ref) VALUES(%s,%s)
            ON CONFLICT(order_id,package_ref) DO NOTHING RETURNING id''',(order_id,body.package_ref)).fetchone()
        if not row:
            reject('此单已使用相同包裹编号')
        for line in body.lines:
            conn.execute('INSERT INTO erp_fbm_package_lines(order_id,package_id,line_id,qty) VALUES(%s,%s,%s,%s)',
                         (order_id,row['id'],line.line_id,line.qty))
        log(conn,actor,order_id,'allocate',body.package_ref,row['id'])
        return order_id
    return execute(actor,body,'allocate',order_id,change)


@router.post('/packages/{package_id}/events')
def record(package_id:UUID, body:PackageAction, actor=Depends(require_user)):
    def change(conn):
        ref=conn.execute('SELECT order_id FROM erp_fbm_packages WHERE id=%s',(package_id,)).fetchone()
        if not ref:
            raise HTTPException(404,'包裹不可见')
        order=order_head(conn,ref['order_id'],actor,lock=True)
        package=conn.execute('SELECT * FROM erp_fbm_packages WHERE id=%s',(package_id,)).fetchone()
        if package['version']!=body.expected_version:
            reject('包裹已更新，请刷新后核对')
        status=TRANSITIONS.get(package['status'],{}).get(body.action)
        if not status:
            reject('当前包裹状态不支持此操作')
        if body.action=='release' and not order['paid']:
            reject('未确认收款，不能审核放行')
        conn.execute('UPDATE erp_fbm_packages SET status=%s,version=version+1 WHERE id=%s',(status,package_id))
        log(conn,actor,order['id'],body.action,body.evidence,package_id)
        return order['id']
    return execute(actor,body,'event',package_id,change)
