"""Receiving and quality allocations for an explicitly granted local warehouse scope."""
import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .db import connection
from .erp_refunds import Amount, Currency, check_amount, money_text
from .stock_ledger import check_postings
from .workbench_scope import resolve_shop_scope
from .workflow_auth import require_admin, require_user

router = APIRouter(prefix='/api/erp-receiving', tags=['Local warehouse receiving'])
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Units = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
Positive = Annotated[int, Field(strict=True, ge=1, le=1_000_000_000)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Command(Input):
    request_id: UUID


class Scope(Command):
    account_ref: Name
    site: Name
    business_timezone: Name
    warehouse: Name
    member_ids: list[UUID] = Field(default_factory=list, max_length=500)


class OrderLine(Input):
    sku: Name
    qty: Positive
    contract_currency: Currency | None = None
    net_unit_price: Amount | None = None
    unit_tax: Annotated[Decimal, Field(ge=0, lt=10**14, max_digits=18, decimal_places=4)] | None = None

    @model_validator(mode='after')
    def complete_pricing(self):
        if self.contract_currency is None and self.net_unit_price is None and self.unit_tax is None:
            return self
        if self.contract_currency is None or self.net_unit_price is None:
            raise ValueError('计价采购行须同时填写合同币种和每件净价')
        check_amount(self.net_unit_price, self.contract_currency)
        check_amount(self.unit_tax or Decimal(0), self.contract_currency)
        return self

    def price_values(self):
        if self.contract_currency is None:
            return (None, None, None)
        return (self.contract_currency, self.net_unit_price, self.unit_tax or Decimal(0))


def format_contract(row):
    if row['contract_currency']:
        for key in ('net_unit_price', 'unit_tax'):
            row[key] = money_text(row[key], row['contract_currency'])
    return row


class Order(Command):
    scope_id: UUID
    number: Name
    supplier: Name
    lines: list[OrderLine] = Field(min_length=1, max_length=200)


class NoticeLine(Input):
    line_id: UUID
    qty: Positive


class Notice(Command):
    lines: list[NoticeLine] = Field(min_length=1, max_length=200)


class ReceivedLine(NoticeLine):
    good: Units | None = None
    bad: Units | None = None


class Receive(Command):
    scope_id: UUID | None = None
    mode: Literal['normal', 'quick']
    lines: list[ReceivedLine] = Field(min_length=1, max_length=200)


class QualityLine(Input):
    line_id: UUID
    good: Units
    bad: Units


class Quality(Command):
    mode: Literal['inspect', 'waive']
    lines: list[QualityLine] = Field(min_length=1, max_length=200)
    note: str = Field(default='', max_length=2000)


class Correction(Command):
    reason: Note


def conflict(message):
    raise HTTPException(409, message)


def invalid(message):
    raise HTTPException(422, message)


def validate_lines(lines, key):
    keys = [getattr(line, key) for line in lines]
    if len(set(keys)) != len(keys):
        invalid('同一表单不可重复录入相同的商品行')


def _scope(conn, scope_id, actor, lock=False):
    row = conn.execute('SELECT * FROM erp_receiving_scopes WHERE id=%s' +
                       (' FOR SHARE' if lock else ''), (scope_id,)).fetchone()
    if not row:
        raise HTTPException(404, '仓库范围不存在或不可见')
    members = conn.execute('SELECT user_id FROM erp_receiving_members WHERE scope_id=%s ORDER BY user_id',
                           (scope_id,)).fetchall()
    row['member_ids'] = [m['user_id'] for m in members]
    if not actor['is_admin'] and actor['id'] not in row['member_ids']:
        raise HTTPException(404, '仓库范围不存在或不可见')
    return row


_HEAD = '''SELECT o.*,s.warehouse,s.site,s.account_ref,s.business_timezone,
           pr.id AS procurement_request_id,pr.number AS procurement_request_number
           FROM erp_receiving_orders o JOIN erp_receiving_scopes s ON s.id=o.scope_id
           LEFT JOIN erp_procurement_requests pr ON pr.order_id=o.id'''


def _detail(conn, order_id, actor, lock=False):
    head = conn.execute(_HEAD+' WHERE o.id=%s'+(' FOR UPDATE OF o' if lock else ''), (order_id,)).fetchone()
    if not head:
        raise HTTPException(404, '采购单不存在或不可见')
    _scope(conn, head['scope_id'], actor, lock)
    lines = conn.execute('SELECT id,sku,qty,contract_currency,net_unit_price,unit_tax FROM erp_receiving_order_lines WHERE order_id=%s ORDER BY position',
                         (order_id,)).fetchall()
    for line in lines:
        format_contract(line)
        line.update(received=0, notified=0, unnotified=line['qty'])
    by_line = {line['id']: line for line in lines}
    notices = conn.execute('SELECT * FROM erp_receiving_notices WHERE order_id=%s ORDER BY created_at,number',
                           (order_id,)).fetchall()
    by_notice = {notice['id']: notice for notice in notices}
    for notice in notices:
        notice.update(lines=[], events=[])
    allocated = {}
    for line in conn.execute('''SELECT n.notice_id,n.line_id,n.qty,l.sku FROM erp_receiving_notice_lines n
                    JOIN erp_receiving_order_lines l ON l.id=n.line_id WHERE n.order_id=%s ORDER BY l.position''', (order_id,)):
        line.update(received=0, pending=0, good=0, bad=0)
        allocated[line['notice_id'], line['line_id']] = line
        notice = by_notice[line['notice_id']]
        notice['lines'].append(line)
        if notice['status'] == 'waiting':
            by_line[line['line_id']]['notified'] += line['qty']
    events = conn.execute('''SELECT e.*,r.id IS NOT NULL AS reversed FROM erp_receiving_events e
                 JOIN erp_receiving_notices n ON e.notice_id=n.id
                 LEFT JOIN erp_receiving_events r ON r.reverses=e.id WHERE n.order_id=%s ORDER BY e.seq''',
                          (order_id,)).fetchall()
    by_event = {event['id']: event for event in events}
    for event in events:
        event['lines'] = []
        by_notice[event['notice_id']]['events'].append(event)
    entries = conn.execute('''SELECT g.*,l.sku,e.action,e.created_at,e.actor_id FROM erp_receiving_ledger g
                 JOIN erp_receiving_events e ON e.id=g.event_id JOIN erp_receiving_order_lines l ON l.id=g.line_id
                 WHERE l.order_id=%s ORDER BY e.seq,l.position''', (order_id,)).fetchall()
    for entry in entries:
        allocated_line = allocated[entry['notice_id'], entry['line_id']]
        net = entry['pending_delta'] + entry['good_delta'] + entry['bad_delta']
        allocated_line['received'] += net
        by_line[entry['line_id']]['received'] += net
        for key in ('pending', 'good', 'bad'):
            allocated_line[key] += entry[key+'_delta']
        by_event[entry['event_id']]['lines'].append(entry)
    for line in lines:
        line['unnotified'] = line['qty'] - line['received'] - line['notified']
    return {**head, 'lines': lines, 'notices': notices, 'ledger': entries}


def _execute(actor, body, operation, target, change):
    # Commit the request receipt, document changes, and all quantity deltas together.
    payload = body.model_dump(mode='json')
    if isinstance(body, Order):
        # Preserve request digests issued before the optional contract fields existed.
        for line in payload['lines']:
            if line['contract_currency'] is None:
                for key in ('contract_currency','net_unit_price','unit_tax'):
                    line.pop(key)
    fingerprint = hashlib.sha256(json.dumps([operation, str(target), payload],
                                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (str(actor['id'])+':'+str(body.request_id),))
        row = conn.execute('SELECT * FROM erp_receiving_requests WHERE actor_id=%s AND request_id=%s',
                           (actor['id'], body.request_id)).fetchone()
        if row:
            if row['digest'] != fingerprint:
                conflict('此请求编号已用于其他内容')
            order_id, scope_id = row['order_id'], row['scope_id']
        else:
            order_id, scope_id = change(conn)
            conn.execute('''INSERT INTO erp_receiving_requests(actor_id,request_id,digest,order_id,scope_id)
                            VALUES(%s,%s,%s,%s,%s)''', (actor['id'], body.request_id, fingerprint, order_id, scope_id))
        return _detail(conn, order_id, actor, lock=True) if order_id else _scope(conn, scope_id, actor, lock=True)


@router.get('/scopes')
def scopes(actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        allowed = conn.execute('''SELECT id FROM erp_receiving_scopes s WHERE %s OR EXISTS
                             (SELECT 1 FROM erp_receiving_members m WHERE m.scope_id=s.id AND m.user_id=%s)
                             ORDER BY s.account_ref,s.site,s.warehouse''', (actor['is_admin'], actor['id'])).fetchall()
        return {'scopes': [_scope(conn, row['id'], actor) for row in allowed]}


@router.post('/scopes', status_code=201)
def configure_scope(body: Scope, actor=Depends(require_admin)):
    def change(conn):
        account, site, zone = resolve_shop_scope(conn, body.account_ref, body.site, body.business_timezone, register_by=actor['id'])
        members = set(body.member_ids)
        if members:
            valid = conn.execute('''SELECT count(*) AS n FROM workflow_accounts a JOIN app_users u ON a.user_id=u.id
                                    WHERE u.is_active AND u.id=ANY(%s)''', (list(members),)).fetchone()['n']
            if valid != len(members):
                invalid('仓库成员必须是有效的登录账户')
        scope_id = conn.execute('''INSERT INTO erp_receiving_scopes(account_ref,site,business_timezone,warehouse)
                                   VALUES(%s,%s,%s,%s) ON CONFLICT(account_ref,site,warehouse)
                                   DO UPDATE SET business_timezone=EXCLUDED.business_timezone RETURNING id''',
                                (account, site, zone, body.warehouse)).fetchone()['id']
        conn.execute('DELETE FROM erp_receiving_members WHERE scope_id=%s', (scope_id,))
        for member in members:
            conn.execute('INSERT INTO erp_receiving_members(scope_id,user_id) VALUES(%s,%s)', (scope_id, member))
        return None, scope_id
    return _execute(actor, body, 'scope', None, change)


@router.get('/orders')
def orders(scope_id: UUID | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
           actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if scope_id:
            _scope(conn, scope_id, actor)
        result = conn.execute(_HEAD+''' WHERE (%s OR EXISTS(SELECT 1 FROM erp_receiving_members m
                         WHERE m.scope_id=o.scope_id AND m.user_id=%s)) AND (%s::uuid IS NULL OR o.scope_id=%s)
                         ORDER BY o.created_at DESC,o.id LIMIT %s OFFSET %s''',
                              (actor['is_admin'], actor['id'], scope_id, scope_id, limit+1, offset)).fetchall()
        return {'items': result[:limit], 'has_more': len(result)>limit, 'next_offset': offset+limit if len(result)>limit else None}


@router.get('/orders/{order_id}')
def order_detail(order_id: UUID, actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return _detail(conn, order_id, actor)


def create_purchase_order(conn, scope_id, number, supplier, lines, creator_id):
    validate_lines(lines, 'sku')
    order = conn.execute('''INSERT INTO erp_receiving_orders(scope_id,number,supplier,created_by) VALUES(%s,%s,%s,%s)
                            ON CONFLICT(scope_id,number) DO NOTHING RETURNING id''',
                         (scope_id, number, supplier, creator_id)).fetchone()
    if not order:
        conflict('此仓库范围已有该采购单号')
    for position,line in enumerate(lines):
        conn.execute('''INSERT INTO erp_receiving_order_lines(order_id,position,sku,qty,contract_currency,net_unit_price,unit_tax)
                        VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                     (order['id'], position, line.sku, line.qty, *line.price_values()))
    return order['id']


@router.post('/orders', status_code=201)
def add_order(body: Order, actor=Depends(require_admin)):
    def change(conn):
        _scope(conn, body.scope_id, actor, lock=True)
        return create_purchase_order(conn, body.scope_id, body.number, body.supplier, body.lines, actor['id']), None
    return _execute(actor, body, 'order', None, change)


@router.post('/orders/{order_id}/notices', status_code=201)
def notify(order_id: UUID, body: Notice, actor=Depends(require_user)):
    def change(conn):
        detail = _detail(conn, order_id, actor, lock=True)
        validate_lines(body.lines,'line_id')
        allowed = {line['id']:line['unnotified'] for line in detail['lines']}
        for line in body.lines:
            if line.line_id not in allowed:
                invalid('请选择本采购单的明细行')
            if line.qty > allowed[line.line_id]:
                conflict('通知数量超过该采购行当前未通知量')
        notice_id = conn.execute('INSERT INTO erp_receiving_notices(order_id,created_by) VALUES(%s,%s) RETURNING id',
                                 (order_id,actor['id'])).fetchone()['id']
        for line in body.lines:
            conn.execute('INSERT INTO erp_receiving_notice_lines(order_id,notice_id,line_id,qty) VALUES(%s,%s,%s,%s)',
                         (order_id,notice_id,line.line_id,line.qty))
        return order_id, None
    return _execute(actor, body, 'notify', order_id, change)


def _notice(conn, notice_id, actor):
    row = conn.execute('SELECT order_id FROM erp_receiving_notices WHERE id=%s', (notice_id,)).fetchone()
    if not row:
        raise HTTPException(404,'收货通知不存在或不可见')
    detail = _detail(conn, row['order_id'], actor, lock=True)
    return detail, next(n for n in detail['notices'] if n['id']==notice_id)


def _event(conn, actor, detail, notice, action, postings, note='', reverses=None):
    notice_id = notice['id']
    skus = {line['line_id']:line['sku'] for line in notice['lines']}
    check_postings(conn,{(detail['scope_id'],skus[line_id]):(pending,good,bad)
                         for line_id,pending,good,bad in postings})
    event = conn.execute('''INSERT INTO erp_receiving_events(notice_id,actor_id,action,note,reverses)
                            VALUES(%s,%s,%s,%s,%s) RETURNING id''',(notice_id,actor['id'],action,note,reverses)).fetchone()
    for line_id, pending, good, bad in postings:
        conn.execute('''INSERT INTO erp_receiving_ledger(event_id,notice_id,line_id,pending_delta,good_delta,bad_delta)
                        VALUES(%s,%s,%s,%s,%s,%s)''',(event['id'],notice_id,line_id,pending,good,bad))


@router.post('/notices/{notice_id}/receive')
def receive(notice_id: UUID, body: Receive, actor=Depends(require_user)):
    def change(conn):
        detail, notice = _notice(conn, notice_id, actor)
        if body.scope_id is not None and body.scope_id != detail['scope_id']:
            conflict('通知所属仓库与当前收货仓库不一致，请重新扫码')
        if notice['status'] != 'waiting':
            conflict('仅待收货通知可收货，一张通知只能确认一次')
        validate_lines(body.lines,'line_id')
        capacity = {line['line_id']:line['qty'] for line in notice['lines']}
        postings = []
        for line in body.lines:
            if line.line_id not in capacity:
                invalid('请选择本通知中的采购行')
            if line.qty > capacity[line.line_id]:
                conflict('本次实收不能超过通知量')
            if body.mode=='normal':
                if line.good is not None or line.bad is not None:
                    invalid('普通收货先计入待检，良次品请在质检时登记')
                postings.append((line.line_id,line.qty,0,0))
            else:
                if line.good is None or line.bad is None or line.qty!=line.good+line.bad:
                    invalid('快捷入库须填写良品和次品，合计等于实收')
                postings.append((line.line_id,0,line.good,line.bad))
        _event(conn,actor,detail,notice,'receive_'+body.mode,postings)
        conn.execute("UPDATE erp_receiving_notices SET status='received' WHERE id=%s",(notice_id,))
        return detail['id'], None
    return _execute(actor, body, 'receive', notice_id, change)


@router.post('/notices/{notice_id}/quality')
def quality(notice_id: UUID, body: Quality, actor=Depends(require_user)):
    def change(conn):
        detail, notice = _notice(conn, notice_id, actor)
        if notice['status']!='received':
            conflict('请先完成实收再登记质检或免检')
        validate_lines(body.lines,'line_id')
        pending = {line['line_id']:line['pending'] for line in notice['lines']}
        postings=[]
        for line in body.lines:
            qty = line.good+line.bad
            if line.line_id not in pending or qty<=0 or (body.mode=='waive' and line.bad):
                invalid('请选择本通知待检行，登记正数件数；免检数量全部转良品')
            if qty>pending[line.line_id]:
                conflict('处置数量超过本次收货剩余待检量')
            postings.append((line.line_id,-qty,line.good,line.bad))
        _event(conn,actor,detail,notice,body.mode,postings,body.note)
        return detail['id'], None
    return _execute(actor, body, 'quality', notice_id, change)


@router.post('/notices/{notice_id}/cancel')
def cancel_notice(notice_id: UUID, body: Correction, actor=Depends(require_user)):
    def change(conn):
        detail, notice=_notice(conn,notice_id,actor)
        if notice['status']!='waiting':
            conflict('仅可取消尚待收货的通知')
        _event(conn,actor,detail,notice,'cancel',[],body.reason)
        conn.execute("UPDATE erp_receiving_notices SET status='cancelled' WHERE id=%s",(notice_id,))
        return detail['id'], None
    return _execute(actor, body, 'cancel', notice_id, change)


@router.post('/events/{event_id}/reverse')
def reverse(event_id: UUID, body: Correction, actor=Depends(require_admin)):
    def change(conn):
        link=conn.execute('SELECT notice_id FROM erp_receiving_events WHERE id=%s',(event_id,)).fetchone()
        if not link:
            raise HTTPException(404,'库存事件不存在')
        notice_id=link['notice_id']
        detail,notice=_notice(conn,notice_id,actor)
        event=next(e for e in notice['events'] if e['id']==event_id)
        if event['reversed'] or event['action'] in ('cancel','reverse'):
            conflict('该事件已冲销或没有可反向过账的数量')
        if event['action'].startswith('receive_'):
            if conn.execute('SELECT 1 FROM erp_payable_sources WHERE event_id=%s AND active',(event_id,)).fetchone():
                conflict('此收货已关联有效供应商应付单，请先核对账单并处理未付的应付单')
            if any(e['action'] in ('inspect','waive') and not e['reversed'] for e in notice['events']):
                conflict('请先冲销此收货通知的所有质检和免检记录')
            capacity={line['id']:line['unnotified'] for line in detail['lines']}
            if any(line['qty']>capacity[line['line_id']]+line['received'] for line in notice['lines']):
                conflict('重开此通知会超占采购量，请先处理后续通知')
            conn.execute("UPDATE erp_receiving_notices SET status='waiting' WHERE id=%s",(notice_id,))
        postings=[(r['line_id'],-r['pending_delta'],-r['good_delta'],-r['bad_delta']) for r in event['lines']]
        _event(conn,actor,detail,notice,'reverse',postings,body.reason,event_id)
        return detail['id'], None
    return _execute(actor, body, 'reverse', event_id, change)

class Barcode(Input):
    scope_id: UUID
    fnsku: Name
    sku: Name
    expected_sku: Name | None = None


def scan_code(raw):
    value = raw.strip()
    if not value or not value.isprintable():
        invalid('请输入完整的单号、SKU 或已登记的 FNSKU')
    return value


@router.get('/barcodes')
def barcodes(scope_id: UUID, fnsku: str | None=Query(None,max_length=140),
             limit: int=Query(25,ge=1,le=100), offset: int=Query(0,ge=0), actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        _scope(conn,scope_id,actor)
        code=scan_code(fnsku) if fnsku is not None else None
        rows=conn.execute("""SELECT scope_id,fnsku,sku,updated_at FROM erp_receiving_barcodes
            WHERE scope_id=%s AND (%s::text IS NULL OR fnsku=%s) ORDER BY fnsku LIMIT %s OFFSET %s""",
            (scope_id,code,code,limit+1,offset)).fetchall()
        more=len(rows)>limit
        return {'items':rows[:limit],'has_more':more,'next_offset':offset+limit if more else None}


@router.post('/barcodes',status_code=201)
def set_barcode(body: Barcode, actor=Depends(require_admin)):
    code, sku = scan_code(body.fnsku), scan_code(body.sku)
    with connection() as conn:
        _scope(conn,body.scope_id,actor,lock=True)
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                     ('receiving-barcode:'+str(body.scope_id)+':'+code,))
        exists=conn.execute("""SELECT 1 FROM erp_receiving_order_lines l JOIN erp_receiving_orders o ON o.id=l.order_id
            WHERE o.scope_id=%s AND l.sku=%s LIMIT 1""",(body.scope_id,sku)).fetchone()
        if not exists:
            invalid('请先核对此仓库中已有采购行的精确 SKU')
        old=conn.execute('SELECT * FROM erp_receiving_barcodes WHERE scope_id=%s AND fnsku=%s',
                         (body.scope_id,code)).fetchone()
        previous=old['sku'] if old else None
        if old and previous==sku:
            return old
        if previous!=body.expected_sku:
            conflict('该 FNSKU 已登记或绑定已更改，请读取原绑定并确认后再改配')
        result=conn.execute("""INSERT INTO erp_receiving_barcodes(scope_id,fnsku,sku,updated_by)
            VALUES(%s,%s,%s,%s) ON CONFLICT(scope_id,fnsku) DO UPDATE
            SET sku=EXCLUDED.sku,updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp() RETURNING *""",
            (body.scope_id,code,sku,actor['id'])).fetchone()
        conn.execute("""INSERT INTO erp_receiving_barcode_history(scope_id,fnsku,previous_sku,sku,actor_id)
                      VALUES(%s,%s,%s,%s,%s)""",(body.scope_id,code,previous,sku,actor['id']))
        return result


@router.get('/scan')
def scan(scope_id: UUID, code: str=Query(min_length=1,max_length=140), limit: int=Query(25,ge=1,le=100),
         offset: int=Query(0,ge=0), actor=Depends(require_user)):
    code=scan_code(code)
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        scope=_scope(conn,scope_id,actor)
        alias=conn.execute('SELECT sku FROM erp_receiving_barcodes WHERE scope_id=%s AND fnsku=%s',
                           (scope_id,code)).fetchone()
        mapped=alias['sku'] if alias else None
        # Page by notice. A multi-line notice appears once, even if both the document number
        # and a SKU match. Only after this step read all the notice's actual receipt lines.
        rows=conn.execute("""SELECT n.id AS notice_id,n.number AS notice_number,o.id AS order_id,
             o.number AS order_number,o.supplier FROM erp_receiving_notices n JOIN erp_receiving_orders o ON o.id=n.order_id
             WHERE o.scope_id=%s AND n.status='waiting' AND (n.number=%s OR o.number=%s OR EXISTS (
               SELECT 1 FROM erp_receiving_notice_lines nl JOIN erp_receiving_order_lines l ON l.id=nl.line_id
               WHERE nl.notice_id=n.id AND l.sku IN (%s,%s)))
             ORDER BY n.created_at,n.id LIMIT %s OFFSET %s""",(scope_id,code,code,code,mapped,limit+1,offset)).fetchall()
        page=rows[:limit]
        by_notice={row['notice_id']:row for row in page}
        for row in page:
            row.update(scope_id=scope_id,warehouse=scope['warehouse'],lines=[],matched_by=[])
            for key in ('order_number','notice_number'):
                if row[key]==code:
                    row['matched_by'].append(key)
        if page:
            lines=conn.execute("""SELECT nl.notice_id,nl.line_id,l.sku,nl.qty
                FROM erp_receiving_notice_lines nl JOIN erp_receiving_order_lines l ON l.id=nl.line_id
                WHERE nl.notice_id=ANY(%s) ORDER BY nl.notice_id,l.position""",(list(by_notice),)).fetchall()
            for line in lines:
                row=by_notice[line.pop('notice_id')]
                line['matched_by']=[label for label,value in [('sku',code),('fnsku',mapped)] if value==line['sku']]
                line['matched']=bool(line['matched_by'])
                row['lines'].append(line)
                for label in line['matched_by']:
                    if label not in row['matched_by']:
                        row['matched_by'].append(label)
        more=len(rows)>limit
        return {'scope_id':scope_id,'code':code,'mapped_sku':mapped,'items':page,'has_more':more,
                'next_offset':offset+limit if more else None}
