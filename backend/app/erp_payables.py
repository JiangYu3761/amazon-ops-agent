"""Supplier liabilities and payment evidence, recorded in the warehouse's authorized scope."""
from datetime import date
from decimal import Decimal
import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from psycopg.errors import UniqueViolation

from .db import connection
from .erp_receiving import Command, Correction, Input, _scope, conflict, invalid
from .erp_refunds import Amount, Currency, Reference, THREE_DIGITS, ZERO_DIGITS, check_amount
from .workflow_auth import require_admin, require_user

router = APIRouter(prefix='/api/erp-payables', tags=['Supplier payables'])


class Source(Input):
    event_id: UUID
    line_id: UUID


class NewBill(Command):
    scope_id: UUID
    document_ref: Reference
    currency: Currency
    amount: Amount
    sources: list[Source] = Field(min_length=1, max_length=200)
    note: str = Field(default='', max_length=2000)


class Payment(Command):
    external_ref: Reference
    amount: Amount
    paid_on: date
    note: str = Field(default='', max_length=2000)


SOURCE = '''SELECT e.id AS event_id,l.id AS line_id,n.id AS notice_id,n.number AS notice_number,
    o.id AS order_id,o.number AS order_number,o.supplier,l.sku,
    g.pending_delta+g.good_delta+g.bad_delta AS received_qty,s.id AS scope_id,s.account_ref,s.site,s.warehouse
    FROM erp_receiving_ledger g JOIN erp_receiving_events e ON g.event_id=e.id
    JOIN erp_receiving_order_lines l ON l.id=g.line_id JOIN erp_receiving_orders o ON o.id=l.order_id
    JOIN erp_receiving_notices n ON n.id=e.notice_id JOIN erp_receiving_scopes s ON s.id=o.scope_id '''
AVAILABLE = """e.action IN ('receive_normal','receive_quick') AND n.status='received'
    AND NOT EXISTS(SELECT 1 FROM erp_receiving_events r WHERE r.reverses=e.id)
    AND NOT EXISTS(SELECT 1 FROM erp_payable_sources ps WHERE ps.event_id=e.id AND ps.line_id=l.id AND ps.active)"""
ACCESS = '''(%s OR EXISTS(SELECT 1 FROM erp_receiving_members m WHERE m.scope_id=s.id AND m.user_id=%s))
    AND (%s::uuid IS NULL OR s.id=%s)'''
BILL = '''SELECT b.*,s.warehouse,s.account_ref,s.site,COALESCE(p.total,0) AS paid FROM erp_payable_bills b
    JOIN erp_receiving_scopes s ON s.id=b.scope_id LEFT JOIN LATERAL (
        SELECT sum(amount) AS total FROM erp_payable_payments WHERE bill_id=b.id) p ON true '''


def exact(value, currency):
    # Money travels as decimal text; a JSON float cannot represent arbitrary cent values.
    digits = 0 if currency in ZERO_DIGITS else 3 if currency in THREE_DIGITS else 4 if currency in {'CLF','UYW'} else 2
    return format(value, f'.{digits}f')


def describe(row):
    paid, amount = row['paid'], row['amount']
    status = 'void' if row['void_at'] else 'paid' if paid == amount else 'partial' if paid else 'open'
    return {**row, 'status': status, 'amount_source':'manual', 'paid': exact(paid,row['currency']), 'amount': exact(amount,row['currency']),
            'balance': exact(Decimal(0) if status=='void' else amount-paid,row['currency'])}


def detail(conn, bill_id, actor):
    row = conn.execute(BILL+' WHERE b.id=%s',(bill_id,)).fetchone()
    if not row:
        raise HTTPException(404,'应付单不存在或不可见')
    _scope(conn,row['scope_id'],actor)
    result = describe(row)
    result['sources'] = conn.execute(SOURCE+''' JOIN erp_payable_sources ps ON ps.event_id=e.id AND ps.line_id=l.id
                      WHERE ps.bill_id=%s ORDER BY o.number,l.position,e.seq''',(bill_id,)).fetchall()
    result['payments'] = conn.execute('SELECT * FROM erp_payable_payments WHERE bill_id=%s ORDER BY created_at,id',
                                      (bill_id,)).fetchall()
    for payment in result['payments']:
        payment['amount'] = exact(payment['amount'],row['currency'])
    return result


def read_page(rows, limit, offset):
    more = len(rows)>limit
    return {'items': rows[:limit], 'has_more': more, 'next_offset': offset+limit if more else None}


@router.get('/sources')
def sources(scope_id: UUID | None=None, limit: int=Query(100,ge=1,le=500), offset: int=Query(0,ge=0),
            actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if scope_id:
            _scope(conn,scope_id,actor)
        rows = conn.execute(SOURCE+' WHERE '+AVAILABLE+' AND '+ACCESS+' ORDER BY e.seq DESC,l.position LIMIT %s OFFSET %s',
                            (actor['is_admin'],actor['id'],scope_id,scope_id,limit+1,offset)).fetchall()
        return read_page(rows,limit,offset)


@router.get('/bills')
def bills(scope_id: UUID | None=None, limit: int=Query(100,ge=1,le=500), offset: int=Query(0,ge=0),
          actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if scope_id:
            _scope(conn,scope_id,actor)
        rows = conn.execute(BILL+' WHERE '+ACCESS+' ORDER BY b.created_at DESC,b.id LIMIT %s OFFSET %s',
                            (actor['is_admin'],actor['id'],scope_id,scope_id,limit+1,offset)).fetchall()
        return read_page([describe(r) for r in rows],limit,offset)


@router.get('/bills/{bill_id}')
def bill(bill_id: UUID,actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return detail(conn,bill_id,actor)


def validate_amount(amount,currency):
    try:
        check_amount(amount,currency)
    except ValueError as error:
        invalid(str(error))


def mutate(actor, body, operation, target, change):
    fingerprint=hashlib.sha256(json.dumps([operation,str(target),body.model_dump(mode='json')],
                                          ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    with connection() as conn:
        key='payable:'+str(actor['id'])+':'+str(body.request_id)
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
        prior=conn.execute('SELECT digest,bill_id FROM erp_payable_requests WHERE actor_id=%s AND request_id=%s',
                           (actor['id'],body.request_id)).fetchone()
        if prior:
            if prior['digest']!=fingerprint:
                conflict('此请求编号已用于不同内容')
            bill_id=prior['bill_id']
            lock_bill(conn,bill_id,actor)
        else:
            bill_id=change(conn)
            conn.execute('INSERT INTO erp_payable_requests(actor_id,request_id,digest,bill_id) VALUES(%s,%s,%s,%s)',
                         (actor['id'],body.request_id,fingerprint,bill_id))
        return detail(conn,bill_id,actor)


@router.post('/bills',status_code=201)
def add_bill(body: NewBill,actor=Depends(require_user)):
    def change(conn):
        _scope(conn,body.scope_id,actor)
        validate_amount(body.amount,body.currency)
        chosen={(source.event_id,source.line_id) for source in body.sources}
        if len(chosen)!=len(body.sources):
            invalid('请勿重复选取相同的收货行')
        event_ids=list({event for event,_ in chosen})
        # Serialize with receipt reversal. Lock every affected order in the same sequence.
        conn.execute('''SELECT o.id FROM erp_receiving_orders o WHERE o.scope_id=%s AND EXISTS(
            SELECT 1 FROM erp_receiving_notices n JOIN erp_receiving_events e ON e.notice_id=n.id
            WHERE n.order_id=o.id AND e.id=ANY(%s)) ORDER BY o.id FOR UPDATE OF o''',(body.scope_id,event_ids)).fetchall()
        _scope(conn,body.scope_id,actor,lock=True)
        rows=conn.execute(SOURCE+''' WHERE s.id=%s AND e.id=ANY(%s) AND '''+AVAILABLE,
                          (body.scope_id,event_ids)).fetchall()
        matched=[row for row in rows if (row['event_id'],row['line_id']) in chosen]
        if len(matched)!=len(chosen):
            conflict('来源已冲销、被其他应付单占用或不属于该仓库，请刷新来源清单')
        suppliers={row['supplier'] for row in matched}
        if len(suppliers)!=1:
            invalid('一张应付单只能关联同一供应商的收货行')
        try:
            row=conn.execute('''INSERT INTO erp_payable_bills(scope_id,supplier,document_ref,currency,amount,note,created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (body.scope_id,suppliers.pop(),body.document_ref,body.currency,body.amount,body.note.strip(),actor['id'])).fetchone()
            for event,line in sorted(chosen):
                conn.execute('INSERT INTO erp_payable_sources(bill_id,event_id,line_id) VALUES(%s,%s,%s)',
                             (row['id'],event,line))
        except UniqueViolation:
            conflict('供应商凭证或收货行已登记，请核对应付单')
        return row['id']
    return mutate(actor,body,'bill',None,change)


def lock_bill(conn,bill_id,actor):
    row=conn.execute('SELECT * FROM erp_payable_bills WHERE id=%s FOR UPDATE',(bill_id,)).fetchone()
    if not row:
        raise HTTPException(404,'应付单不存在或不可见')
    _scope(conn,row['scope_id'],actor,lock=True)
    return row


@router.post('/bills/{bill_id}/payments',status_code=201)
def add_payment(bill_id: UUID,body: Payment,actor=Depends(require_user)):
    def change(conn):
        bill=lock_bill(conn,bill_id,actor)
        validate_amount(body.amount,bill['currency'])
        if bill['void_at']:
            conflict('已作废应付单不能登记付款')
        # One bank/voucher reference identifies a payment within this warehouse scope.
        key='payable-reference:'+str(bill['scope_id'])+':'+body.external_ref
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
        old=conn.execute('SELECT * FROM erp_payable_payments WHERE scope_id=%s AND external_ref=%s',
                         (bill['scope_id'],body.external_ref)).fetchone()
        if old:
            if (old['bill_id'],old['amount'],old['paid_on'],old['note'])!=(bill_id,body.amount,body.paid_on,body.note.strip()):
                conflict('同一外部流水已对应其他账单或付款内容')
            return bill_id
        paid=conn.execute('SELECT coalesce(sum(amount),0) total FROM erp_payable_payments WHERE bill_id=%s',
                          (bill_id,)).fetchone()['total']
        if paid+body.amount>bill['amount']:
            conflict('付款额超过该单剩余应付金额')
        conn.execute('''INSERT INTO erp_payable_payments(bill_id,scope_id,external_ref,amount,paid_on,note,actor_id)
                     VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                     (bill_id,bill['scope_id'],body.external_ref,body.amount,body.paid_on,body.note.strip(),actor['id']))
        return bill_id
    return mutate(actor,body,'payment',bill_id,change)


@router.post('/bills/{bill_id}/void')
def void_bill(bill_id: UUID,body: Correction,actor=Depends(require_admin)):
    def change(conn):
        current=lock_bill(conn,bill_id,actor)
        if current['void_at'] or conn.execute('SELECT 1 FROM erp_payable_payments WHERE bill_id=%s',(bill_id,)).fetchone():
            conflict('仅尚未登记付款的有效应付单可作废')
        conn.execute('UPDATE erp_payable_bills SET void_by=%s,void_at=clock_timestamp(),void_reason=%s WHERE id=%s',
                     (actor['id'],body.reason,bill_id))
        conn.execute('UPDATE erp_payable_sources SET active=false WHERE bill_id=%s',(bill_id,))
        return bill_id
    return mutate(actor,body,'void',bill_id,change)
