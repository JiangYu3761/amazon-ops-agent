"""Stock positions and evidence-backed good-stock transfers between local warehouses."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .db import connection
from .erp_receiving import _scope
from .stock_ledger import check_postings
from .workflow_auth import require_user

router = APIRouter(prefix='/api/erp-stock', tags=['Local warehouse stock'])
Ref = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=140)]


class Transfer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    from_scope_id: UUID
    to_scope_id: UUID
    sku: Ref
    qty: int = Field(strict=True, gt=0, le=1_000_000)
    reference: Ref
    evidence: str = Field(min_length=1, max_length=1000)
    request_id: UUID

    @field_validator('sku', 'reference', 'evidence')
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or '\x00' in value:
            raise ValueError('请提供有效的 SKU、调拨编号和处理凭证')
        return value.strip()


DETAIL = '''SELECT t.*,f.warehouse AS from_warehouse,d.warehouse AS to_warehouse,u.display_name AS actor_name
            FROM erp_stock_transfers t JOIN erp_receiving_scopes f ON f.id=t.from_scope_id
            JOIN erp_receiving_scopes d ON d.id=t.to_scope_id JOIN app_users u ON u.id=t.actor_id'''


def page(rows, offset, limit):
    return {'items': rows[:limit], 'has_more': len(rows) > limit,
            'next_offset': offset + limit if len(rows) > limit else None}


@router.get('/balances')
def balances(scope_id: UUID, sku: str = Query('', max_length=140), offset: int = Query(0, ge=0),
             limit: int = Query(50, ge=1, le=250), actor=Depends(require_user)):
    if '\x00' in sku:
        raise HTTPException(422,'无效的 SKU')
    with connection() as conn:
        _scope(conn, scope_id, actor, lock=True)
        query = '''SELECT scope_id,sku,sum(pending_delta) AS pending,sum(good_delta) AS good,sum(bad_delta) AS bad
                   FROM erp_stock_postings WHERE scope_id=%s'''
        params = [scope_id]
        if sku.strip():
            query += ' AND sku=%s'
            params.append(sku.strip())
        rows = conn.execute(query+' GROUP BY scope_id,sku ORDER BY sku LIMIT %s OFFSET %s',
                            (*params,limit+1,offset)).fetchall()
        return page(rows, offset, limit)


@router.get('/transfers')
def transfers(scope_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=250),
              actor=Depends(require_user)):
    with connection() as conn:
        _scope(conn, scope_id, actor, lock=True)
        rows = conn.execute(DETAIL + ''' WHERE (t.from_scope_id=%s OR t.to_scope_id=%s)
            AND (%s OR EXISTS (SELECT 1 FROM erp_receiving_members m WHERE m.scope_id=t.from_scope_id AND m.user_id=%s)
                    AND EXISTS (SELECT 1 FROM erp_receiving_members m WHERE m.scope_id=t.to_scope_id AND m.user_id=%s))
            ORDER BY t.created_at DESC,t.id DESC LIMIT %s OFFSET %s''',
                            (scope_id,scope_id,actor['is_admin'],actor['id'],actor['id'],limit+1,offset)).fetchall()
        return page(rows, offset, limit)


@router.post('/transfers', status_code=201)
def transfer(body: Transfer, actor=Depends(require_user)):
    if body.from_scope_id == body.to_scope_id:
        raise HTTPException(422, '调出仓与调入仓必须不同')
    with connection() as conn:
        scopes = {s: _scope(conn, s, actor, lock=True) for s in sorted([body.from_scope_id,body.to_scope_id])}
        source, dest = scopes[body.from_scope_id], scopes[body.to_scope_id]
        if (source['account_ref'], source['site']) != (dest['account_ref'], dest['site']):
            raise HTTPException(422, '仅可在同店铺、同站点的不同仓库调拨良品')
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                     (f"stock-request:{actor['id']}:{body.request_id}",))
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                     (f"stock-ref:{source['account_ref']}:{source['site']}:{body.reference}",))
        existing = conn.execute(DETAIL+''' JOIN erp_stock_requests r ON r.transfer_id=t.id
                                WHERE r.actor_id=%s AND r.request_id=%s''',
                                (actor['id'],body.request_id)).fetchone()
        if existing is None:
            existing = conn.execute(DETAIL+' WHERE t.account_ref=%s AND t.site=%s AND t.reference=%s',
                                    (source['account_ref'],source['site'],body.reference)).fetchone()
        if existing is not None:
            for original_scope in (existing['from_scope_id'], existing['to_scope_id']):
                if original_scope not in scopes:
                    _scope(conn, original_scope, actor)
            keys = ('from_scope_id','to_scope_id','sku','qty','reference','evidence')
            if any(existing[k] != getattr(body,k) for k in keys):
                raise HTTPException(409, '相同调拨编号或请求标识已用于不同的明细或凭证')
            record = existing
        else:
            check_postings(conn, {(body.from_scope_id,body.sku):(0,-body.qty,0),
                                  (body.to_scope_id,body.sku):(0,body.qty,0)})
            assigned = conn.execute('''INSERT INTO erp_stock_transfers
                (from_scope_id,to_scope_id,account_ref,site,sku,qty,reference,evidence,actor_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (body.from_scope_id,body.to_scope_id,source['account_ref'],source['site'],body.sku,
                 body.qty,body.reference,body.evidence,actor['id'])).fetchone()
            record = conn.execute(DETAIL+' WHERE t.id=%s',(assigned['id'],)).fetchone()
        conn.execute('''INSERT INTO erp_stock_requests(actor_id,request_id,transfer_id) VALUES (%s,%s,%s)
                        ON CONFLICT DO NOTHING''',(actor['id'],body.request_id,record['id']))
        return record
