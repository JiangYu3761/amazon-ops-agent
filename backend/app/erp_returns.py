"""Local return-package receipt and disposition journals bound to an existing order line."""
from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, model_validator

from .db import connection
from .erp_refunds import Body, Reference, line, page
from .erp_receiving import _scope
from .stock_ledger import check_postings
from .workflow_auth import require_user

router = APIRouter(prefix='/api/erp-returns', tags=['Local return reconciliation'])
CHANNEL_SOURCE = {'local':'local_record','third_party':'warehouse_report','fba':'fba_report'}
MAX_UNITS = 10_000_000


class Package(Body):
    package_ref: Reference
    expected_units: int = Field(gt=0, le=MAX_UNITS, strict=True)
    channel: Literal['local','third_party','fba']
    ordered_units: int = Field(gt=0, le=MAX_UNITS, strict=True)
    order_evidence: Reference


class Record(Body):
    kind: Literal['receive','dispose']
    event_ref: Reference
    evidence_ref: Reference
    evidence_type: Literal['local_record','warehouse_report','fba_report']
    occurred_on: date
    received: int = Field(default=0, ge=0, le=MAX_UNITS, strict=True)
    good: int = Field(default=0, ge=0, le=MAX_UNITS, strict=True)
    defective: int = Field(default=0, ge=0, le=MAX_UNITS, strict=True)
    destroyed: int = Field(default=0, ge=0, le=MAX_UNITS, strict=True)

    @model_validator(mode='after')
    def quantities(self):
        classified = self.good + self.defective + self.destroyed
        if self.kind == 'receive' and (self.received == 0 or classified):
            raise ValueError('实收流水只登记收到的数量')
        if self.kind == 'dispose' and (self.received or not classified):
            raise ValueError('处置流水请登记良品、次品或销毁数量')
        return self


_TOTALS = '''SELECT COALESCE(sum(received),0) received,COALESCE(sum(good),0) good,
            COALESCE(sum(defective),0) defective,COALESCE(sum(destroyed),0) destroyed
            FROM erp_return_events WHERE package_id=p.id'''


def package_summary(conn, scope_id, package_id):
    row = conn.execute('SELECT p.*,t.* FROM erp_return_packages p CROSS JOIN LATERAL (' + _TOTALS +
        ') t WHERE p.line_id=%s AND p.id=%s', (scope_id,package_id)).fetchone()
    if not row:
        raise HTTPException(404,'此订单行没有该退货包裹')
    return row


def enrich(row):
    result = dict(row)
    result['outstanding'] = row['expected_units'] - row['received']
    result['unclassified'] = row['received'] - row['good'] - row['defective'] - row['destroyed']
    return result


def get_detail(conn, row, actor, offset=0, limit=25):
    warehouses = []
    if row['channel']=='local':
        warehouses = conn.execute('''SELECT s.id,s.warehouse,s.account_ref,s.site FROM erp_receiving_scopes s
            JOIN erp_refund_lines l ON l.account_ref=s.account_ref AND l.site=s.site
            LEFT JOIN erp_return_stock_warehouses bound ON bound.package_id=%s
            WHERE l.id=%s AND (bound.scope_id IS NULL OR bound.scope_id=s.id)
            AND (%s OR EXISTS (SELECT 1 FROM erp_receiving_members m WHERE m.scope_id=s.id AND m.user_id=%s))
            ORDER BY s.warehouse,s.id''',(row['id'],row['line_id'],actor['is_admin'],actor['id'])).fetchall()
    allowed = {s['id']:s['warehouse'] for s in warehouses}
    events = conn.execute('''SELECT e.id,e.kind,e.event_ref,e.evidence_ref,e.evidence_type,e.occurred_on,
            e.received,e.good,e.defective,e.destroyed,e.recorded_at,u.display_name recorded_by_name,
            sp.scope_id AS stock_scope_id,sp.posted_at AS stock_posted_at
            FROM erp_return_events e JOIN app_users u ON u.id=e.recorded_by
            LEFT JOIN erp_return_stock_postings sp ON sp.event_id=e.id WHERE e.package_id=%s
            ORDER BY e.recorded_at DESC,e.id DESC LIMIT %s OFFSET %s''',(row['id'],limit+1,offset)).fetchall()
    for record in events:
        scope_id=record.pop('stock_scope_id')
        record['stock_warehouse']=allowed.get(scope_id)
        record['stock_posted']=record['stock_posted_at'] is not None
    return {**enrich(row),'events':page(events,limit,offset),'can_record':bool(actor['is_admin']),
            'stock_scopes':warehouses}


@router.get('/lines/{line_id}/packages')
def list_packages(line_id: UUID, limit: int=Query(25,ge=1,le=100), offset: int=Query(0,ge=0), actor=Depends(require_user)):
    with connection() as conn:
        line(conn,line_id,actor,lock=True)
        order = conn.execute('SELECT ordered_units,order_evidence FROM erp_return_limits WHERE line_id=%s',(line_id,)).fetchone()
        reserved = conn.execute('SELECT COALESCE(sum(expected_units),0) n FROM erp_return_packages WHERE line_id=%s',
                                (line_id,)).fetchone()['n']
        rows = conn.execute('SELECT p.*,t.* FROM erp_return_packages p CROSS JOIN LATERAL ('+_TOTALS+
            ') t WHERE p.line_id=%s ORDER BY p.created_at DESC,p.id DESC LIMIT %s OFFSET %s',(line_id,limit+1,offset)).fetchall()
        return {'order_quantity':order,'expected_total':reserved,**page([enrich(r) for r in rows],limit,offset)}


@router.post('/lines/{line_id}/packages',status_code=201)
def register_package(line_id: UUID, body: Package, actor=Depends(require_user)):
    with connection() as conn:
        line(conn,line_id,actor,lock=True)
        conn.execute('''INSERT INTO erp_return_limits(line_id,ordered_units,order_evidence,created_by)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(line_id,body.ordered_units,body.order_evidence,actor['id']))
        order = conn.execute('SELECT ordered_units,order_evidence FROM erp_return_limits WHERE line_id=%s',(line_id,)).fetchone()
        if order['ordered_units'] != body.ordered_units or order['order_evidence'] != body.order_evidence:
            raise HTTPException(409,'已登记的原订单件数与凭证须保持一致')
        existing = conn.execute('SELECT id,expected_units,channel FROM erp_return_packages WHERE line_id=%s AND package_ref=%s',
                                (line_id,body.package_ref)).fetchone()
        if existing:
            if existing['expected_units'] != body.expected_units or existing['channel'] != body.channel:
                raise HTTPException(409,'同一订单行的退货包裹引用已用于不同记录')
            return get_detail(conn,package_summary(conn,line_id,existing['id']),actor)
        reserved = conn.execute('SELECT COALESCE(sum(expected_units),0) n FROM erp_return_packages WHERE line_id=%s',
                                (line_id,)).fetchone()['n']
        if reserved + body.expected_units > order['ordered_units']:
            raise HTTPException(422,'包裹登记的累计退回件数超过原订单行件数')
        new = conn.execute('''INSERT INTO erp_return_packages(line_id,package_ref,expected_units,channel,created_by)
            VALUES(%s,%s,%s,%s,%s) RETURNING id''',(line_id,body.package_ref,body.expected_units,body.channel,actor['id'])).fetchone()
        return get_detail(conn,package_summary(conn,line_id,new['id']),actor)


@router.get('/lines/{line_id}/packages/{package_id}')
def read_package(line_id: UUID, package_id: UUID, limit: int=Query(25,ge=1,le=100), offset: int=Query(0,ge=0),
                 actor=Depends(require_user)):
    with connection() as conn:
        line(conn,line_id,actor,lock=True)
        return get_detail(conn,package_summary(conn,line_id,package_id),actor,offset,limit)


@router.post('/lines/{line_id}/packages/{package_id}/events',status_code=201)
def register_event(line_id: UUID, package_id: UUID, body: Record, actor=Depends(require_user)):
    if not actor['is_admin']:
        raise HTTPException(403,'实收与处置凭证须由流程管理员核对后登记')
    with connection() as conn:
        line(conn,line_id,actor,lock=True)
        package = enrich(package_summary(conn,line_id,package_id))
        if body.evidence_type != CHANNEL_SOURCE[package['channel']]:
            raise HTTPException(422,'凭证类型须与本地仓、三方仓或 FBA 包裹来源一致')
        prior = conn.execute('SELECT * FROM erp_return_events WHERE line_id=%s AND kind=%s AND event_ref=%s',
                             (line_id,body.kind,body.event_ref)).fetchone()
        if prior:
            if prior['package_id'] != package_id or any(prior[k] != v for k,v in body.model_dump().items()):
                raise HTTPException(409,'此订单行的回执引用已用于不同记录')
            return get_detail(conn,package,actor)
        if body.received > package['outstanding']:
            raise HTTPException(422,'本次实收超过包裹尚未收回的数量')
        if body.good + body.defective + body.destroyed > package['unclassified']:
            raise HTTPException(422,'处置件数超过已收到且尚未分类的数量')
        conn.execute('''INSERT INTO erp_return_events(package_id,line_id,kind,event_ref,evidence_ref,evidence_type,
            occurred_on,received,good,defective,destroyed,recorded_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (package_id,line_id,body.kind,body.event_ref,body.evidence_ref,body.evidence_type,body.occurred_on,body.received,
             body.good,body.defective,body.destroyed,actor['id']))
        if body.kind == 'dispose':
            shortage = conn.execute('''SELECT 1 FROM (
                SELECT occurred_on, sum(sum(received - good - defective - destroyed))
                       OVER (ORDER BY occurred_on) AS available
                FROM erp_return_events WHERE package_id=%s GROUP BY occurred_on
            ) days WHERE occurred_on >= %s AND available < 0 LIMIT 1''',
                (package_id,body.occurred_on)).fetchone()
            if shortage:
                raise HTTPException(422,'按凭证发生日核对，处置件数超过当时已实收且尚未分类的数量')
        return get_detail(conn,package_summary(conn,line_id,package_id),actor)


class StockPosting(Body):
    event_id: UUID
    scope_id: UUID
    request_id: UUID


@router.post('/packages/{package_id}/post-stock',status_code=201)
def post_stock(package_id: UUID, body: StockPosting, actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                     ('return-stock:'+str(actor['id'])+':'+str(body.request_id),))
        lookup=conn.execute('SELECT line_id FROM erp_return_packages WHERE id=%s',(package_id,)).fetchone()
        if not lookup:
            raise HTTPException(404,'包裹不存在或不可见')
        source=line(conn,lookup['line_id'],actor,lock=True)
        package=package_summary(conn,source['id'],package_id)
        destination=_scope(conn,body.scope_id,actor,lock=True)
        if destination['account_ref']!=source['account_ref'] or destination['site']!=source['site']:
            raise HTTPException(422,'请选择原订单同店铺、同站点的实际收货仓库')
        if package['channel']!='local':
            raise HTTPException(422,'三方仓和 FBA 报告只能作为外部处置凭证')
        event=conn.execute('SELECT * FROM erp_return_events WHERE id=%s AND package_id=%s',
                           (body.event_id,package_id)).fetchone()
        if not event or event['kind']!='dispose' or event['evidence_type']!='local_record':
            raise HTTPException(422,'请选择该包裹的本地仓处置凭证')
        if event['good']+event['defective']==0:
            raise HTTPException(422,'全部销毁的处置没有可入库件数')
        prior=conn.execute('SELECT event_id FROM erp_return_stock_requests WHERE actor_id=%s AND request_id=%s',
                           (actor['id'],body.request_id)).fetchone()
        if prior and prior['event_id']!=body.event_id:
            raise HTTPException(409,'该请求编号已用于其他处置流水')
        # The order-line lock above serializes package binding, recording and replay.
        bound=conn.execute('SELECT scope_id FROM erp_return_stock_warehouses WHERE package_id=%s',
                           (package_id,)).fetchone()
        if bound and bound['scope_id']!=body.scope_id:
            raise HTTPException(409,'包裹已登记到其他仓库，不能跨仓过账')
        if not bound:
            conn.execute('INSERT INTO erp_return_stock_warehouses(package_id,scope_id) VALUES(%s,%s)',
                         (package_id,body.scope_id))
        existing=conn.execute('SELECT 1 FROM erp_return_stock_postings WHERE event_id=%s',(body.event_id,)).fetchone()
        if not existing:
            check_postings(conn,{(body.scope_id,source['sku']):(0,event['good'],event['defective'])})
            conn.execute("""INSERT INTO erp_return_stock_postings
                (event_id,package_id,line_id,scope_id,sku,good,defective,destroyed,actor_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (body.event_id,package_id,source['id'],body.scope_id,source['sku'],
                 event['good'],event['defective'],event['destroyed'],actor['id']))
        if not prior:
            conn.execute('INSERT INTO erp_return_stock_requests(actor_id,request_id,event_id) VALUES(%s,%s,%s)',
                         (actor['id'],body.request_id,body.event_id))
        return get_detail(conn,package,actor)
