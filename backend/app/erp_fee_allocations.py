"""Preview and post expense attribution for a settlement fee without changing platform funds."""
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from .db import connection
from .erp_refunds import Body, Reference, money_text, page
from .erp_settlements import statement
from .fee_allocation_ledger import distribute, last_event, reverse_active
from .workflow_auth import require_user

router=APIRouter(prefix='/api/erp-settlements/statements',tags=['Local fee attribution'])
Weight=Annotated[Decimal,Field(gt=0,le=10**9,max_digits=16,decimal_places=6)]


class Target(Body):
    product_id:UUID
    sku:Reference|None=None
    weight:Weight


class Selection(Body):
    basis:Literal['product','sku']
    targets:list[Target]=Field(min_length=1,max_length=100)
    reason:Reference

    @model_validator(mode='after')
    def identities(self):
        keys=[(t.product_id,t.sku) for t in self.targets]
        if len(set(keys))!=len(keys):
            raise ValueError('同一分摊对象只能选择一次')
        if any((t.sku is not None)!=(self.basis=='sku') for t in self.targets):
            raise ValueError('产品分摊只选产品，SKU 分摊须选择商品 SKU')
        return self


class Post(Body):
    preview_id:UUID
    request_id:UUID


class Void(Body):
    expected_version:int=Field(ge=1)
    reason:Reference
    request_id:UUID


def expense_line(conn,statement_id,entry_id,actor):
    s=statement(conn,statement_id,actor)
    fee=conn.execute('''SELECT e.*,EXISTS(SELECT 1 FROM erp_settlement_entries r WHERE r.reverses_id=e.id) reversed
                       FROM erp_settlement_entries e WHERE e.id=%s AND e.statement_id=%s AND e.kind='fee' ''',
                       (entry_id,statement_id)).fetchone()
    if not fee:
        raise HTTPException(404,'请选择当前结算单的费用交易行')
    return s,fee


def selection_targets(conn,s,basis,ids=None,search='',limit=None,offset=0):
    # Only explicit product master or shop-mapped SKUs are valid recipients of a fee.
    projected='''SELECT p.id product_id,p.model,p.name,NULL::text sku FROM scoped p'''
    args=[s['site'],s['currency'],s['account_ref']]
    if basis=='sku':
        projected='''SELECT p.id product_id,p.model,p.name,p.sku FROM scoped p WHERE nullif(btrim(p.sku),'') IS NOT NULL
            UNION SELECT p.id,p.model,p.name,m.sku FROM scoped p JOIN operating_product_mappings m ON m.product_id=p.id
            JOIN operating_connections c ON c.id=m.connection_id WHERE c.account_key=%s AND c.marketplace=%s AND c.currency=%s'''
        args.extend((s['account_ref'],s['site'],s['currency']))
    sql='''WITH scoped AS (SELECT p.id,p.model,p.name,p.sku FROM products p WHERE upper(p.marketplace)=%s
           AND upper(p.currency)=%s AND (p.data_account_key IS NULL OR p.data_account_key=%s)),
           options AS ('''+projected+') SELECT * FROM options WHERE TRUE'
    if ids:
        sql+=' AND product_id=ANY(%s)'
        args.append(ids)
    if search:
        sql+=" AND (strpos(lower(model),lower(%s))>0 OR strpos(lower(name),lower(%s))>0 OR strpos(lower(coalesce(sku,'')),lower(%s))>0)"
        args.extend([search]*3)
    sql+=' ORDER BY model,sku,product_id'
    if limit:
        sql+=' LIMIT %s OFFSET %s'
        args.extend((limit,offset))
    return conn.execute(sql,args).fetchall()


def resolve_targets(conn,s,selection):
    available=selection_targets(conn,s,selection.basis,ids=[t.product_id for t in selection.targets])
    allowed={(r['product_id'],r['sku']):r for r in available}
    targets=[]
    for t in selection.targets:
        row=allowed.get((t.product_id,t.sku))
        if row is None:
            raise HTTPException(422,'产品或 SKU 与当前店铺、站点和结算币种不匹配')
        targets.append({**row,'product_id':str(t.product_id),'weight':format(t.weight.normalize(),'f')})
    return targets


def public(event):
    if not event:return None
    return {**event,'expense':money_text(event['expense'],event['currency'])}


def history(conn,s,fee,limit=20,offset=0):
    rows=conn.execute('''SELECT a.*,u.display_name actor_name FROM erp_fee_allocations a
        JOIN app_users u ON u.id=a.actor_id WHERE a.entry_id=%s ORDER BY a.version DESC LIMIT %s OFFSET %s''',
        (fee['id'],limit+1,offset)).fetchall()
    last=last_event(conn,fee['id'])
    valid=last and last['action']=='post' and not fee['reversed'] and last['payout_date']==s['payout_date']
    return {'entry_ref':fee['entry_ref'],'currency':s['currency'],'payout_date':s['payout_date'],
            'source_amount':money_text(fee['amount'],s['currency']),'expense':money_text(-fee['amount'],s['currency']),
            'source_reversed':fee['reversed'],'version':last['version'] if last else 0,
            'active':public(last) if valid else None,'history':page([public(r) for r in rows],limit,offset)}


@router.get('/{statement_id}/allocation-targets')
def get_targets(statement_id:UUID,basis:Literal['product','sku'],search:str=Query('',max_length=100),
                offset:int=Query(0,ge=0),limit:int=Query(40,ge=1,le=100),actor=Depends(require_user)):
    with connection() as conn:
        s=statement(conn,statement_id,actor)
        return page(selection_targets(conn,s,basis,search=search,limit=limit+1,offset=offset),limit,offset)


BASE='/{statement_id}/entries/{entry_id}/allocations'


@router.get(BASE)
def get_history(statement_id:UUID,entry_id:UUID,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100),
                actor=Depends(require_user)):
    with connection() as conn:
        s,fee=expense_line(conn,statement_id,entry_id,actor)
        return history(conn,s,fee,limit,offset)


@router.post(BASE+'/preview')
def preview(statement_id:UUID,entry_id:UUID,body:Selection,actor=Depends(require_user)):
    with connection() as conn:
        s,fee=expense_line(conn,statement_id,entry_id,actor)
        last=last_event(conn,entry_id)
        if fee['reversed'] or last and last['action']=='post':
            raise HTTPException(409,'原费用已冲正或仍有生效分摊，请先核实原凭证及分摊记录')
        rows=distribute(-fee['amount'],s['currency'],resolve_targets(conn,s,body))
        result=conn.execute('''INSERT INTO erp_fee_previews(entry_id,actor_id,statement_version,base_version,payout_date,
            currency,source_amount,basis,reason,targets) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (entry_id,actor['id'],s['version'],last['version'] if last else 0,s['payout_date'],s['currency'],
             fee['amount'],body.basis,body.reason,Jsonb(rows))).fetchone()
        return {'preview_id':result['id'],'currency':s['currency'],'payout_date':s['payout_date'],
                'expense':money_text(-fee['amount'],s['currency']),
                'allocated':money_text(sum(Decimal(r['expense']) for r in rows),s['currency']),'targets':rows}


@router.post(BASE+'/post')
def post(statement_id:UUID,entry_id:UUID,body:Post,actor=Depends(require_user)):
    with connection() as conn:
        s,fee=expense_line(conn,statement_id,entry_id,actor)
        draft=conn.execute('SELECT * FROM erp_fee_previews WHERE entry_id=%s AND id=%s AND actor_id=%s',
                           (entry_id,body.preview_id,actor['id'])).fetchone()
        if not draft:
            raise HTTPException(404,'请先由当前账户对本费用行生成预览')
        prior=conn.execute('SELECT action,preview_id FROM erp_fee_allocations WHERE entry_id=%s AND request_id=%s',
                            (entry_id,body.request_id)).fetchone()
        if prior:
            if prior['preview_id']!=body.preview_id or prior['action']!='post':
                raise HTTPException(409,'此请求编号已用于其他分摊动作')
            return history(conn,s,fee)
        if conn.execute('SELECT id FROM erp_fee_allocations WHERE preview_id=%s',(body.preview_id,)).fetchone():
            return history(conn,s,fee)
        last=last_event(conn,entry_id)
        if (fee['reversed'] or draft['source_amount']!=fee['amount'] or draft['statement_version']!=s['version']
                or draft['payout_date']!=s['payout_date'] or draft['base_version']!=(last['version'] if last else 0)):
            raise HTTPException(409,'原凭证或分摊版本已改变，请重新预览')
        checked=Selection(basis=draft['basis'],reason=draft['reason'],targets=[
            {k:r[k] for k in ('product_id','sku','weight')} for r in draft['targets']])
        current=distribute(-fee['amount'],s['currency'],resolve_targets(conn,s,checked))
        if current!=draft['targets']:
            raise HTTPException(409,'分摊对象已改变，请重新预览')
        conn.execute('''INSERT INTO erp_fee_allocations(entry_id,version,action,preview_id,actor_id,request_id,
            payout_date,currency,expense,basis,reason,targets) VALUES(%s,%s,'post',%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (entry_id,draft['base_version']+1,body.preview_id,actor['id'],body.request_id,s['payout_date'],
             s['currency'],-fee['amount'],draft['basis'],draft['reason'],Jsonb(current)))
        return history(conn,s,fee)


@router.post(BASE+'/void')
def void(statement_id:UUID,entry_id:UUID,body:Void,actor=Depends(require_user)):
    with connection() as conn:
        s,fee=expense_line(conn,statement_id,entry_id,actor)
        prior=conn.execute('SELECT actor_id,version,action,reason FROM erp_fee_allocations WHERE entry_id=%s AND request_id=%s',
                           (entry_id,body.request_id)).fetchone()
        if prior:
            if (prior['actor_id']!=actor['id'] or prior['action']!='void' or prior['reason']!=body.reason
                    or prior['version']!=body.expected_version+1):
                raise HTTPException(409,'此请求编号已用于其他分摊动作')
            return history(conn,s,fee)
        last=last_event(conn,entry_id)
        if not last or last['action']!='post' or last['version']!=body.expected_version:
            raise HTTPException(409,'没有该版本的生效分摊，请重新读取')
        reverse_active(conn,entry_id,actor,body.reason,body.request_id)
        return history(conn,s,fee)


@router.get('/{statement_id}/allocation-summary')
def summary(statement_id:UUID,offset:int=Query(0,ge=0),limit:int=Query(40,ge=1,le=100),actor=Depends(require_user)):
    with connection() as conn:
        s=statement(conn,statement_id,actor)
        rows=conn.execute('''SELECT target->>'product_id' product_id,max(target->>'model') model,target->>'sku' sku,
            sum((target->>'expense')::numeric) expense FROM erp_fee_allocations a JOIN erp_settlement_entries e
            ON e.id=a.entry_id CROSS JOIN LATERAL jsonb_array_elements(a.targets) target
            WHERE e.statement_id=%s AND a.action='post' AND a.payout_date=%s
            AND NOT EXISTS (SELECT 1 FROM erp_fee_allocations v WHERE v.reverses_id=a.id)
            AND NOT EXISTS (SELECT 1 FROM erp_settlement_entries r WHERE r.reverses_id=e.id)
            GROUP BY target->>'product_id',target->>'sku' ORDER BY product_id,sku LIMIT %s OFFSET %s''',
            (statement_id,s['payout_date'],limit+1,offset)).fetchall()
        for row in rows:row['expense']=money_text(row['expense'],s['currency'])
        return {'currency':s['currency'],'payout_date':s['payout_date'],**page(rows,limit,offset)}
