"""Supplier liabilities and payment evidence, recorded in the warehouse's authorized scope."""
from datetime import date
from decimal import Decimal, localcontext
import hashlib
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .db import connection
from .erp_receiving import Command, Correction, Input, Name, Positive, _scope, conflict, format_contract, invalid
from .erp_refunds import Amount, Currency, Reference, check_amount, money_text
from .workflow_auth import require_admin, require_user

router = APIRouter(prefix='/api/erp-payables', tags=['Supplier payables'])


NonnegativeMoney = Annotated[Decimal, Field(ge=0, lt=10**14, max_digits=18, decimal_places=4)]


class InvoiceLine(Input):
    unit: Name
    qty: Positive
    net_unit_price: NonnegativeMoney
    tax_amount: NonnegativeMoney
    amount: NonnegativeMoney


class Source(Input):
    event_id: UUID
    line_id: UUID
    invoice: InvoiceLine | None = None


class BillPreview(Input):
    scope_id: UUID
    currency: Currency
    amount: Amount
    sources: list[Source] = Field(min_length=1, max_length=200)


class NewBill(BillPreview, Command):
    document_ref: Reference
    note: str = Field(default='', max_length=2000)


class Payment(Command):
    external_ref: Reference
    amount: Amount
    paid_on: date
    note: str = Field(default='', max_length=2000)


RECEIPT_COLUMNS = '''e.id AS event_id,l.id AS line_id,n.id AS notice_id,n.number AS notice_number,
    o.id AS order_id,o.number AS order_number,o.supplier,l.sku,l.contract_currency,l.net_unit_price,l.unit_tax,
    g.pending_delta+g.good_delta+g.bad_delta AS received_qty,s.id AS scope_id,s.account_ref,s.site,s.warehouse'''
RECEIPT_JOINS = ''' FROM erp_receiving_ledger g JOIN erp_receiving_events e ON g.event_id=e.id
    JOIN erp_receiving_order_lines l ON l.id=g.line_id JOIN erp_receiving_orders o ON o.id=l.order_id
    JOIN erp_receiving_notices n ON n.id=e.notice_id JOIN erp_receiving_scopes s ON s.id=o.scope_id '''
SOURCE = 'SELECT ' + RECEIPT_COLUMNS + RECEIPT_JOINS
AVAILABLE = """e.action IN ('receive_normal','receive_quick') AND n.status='received'
    AND NOT EXISTS(SELECT 1 FROM erp_receiving_events r WHERE r.reverses=e.id)
    AND NOT EXISTS(SELECT 1 FROM erp_payable_sources ps WHERE ps.event_id=e.id AND ps.line_id=l.id AND ps.active)"""
ACCESS = '''(%s OR EXISTS(SELECT 1 FROM erp_receiving_members m WHERE m.scope_id=s.id AND m.user_id=%s))
    AND (%s::uuid IS NULL OR s.id=%s)'''
BILL = '''SELECT b.*,s.warehouse,s.account_ref,s.site,COALESCE(p.total,0) AS paid FROM erp_payable_bills b
    JOIN erp_receiving_scopes s ON s.id=b.scope_id LEFT JOIN LATERAL (
        SELECT sum(amount) AS total FROM erp_payable_payments WHERE bill_id=b.id) p ON true '''


def receipt_values(row):
    currency = row['contract_currency']
    net = row['net_unit_price']
    tax = row['unit_tax']
    qty = row['received_qty']
    return {**format_contract(dict(row)), 'pricing_status': 'priced' if currency else 'unpriced',
            'expected_net': money_text(qty * net, currency) if currency else None,
            'expected_tax': money_text(qty * tax, currency) if currency else None,
            'expected_amount': money_text(qty * (net + tax), currency) if currency else None}


def describe(row):
    paid, amount = row['paid'], row['amount']
    status = 'void' if row['void_at'] else 'paid' if paid == amount else 'partial' if paid else 'open'
    return {**row, 'status': status, 'paid': money_text(paid,row['currency']), 'amount': money_text(amount,row['currency']),
            'balance': money_text(Decimal(0) if status=='void' else amount-paid,row['currency'])}


def detail(conn, bill_id, actor):
    row = conn.execute(BILL+' WHERE b.id=%s',(bill_id,)).fetchone()
    if not row:
        raise HTTPException(404,'应付单不存在或不可见')
    _scope(conn,row['scope_id'],actor)
    result = describe(row)
    stored_source = 'SELECT ps.invoice,' + RECEIPT_COLUMNS + RECEIPT_JOINS
    rows = conn.execute(stored_source+''' JOIN erp_payable_sources ps ON ps.event_id=e.id AND ps.line_id=l.id
                      WHERE ps.bill_id=%s ORDER BY o.number,l.position,e.seq''',(bill_id,)).fetchall()
    result['sources'] = [receipt_values(item) for item in rows]
    result['payments'] = conn.execute('SELECT * FROM erp_payable_payments WHERE bill_id=%s ORDER BY created_at,id',
                                      (bill_id,)).fetchall()
    for payment in result['payments']:
        payment['amount'] = money_text(payment['amount'],row['currency'])
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
        return read_page([receipt_values(row) for row in rows],limit,offset)


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
    payload = body.model_dump(mode='json')
    if isinstance(body, NewBill):
        # Previously issued request receipts omit the optional invoice line entirely.
        for line in payload['sources']:
            if line['invoice'] is None:
                line.pop('invoice')
    fingerprint=hashlib.sha256(json.dumps([operation,str(target),payload],
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


def selected_receipts(conn, body, actor, *, lock):
    _scope(conn, body.scope_id, actor)
    validate_amount(body.amount, body.currency)
    keys = [(source.event_id, source.line_id) for source in body.sources]
    if len(set(keys)) != len(keys):
        invalid('请勿重复选取相同的收货行')
    event_ids = list({event for event, _ in keys})
    if lock:
        # Serialize with bill creation, receipt reversal and changes to warehouse membership.
        conn.execute('''SELECT o.id FROM erp_receiving_orders o WHERE o.scope_id=%s AND EXISTS(
            SELECT 1 FROM erp_receiving_notices n JOIN erp_receiving_events e ON e.notice_id=n.id
            WHERE n.order_id=o.id AND e.id=ANY(%s)) ORDER BY o.id FOR UPDATE OF o''', (body.scope_id,event_ids)).fetchall()
        _scope(conn, body.scope_id, actor, lock=True)
    rows = conn.execute(SOURCE+' WHERE s.id=%s AND e.id=ANY(%s) AND '+AVAILABLE,
                        (body.scope_id, event_ids)).fetchall()
    index = {(row['event_id'],row['line_id']):row for row in rows}
    if not all(key in index for key in keys):
        conflict('来源已冲销、被其他应付单占用或不属于该仓库，请刷新来源清单')
    selected = [index[key] for key in keys]
    if len({row['supplier'] for row in selected}) != 1:
        invalid('一张应付单只能关联同一供应商的收货行')
    return selected


def compare_invoice(row, invoice, currency):
    differences = []
    if not row['contract_currency']:
        if invoice:
            differences.append('此采购行无合同价，不能登记为三单匹配')
        return differences
    if row['contract_currency'] != currency:
        differences.append(f"合同币种 {row['contract_currency']} 与发票币种 {currency} 不同")
    if not invoice:
        return differences + ['请录入对应的发票行']
    comparisons = [
        ('计量单位', invoice.unit, '件'), ('实收数量', invoice.qty, row['received_qty']),
        ('每件净价', invoice.net_unit_price, row['net_unit_price']),
        ('本行税额', invoice.tax_amount, row['unit_tax'] * row['received_qty']),
        ('本行含税金额', invoice.amount, (row['net_unit_price'] + row['unit_tax']) * row['received_qty'])
    ]
    for label, entered, expected in comparisons:
        if entered != expected:
            differences.append(f'{label}应为 {expected}，发票录入 {entered}')
    if invoice.qty * invoice.net_unit_price + invoice.tax_amount != invoice.amount:
        differences.append('发票行含税金额与发票数量 × 净单价 + 行税额不相等')
    return differences


def match_result(body, selected):
    differences, lines = [], []
    currencies = {row['contract_currency'] for row in selected}
    if None in currencies and len(currencies) > 1:
        differences.append('请分别登记已计价采购行和无合同价的采购行')
    for source, row in zip(body.sources, selected):
        problems = compare_invoice(row, source.invoice, body.currency)
        label = f"{row['order_number']} / {row['notice_number']} / {row['sku']}"
        differences.extend(label + '：' + item for item in problems)
        lines.append({**receipt_values(row), 'differences': problems,
                      'invoice': source.invoice.model_dump(mode='json') if source.invoice else None})
    total = None
    if currencies == {body.currency}:
        with localcontext() as ctx:
            ctx.prec = 40
            total = sum(Decimal(line['expected_amount']) for line in lines)
        if body.amount != total:
            differences.append(f'发票总额应为 {total} {body.currency}，录入 {body.amount}')
    status = 'difference' if differences else ('manual' if currencies == {None} else 'matched')
    return {'status': status, 'differences': differences, 'currency': body.currency, 'lines': lines,
            'expected_amount': money_text(total, body.currency) if total is not None else None}


@router.post('/match-preview')
def preview(body: BillPreview, actor=Depends(require_user)):
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return match_result(body, selected_receipts(conn, body, actor, lock=False))


@router.post('/bills',status_code=201)
def add_bill(body: NewBill,actor=Depends(require_user)):
    def change(conn):
        selected = selected_receipts(conn, body, actor, lock=True)
        result = match_result(body, selected)
        if result['status'] == 'difference':
            invalid('；'.join(result['differences']))
        method = 'po_receipt_invoice' if result['status'] == 'matched' else 'manual'
        try:
            row=conn.execute('''INSERT INTO erp_payable_bills(scope_id,supplier,document_ref,currency,amount,note,created_by,amount_source)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (body.scope_id,selected[0]['supplier'],body.document_ref,body.currency,body.amount,body.note.strip(),actor['id'],method)).fetchone()
            for item in result['lines']:
                conn.execute('INSERT INTO erp_payable_sources(bill_id,event_id,line_id,invoice) VALUES(%s,%s,%s,%s)',
                             (row['id'], item['event_id'], item['line_id'], Jsonb(item['invoice']) if item['invoice'] else None))
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
