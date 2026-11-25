"""Reconciliation of one external payout statement in its original currency."""
import csv
import hashlib
import io
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import Field, StringConstraints, ValidationError, model_validator

from .db import connection
from .erp_refunds import Body, Currency, Reference, check_amount, money_text, page
from .fee_allocation_ledger import reverse_active, reverse_on_date_change
from .workflow_auth import require_user

router = APIRouter(prefix='/api/erp-settlements', tags=['Local payout statements'])
Signed = Annotated[Decimal, Field(gt=-(10**13),lt=10**13,max_digits=17,decimal_places=4)]
Site = Annotated[str,StringConstraints(strip_whitespace=True,to_upper=True,pattern='^[A-Z]{2,4}$')]
HEADER = ('account_ref','site','currency','statement_ref','entry_ref','kind','occurred_on','original_order_date','amount')
PAYOUT_FIELDS = ('payout_date','opening_balance','closing_balance','actual_payout','payout_ref')
MONEY_FIELDS = ('opening_balance','closing_balance','actual_payout')
MAX_FILE = 1024*1024


class Payout(Body):
    payout_date: date
    opening_balance: Signed
    closing_balance: Signed
    actual_payout: Signed
    payout_ref: Reference


def validate_money(body, currency):
    try:
        for key in MONEY_FIELDS:
            check_amount(getattr(body,key),currency)
    except ValueError as error:
        raise HTTPException(422,str(error)) from error


class Statement(Payout):
    account_ref: Reference
    site: Site
    currency: Currency
    statement_ref: Reference


class Revision(Payout):
    expected_version: int = Field(ge=1)
    reason: Reference


class Reversal(Body):
    reason: Reference


class Entry(Body):
    account_ref: Reference
    site: Site
    currency: Currency
    statement_ref: Reference
    entry_ref: Reference
    kind: Literal['sale','fee','refund','adjustment']
    occurred_on: date
    original_order_date: date | None
    amount: Signed

    @model_validator(mode='after')
    def ledger_values(self):
        if self.entry_ref.startswith('REV:'):
            raise ValueError('REV: 前缀保留给本地冲正记录')
        if not self.amount or self.kind=='sale' and self.amount<0 or self.kind in ('refund','fee') and self.amount>0:
            raise ValueError('销售填正额，费用和退款填负额，调整填有依据的非零金额')
        if self.original_order_date and self.original_order_date>self.occurred_on:
            raise ValueError('原订单日期晚于交易日期')
        check_amount(self.amount,self.currency)
        return self


def visible(actor):
    return ('TRUE',[]) if actor['is_admin'] else ('s.created_by=%s',[actor['id']])


def statement(conn, statement_id, actor):
    check,args=visible(actor)
    row=conn.execute('SELECT s.* FROM erp_settlements s WHERE id=%s AND '+check+' FOR UPDATE',
                     (statement_id,*args)).fetchone()
    if not row:
        raise HTTPException(404,'结算单不存在或当前账户无权查看')
    return row


def describe(row, count, net):
    expected=row['opening_balance']+net-row['closing_balance']
    delta=row['actual_payout']-expected
    result={**row,'entry_count':count,'transactions_net':net,'expected_payout':expected,'difference':delta,
            'state':'pending_entries' if not count else 'balanced' if delta==0 else 'difference'}
    for key in (*MONEY_FIELDS,'transactions_net','expected_payout','difference'):
        result[key]=money_text(result[key],row['currency'])
    if not count:
        result.update(transactions_net=None,expected_payout=None,difference=None)
    return result


def revision(conn,row,actor,reason):
    snapshot={k:money_text(row[k],row['currency']) if k in MONEY_FIELDS else row[k] for k in PAYOUT_FIELDS}
    conn.execute('''INSERT INTO erp_settlement_revisions(statement_id,version,snapshot,reason,actor_id)
                    VALUES(%s,%s,%s,%s,%s)''',(row['id'],row['version'],Jsonb(jsonable_encoder(snapshot)),reason,actor['id']))


def details(conn,row,limit=25,offset=0,import_offset=0,revision_offset=0):
    entries=conn.execute('''SELECT t.id,t.entry_ref,t.kind,t.amount,t.occurred_on,t.original_order_date,t.reverses_id,
            t.reason,i.filename,u.display_name recorded_by,EXISTS(SELECT 1 FROM erp_settlement_entries undo
            WHERE undo.reverses_id=t.id) reversed FROM erp_settlement_entries t LEFT JOIN erp_settlement_imports i
            ON t.import_id=i.id JOIN app_users u ON u.id=t.actor_id WHERE t.statement_id=%s
            ORDER BY t.created_at DESC,t.id DESC LIMIT %s OFFSET %s''',(row['id'],limit+1,offset)).fetchall()
    for item in entries:
        item['amount']=money_text(item['amount'],row['currency'])
    categories=conn.execute('''SELECT kind,count(*) n,sum(amount) total FROM erp_settlement_entries
                               WHERE statement_id=%s GROUP BY kind ORDER BY kind''',(row['id'],)).fetchall()
    summary=describe(row,sum(g['n'] for g in categories),sum((g['total'] for g in categories),Decimal(0)))
    for group in categories:
        group['total']=money_text(group['total'],row['currency'])
    revisions=conn.execute('''SELECT r.version,r.snapshot,r.reason,r.created_at,u.display_name actor_name
            FROM erp_settlement_revisions r JOIN app_users u ON u.id=r.actor_id WHERE r.statement_id=%s
            ORDER BY r.version DESC LIMIT 11 OFFSET %s''',(row['id'],revision_offset)).fetchall()
    imports=conn.execute('''SELECT i.filename,i.sha256,i.added,i.skipped,i.created_at,u.display_name actor_name
            FROM erp_settlement_imports i JOIN app_users u ON u.id=i.actor_id WHERE i.statement_id=%s
            ORDER BY i.created_at DESC,i.id DESC LIMIT 11 OFFSET %s''',(row['id'],import_offset)).fetchall()
    return {**summary,'categories':categories,'entries':page(entries,limit,offset),'revisions':page(revisions,10,revision_offset),
            'imports':page(imports,10,import_offset)}


@router.get('/statements')
def statements(account_ref: str | None=None, currency: str | None=None, start: date | None=None,end: date | None=None,
               limit:int=Query(25,ge=1,le=100),offset:int=Query(0,ge=0),actor=Depends(require_user)):
    check,params=visible(actor)
    if start and end and end<start:
        raise HTTPException(422,'发放日期区间无效')
    for sql,value in [('s.account_ref=%s',account_ref),('s.currency=%s',currency.upper() if currency else None),
                      ('s.payout_date>=%s',start),('s.payout_date<=%s',end)]:
        if value is not None:
            check+=' AND '+sql
            params.append(value)
    with connection() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        query=''' FROM erp_settlements s CROSS JOIN LATERAL (SELECT count(*) n,COALESCE(sum(amount),0) net
                  FROM erp_settlement_entries WHERE statement_id=s.id) t WHERE '''+check
        rows=conn.execute('SELECT s.*,n,net'+query+' ORDER BY s.payout_date DESC,s.id DESC LIMIT %s OFFSET %s',
                          (*params,limit+1,offset)).fetchall()
        totals=conn.execute('''SELECT currency,count(*) statement_count,count(*) FILTER(WHERE n=0) pending_count,
            sum(actual_payout) FILTER(WHERE n>0) actual,
            sum(opening_balance+net-closing_balance) FILTER(WHERE n>0) expected,
            COALESCE(sum(actual_payout) FILTER(WHERE n=0),0) pending_actual
            '''+query+' GROUP BY currency ORDER BY currency',params).fetchall()
    for result in totals:
        result['difference']=None if result['expected'] is None else money_text(result['actual']-result['expected'],result['currency'])
        for key in ('actual','expected','pending_actual'):
            if result[key] is not None:
                result[key]=money_text(result[key],result['currency'])
    summaries=[]
    for row in rows:
        count,net=row.pop('n'),row.pop('net')
        summaries.append(describe(row,count,net))
    return {**page(summaries,limit,offset),'currency_totals':totals}


@router.post('/statements',status_code=201)
def create(body:Statement,actor=Depends(require_user)):
    validate_money(body,body.currency)
    natural=(body.account_ref,body.site,body.currency,body.statement_ref)
    with connection() as conn:
        row=conn.execute('''INSERT INTO erp_settlements(account_ref,site,currency,statement_ref,payout_date,
                opening_balance,closing_balance,actual_payout,payout_ref,created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *''',
                (*natural,*(getattr(body,k) for k in PAYOUT_FIELDS),actor['id'])).fetchone()
        if row:
            revision(conn,row,actor,'初次登记')
        else:
            check,params=visible(actor)
            row=conn.execute('''SELECT * FROM erp_settlements s WHERE (account_ref,site,currency,statement_ref)
                            =(%s,%s,%s,%s) AND '''+check+' FOR UPDATE',(*natural,*params)).fetchone()
            if not row or any(row[k]!=getattr(body,k) for k in PAYOUT_FIELDS):
                raise HTTPException(409,'结算单已登记，请核对范围和原始金额')
        return details(conn,row)


@router.get('/statements/{statement_id}')
def get_statement(statement_id:UUID,limit:int=Query(25,ge=1,le=100),offset:int=Query(0,ge=0),
                  import_offset:int=Query(0,ge=0),revision_offset:int=Query(0,ge=0),actor=Depends(require_user)):
    with connection() as conn:
        return details(conn,statement(conn,statement_id,actor),limit,offset,import_offset,revision_offset)


@router.patch('/statements/{statement_id}')
def amend(statement_id:UUID,body:Revision,actor=Depends(require_user)):
    with connection() as conn:
        row=statement(conn,statement_id,actor)
        validate_money(body,row['currency'])
        if row['version']!=body.expected_version:
            raise HTTPException(409,'结算单头已更新，请重新读取再修订')
        if body.payout_date != row['payout_date'] and conn.execute(
                'SELECT 1 FROM erp_settlement_entries WHERE statement_id=%s AND occurred_on>%s LIMIT 1',
                (statement_id,body.payout_date)).fetchone():
            raise HTTPException(422,'修订的发放日早于本单交易日期，请核对结算凭证')
        if body.payout_date != row['payout_date']:
            reverse_on_date_change(conn,statement_id,actor)
        if any(row[k]!=getattr(body,k) for k in PAYOUT_FIELDS):
            row=conn.execute('''UPDATE erp_settlements SET payout_date=%s,opening_balance=%s,closing_balance=%s,
                actual_payout=%s,payout_ref=%s,version=version+1 WHERE id=%s RETURNING *''',
                (*(getattr(body,k) for k in PAYOUT_FIELDS),statement_id)).fetchone()
            revision(conn,row,actor,body.reason)
        return details(conn,row)


def parse_file(data):
    try:
        reader=csv.DictReader(io.StringIO(data.decode('utf-8-sig'),newline=''),strict=True)
        if not reader.fieldnames or len(reader.fieldnames)!=len(HEADER) or set(reader.fieldnames)!=set(HEADER):
            raise HTTPException(422,'CSV 表头须包含模板中的所有列，且列名唯一')
        entries=[]
        for row in reader:
            if len(entries)>=1000:
                raise HTTPException(422,'单次最多 1000 行')
            try:
                row['original_order_date']=row['original_order_date'] or None
                entry=Entry.model_validate(row)
            except (ValidationError,TypeError,KeyError) as error:
                message=error.errors()[0]['msg'] if isinstance(error,ValidationError) else '列数不正确'
                raise HTTPException(422,f'CSV 第 {reader.line_num} 行：{message}') from error
            entries.append((reader.line_num,entry))
    except (UnicodeError,csv.Error) as error:
        raise HTTPException(422,'文件须使用 UTF-8 CSV，且引号正确闭合') from error
    if not entries:
        raise HTTPException(422,'CSV 没有交易行')
    return entries


@router.post('/statements/{statement_id}/imports')
def import_entries(statement_id:UUID,file:UploadFile=File(...),actor=Depends(require_user)):
    data=file.file.read(MAX_FILE+1)
    if len(data)>MAX_FILE:
        raise HTTPException(422,'单次上传限 1 MiB')
    digest=hashlib.sha256(data).hexdigest()
    with connection() as conn:
        row=statement(conn,statement_id,actor)
        prior=conn.execute('SELECT added,skipped,filename FROM erp_settlement_imports WHERE statement_id=%s AND sha256=%s',
                           (statement_id,digest)).fetchone()
        if prior:
            return {'replayed':True,**prior,'sha256':digest}
        entries=parse_file(data)
        filename=(file.filename or 'settlement.csv').split('/')[-1].split('\\')[-1][:150]
        if not filename.isprintable():
            raise HTTPException(422,'文件名包含无效字符')
        batch=conn.execute('''INSERT INTO erp_settlement_imports(statement_id,filename,sha256,actor_id)
                             VALUES(%s,%s,%s,%s) RETURNING id''',(statement_id,filename,digest,actor['id'])).fetchone()['id']
        added=0
        # Acquire unique entry keys in a stable order when two different statements import overlapping files.
        for number,entry in sorted(entries,key=lambda r:r[1].entry_ref):
            if any(row[k]!=getattr(entry,k) for k in ('account_ref','site','currency','statement_ref')):
                raise HTTPException(422,f'CSV 第 {number} 行的店铺、站点、币种或结算单号不匹配')
            if entry.occurred_on>row['payout_date']:
                raise HTTPException(422,f'CSV 第 {number} 行的交易日晚于结算发放日')
            inserted=conn.execute('''INSERT INTO erp_settlement_entries(account_ref,site,currency,
                statement_id,entry_ref,kind,occurred_on,original_order_date,amount,import_id,actor_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id''',
                (row['account_ref'],row['site'],row['currency'],statement_id,entry.entry_ref,entry.kind,entry.occurred_on,
                 entry.original_order_date,entry.amount,batch,actor['id'])).fetchone()
            if inserted:
                added+=1
            else:
                previous=conn.execute('''SELECT * FROM erp_settlement_entries WHERE (account_ref,site,currency,entry_ref)
                                        =(%s,%s,%s,%s)''',(row['account_ref'],row['site'],row['currency'],entry.entry_ref)).fetchone()
                if previous['statement_id']!=statement_id or any(previous[k]!=getattr(entry,k) for k in
                        ('kind','occurred_on','original_order_date','amount')):
                    raise HTTPException(409,f'CSV 第 {number} 行的交易行引用已记录不同内容，本批未写入')
        skipped=len(entries)-added
        conn.execute('UPDATE erp_settlement_imports SET added=%s,skipped=%s WHERE id=%s',(added,skipped,batch))
        return {'replayed':False,'added':added,'skipped':skipped,'sha256':digest,'filename':filename}


@router.post('/statements/{statement_id}/entries/{entry_id}/reversal')
def reverse(statement_id:UUID,entry_id:UUID,body:Reversal,actor=Depends(require_user)):
    with connection() as conn:
        row=statement(conn,statement_id,actor)
        original=conn.execute('SELECT * FROM erp_settlement_entries WHERE statement_id=%s AND id=%s',
                              (statement_id,entry_id)).fetchone()
        if not original or original['kind']=='reversal':
            raise HTTPException(422,'请选择本单中的原交易行冲正')
        previous=conn.execute('SELECT reason FROM erp_settlement_entries WHERE reverses_id=%s',(entry_id,)).fetchone()
        if previous:
            if previous['reason']!=body.reason:
                raise HTTPException(409,'该行已有冲正记录，请查看原原因')
        else:
            if original['kind']=='fee':
                reverse_active(conn,entry_id,actor,'原费用已冲正：'+body.reason)
            conn.execute('''INSERT INTO erp_settlement_entries(statement_id,account_ref,site,currency,entry_ref,kind,
                        occurred_on,original_order_date,amount,reverses_id,reason,actor_id)
                        VALUES(%s,%s,%s,%s,%s,'reversal',%s,%s,%s,%s,%s,%s)''',
                        (statement_id,row['account_ref'],row['site'],row['currency'],'REV:'+str(entry_id),
                         original['occurred_on'],original['original_order_date'],-original['amount'],entry_id,body.reason,actor['id']))
        return details(conn,row)
