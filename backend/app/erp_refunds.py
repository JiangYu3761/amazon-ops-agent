"""Local order-line reconciliation against manually entered settlement rows."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .db import connection
from .workflow_auth import require_user

router = APIRouter(prefix='/api/erp-refunds', tags=['Local refund reconciliation'])

def printable(value):
    if not value.isprintable():
        raise ValueError('引用与标记说明不得包含控制字符')
    return value


Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150),
                      AfterValidator(printable)]
Currency = Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r'^[A-Z]{3}$')]
Amount = Annotated[Decimal, Field(gt=0, lt=10**14, max_digits=18, decimal_places=4)]
ZERO_DIGITS = {'BIF','CLP','DJF','GNF','ISK','JPY','KMF','KRW','PYG','RWF','UGX','UYI','VND','VUV','XAF','XOF','XPF'}
THREE_DIGITS = {'BHD','IQD','JOD','KWD','LYD','OMR','TND'}


def currency_digits(currency):
    if currency in ZERO_DIGITS:
        return 0
    if currency in THREE_DIGITS:
        return 3
    return 4 if currency in {'CLF','UYW'} else 2


def money_text(amount, currency):
    return format(amount, f'.{currency_digits(currency)}f')


def check_amount(amount, currency):
    digits = currency_digits(currency)
    if amount != amount.quantize(Decimal(1).scaleb(-digits)):
        raise ValueError(f'{currency} 金额最多允许 {digits} 位小数')


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class NewLine(Body):
    product_id: UUID
    account_ref: Reference
    order_ref: Reference
    line_ref: Reference
    sku: Reference
    currency: Currency
    refundable_amount: Amount

    @model_validator(mode='after')
    def precision(self):
        check_amount(self.refundable_amount, self.currency)
        return self


class NewEntry(Body):
    entry_ref: Reference
    statement_ref: Reference
    currency: Currency
    amount: Amount
    posted_on: date

    @model_validator(mode='after')
    def precision(self):
        check_amount(self.amount, self.currency)
        return self


class SetMark(Body):
    marked: bool
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500), AfterValidator(printable)]
    expected_version: int = Field(ge=1)


# Members see their own lines. Administrators reconcile all submitted lines against statements.
def access(actor):
    if actor['is_admin']:
        return 'TRUE', []
    return 'l.created_by=%s', [actor['id']]


def line(conn, line_id, actor, lock=False):
    test, args = access(actor)
    row = conn.execute('SELECT l.* FROM erp_refund_lines l WHERE l.id=%s AND ' + test +
                       (' FOR UPDATE' if lock else ''), (line_id, *args)).fetchone()
    if not row:
        raise HTTPException(404, '未找到有权限查看的订单行')
    return row


def summary(row, count, total):
    state = 'full' if total == row['refundable_amount'] else 'partial' if count else 'marked' if row['marked'] else 'unmarked'
    currency = row['currency']
    return {**row, 'refundable_amount':money_text(row['refundable_amount'],currency), 'entry_count':count,
            'recorded_amount':money_text(total,currency), 'remaining_amount':money_text(row['refundable_amount']-total,currency),
            'state':state}


def page(rows, limit, offset):
    return {'items':rows[:limit], 'next_offset':offset + limit if len(rows) > limit else None}


def detail(conn, row, actor, limit=10, offset=0, mark_offset=0):
    entries = conn.execute('''SELECT r.id,r.entry_ref,r.statement_ref,r.currency,r.amount,r.posted_on,r.recorded_at,
            u.display_name recorded_by_name FROM erp_refund_entries r JOIN app_users u ON u.id=r.recorded_by
            WHERE r.line_id=%s ORDER BY r.recorded_at DESC,r.id DESC LIMIT %s OFFSET %s''',
            (row['id'], limit+1, offset)).fetchall()
    for entry in entries:
        entry['amount'] = money_text(entry['amount'],entry['currency'])
    stats = conn.execute('''SELECT count(*) n,COALESCE(sum(amount),0) total FROM erp_refund_entries WHERE line_id=%s''',
                         (row['id'],)).fetchone()
    marks = conn.execute('''SELECT m.marked,m.reason,m.created_at,u.display_name actor_name FROM erp_refund_marks m
            JOIN app_users u ON u.id=m.actor_id WHERE m.line_id=%s ORDER BY m.version DESC LIMIT %s OFFSET %s''',
            (row['id'], limit+1, mark_offset)).fetchall()
    return {**summary(row,stats['n'],stats['total']), 'entries':page(entries,limit,offset),
            'marks':page(marks,limit,mark_offset), 'can_record_entry':bool(actor['is_admin']),
            'source':'manual_statement_reconciliation'}


@router.get('/lines')
def list_lines(product_id: UUID | None=None, order_ref: str|None=None, account_ref: str|None=None,
               state: Literal['unmarked','marked','partial','full']|None=None,
               limit: int=Query(25,ge=1,le=100), offset: int=Query(0,ge=0), actor=Depends(require_user)):
    with connection() as conn:
        test, params = access(actor)
        for key,value in (('product_id',product_id),('order_ref',order_ref),('account_ref',account_ref)):
            if value:
                test += ' AND l.' + key + '=%s'
                params.append(value)
        if state:
            test += ' AND ' + {'unmarked':'(n=0 AND NOT marked)', 'marked':'(n=0 AND marked)',
                               'partial':'(n>0 AND total<refundable_amount)', 'full':'(total=refundable_amount)'}[state]
        rows = conn.execute('''SELECT l.*,r.n,r.total FROM erp_refund_lines l CROSS JOIN LATERAL
            (SELECT count(*) n,COALESCE(sum(amount),0) total FROM erp_refund_entries WHERE line_id=l.id) r WHERE ''' +
            test + ' ORDER BY l.created_at DESC,l.id DESC LIMIT %s OFFSET %s', (*params,limit+1,offset)).fetchall()
        results = []
        for row in rows:
            count, total = row.pop('n'), row.pop('total')
            results.append(summary(row,count,total))
        return page(results,limit,offset)


@router.get('/lines/{line_id}')
def get_line(line_id: UUID, limit: int=Query(25,ge=1,le=100), offset: int=Query(0,ge=0),
             mark_offset: int=Query(0,ge=0), actor=Depends(require_user)):
    with connection() as conn:
        # A mark or entry must lock this parent. Keep row, total and paged ledgers on the same revision.
        return detail(conn, line(conn,line_id,actor,lock=True), actor, limit, offset, mark_offset)


@router.post('/lines', status_code=201)
def register_line(body: NewLine, actor=Depends(require_user)):
    with connection() as conn:
        product = conn.execute('SELECT marketplace,currency,data_account_key FROM products WHERE id=%s',
                               (body.product_id,)).fetchone()
        if not product:
            raise HTTPException(404, '产品不存在')
        if body.currency != product['currency'].upper():
            raise HTTPException(422, '币种须与所选产品主档一致')
        if product['data_account_key'] and product['data_account_key'] != body.account_ref:
            raise HTTPException(422, '店铺与产品已绑定的账号不一致')
        scope = (body.account_ref,product['marketplace'],body.order_ref,body.line_ref)
        row = conn.execute('''INSERT INTO erp_refund_lines(account_ref,site,order_ref,line_ref,product_id,sku,
            currency,refundable_amount,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(account_ref,site,order_ref,line_ref) DO NOTHING RETURNING *''',
            (*scope,body.product_id,body.sku,body.currency,body.refundable_amount,actor['id'])).fetchone()
        if row is None:
            owner_test, params = access(actor)
            row = conn.execute('''SELECT l.* FROM erp_refund_lines l WHERE
                (account_ref,site,order_ref,line_ref)=(%s,%s,%s,%s) AND ''' + owner_test + ' FOR UPDATE', (*scope,*params)).fetchone()
            if row is None or any(row[k] != getattr(body,k) for k in ('product_id','sku','currency','refundable_amount')):
                raise HTTPException(409,'订单行已登记，请核对店铺、订单号、行号和原始金额')
        return detail(conn,row,actor)


@router.put('/lines/{line_id}/mark')
def set_mark(line_id: UUID, body: SetMark, actor=Depends(require_user)):
    with connection() as conn:
        row = line(conn,line_id,actor,lock=True)
        if row['version'] != body.expected_version:
            last = conn.execute('''SELECT 1 FROM erp_refund_marks WHERE line_id=%s AND version=%s
                AND actor_id=%s AND marked=%s AND reason=%s''',
                (line_id,body.expected_version+1,actor['id'],body.marked,body.reason)).fetchone()
            if last:
                return detail(conn,row,actor)
            raise HTTPException(409,'订单行已更新，请刷新后再修改标记')
        if row['marked'] != body.marked:
            row = conn.execute('''UPDATE erp_refund_lines SET marked=%s,version=version+1,updated_at=now()
                WHERE id=%s RETURNING *''',(body.marked,line_id)).fetchone()
            conn.execute('''INSERT INTO erp_refund_marks(line_id,marked,reason,actor_id,version) VALUES(%s,%s,%s,%s,%s)''',
                         (line_id,body.marked,body.reason,actor['id'],row['version']))
        return detail(conn,row,actor)


@router.post('/lines/{line_id}/entries', status_code=201)
def register_entry(line_id: UUID, body: NewEntry, actor=Depends(require_user)):
    if not actor['is_admin']:
        raise HTTPException(403, '请由流程管理员核对结算单后登记流水')
    with connection() as conn:
        row = line(conn,line_id,actor,lock=True)
        if body.currency != row['currency']:
            raise HTTPException(422,'流水币种与订单行不一致')
        key = (row['account_ref'],row['site'],body.entry_ref)
        prior = conn.execute('''SELECT * FROM erp_refund_entries WHERE account_ref=%s AND site=%s AND entry_ref=%s''',
                             key).fetchone()
        if prior:
            if prior['line_id'] == line_id and all(prior[k] == getattr(body,k) for k in ('amount','currency','statement_ref','posted_on')):
                return detail(conn,row,actor)
            raise HTTPException(409,'该店铺站点的流水行引用已用于另一笔记录')
        recorded = conn.execute('SELECT COALESCE(sum(amount),0) total FROM erp_refund_entries WHERE line_id=%s',
                                (line_id,)).fetchone()['total']
        if recorded + body.amount > row['refundable_amount']:
            raise HTTPException(422,'累计流水金额超过该订单行的可退金额')
        inserted = conn.execute('''INSERT INTO erp_refund_entries(line_id,account_ref,site,entry_ref,statement_ref,
            currency,posted_on,amount,recorded_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(account_ref,site,entry_ref) DO NOTHING RETURNING id''',
            (line_id,*key,body.statement_ref,body.currency,body.posted_on,body.amount,actor['id'])).fetchone()
        if not inserted:
            raise HTTPException(409,'该店铺站点的流水行引用已被另一笔记录占用')
        row = conn.execute('''UPDATE erp_refund_lines SET version=version+1,updated_at=now() WHERE id=%s RETURNING *''',
                           (line_id,)).fetchone()
        return detail(conn,row,actor)
