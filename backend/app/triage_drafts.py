"""Private operator notes. Concurrent edits fork so that both originals remain readable."""
import hashlib
import json
import re
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .db import connection
from .workflow_auth import require_user

router = APIRouter(prefix='/api/workbench/triage-drafts', tags=['Operator notes'])


class DraftWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    actor_id: UUID
    product_id: UUID | None = None
    account_ref: str = Field(default='', max_length=120)
    site: str = Field(default='', max_length=4)
    draft_id: UUID
    request_id: UUID
    expected_version: int = Field(ge=0)
    note: str = Field(min_length=1, max_length=6000)
    action: Literal['save', 'archive'] = 'save'


def scope_key(actor, actor_id, product_id, account_ref, site):
    if actor['id'] != actor_id:
        raise HTTPException(403, '登录身份已切换。原文保留在原账户，请重新登录后同步。')
    account, market = account_ref.strip(), site.strip().upper()
    if bool(account) != bool(market) or (account and (not account.isprintable() or not re.fullmatch('[A-Z]{2,4}', market))):
        raise HTTPException(422, '请同时填写店铺标识与 2 至 4 位站点代码，或同时留空。')
    # These are private filing labels. They confer no access to store orders or activities.
    return json.dumps([str(product_id) if product_id else None, account, market], ensure_ascii=False)


@router.get('')
def list_drafts(actor_id: UUID, product_id: UUID | None = None,
                account_ref: str = Query(default='', max_length=120), site: str = Query(default='', max_length=4),
                actor=Depends(require_user)):
    scope = scope_key(actor, actor_id, product_id, account_ref, site)
    with connection() as conn:
        items = conn.execute('''SELECT id, note, version, archived, updated_at
            FROM workbench_triage_drafts WHERE owner_id=%s AND scope_key=%s ORDER BY updated_at DESC,id''',
            (actor_id, scope)).fetchall()
        # A second device can discover the user's existing private shop labels without typing them again.
        scopes = conn.execute('''SELECT DISTINCT scope_key FROM workbench_triage_drafts WHERE owner_id=%s
            AND scope_key::jsonb->>0 IS NOT DISTINCT FROM %s::text''',
            (actor_id, str(product_id) if product_id else None)).fetchall()
    return {'items': items, 'shops': [json.loads(row['scope_key'])[1:] for row in scopes if json.loads(row['scope_key'])[1]]}


@router.put('')
def write_draft(body: DraftWrite, actor=Depends(require_user)):
    scope = scope_key(actor, body.actor_id, body.product_id, body.account_ref, body.site)
    if not body.note.strip():
        raise HTTPException(422, '空白内容不能覆盖原文，请核实办理结果后归档。')
    fingerprint = hashlib.sha256(body.model_dump_json(exclude={'request_id'}).encode()).hexdigest()
    owner, draft_id = actor['id'], body.draft_id
    with connection() as conn:
        # Serializes only one user's writes, including simultaneous creation and lost-response retries.
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 29))', (str(owner),))
        prior = conn.execute('''SELECT draft_id AS id, version, branched, fingerprint
            FROM workbench_triage_receipts WHERE owner_id=%s AND request_id=%s''', (owner, body.request_id)).fetchone()
        if prior:
            if prior.pop('fingerprint') != fingerprint:
                raise HTTPException(409, '此保存编号已用于另一份内容，请重新读取。')
            return prior
        if body.product_id and not conn.execute('SELECT 1 FROM products WHERE id=%s', (body.product_id,)).fetchone():
            raise HTTPException(404, '所选产品已不存在，请保留本机原文。')
        row = conn.execute('''SELECT version, note, archived, scope_key FROM workbench_triage_drafts
            WHERE owner_id=%s AND id=%s''', (owner, draft_id)).fetchone()
        if row and row['scope_key'] != scope:
            raise HTTPException(409, '此草稿不属于当前范围。')
        branched = False
        if body.action == 'archive':
            if not row or row['version'] != body.expected_version or row['note'] != body.note or row['archived']:
                raise HTTPException(409, '原文已变化，仍然保留。请重新核对后归档。')
            version = row['version'] + 1
            conn.execute('''UPDATE workbench_triage_drafts SET archived=true,version=%s,updated_at=now()
                WHERE owner_id=%s AND id=%s''', (version, owner, draft_id))
        elif row and not row['archived'] and row['note'] == body.note:
            version = row['version']
        elif row and not row['archived'] and row['version'] == body.expected_version:
            version = row['version'] + 1
            conn.execute('''UPDATE workbench_triage_drafts SET note=%s,version=%s,updated_at=now()
                WHERE owner_id=%s AND id=%s''', (body.note, version, owner, draft_id))
        else:
            if row or body.expected_version:
                draft_id, branched = uuid4(), True
            version = 1
            conn.execute('''INSERT INTO workbench_triage_drafts(owner_id,id,scope_key,note,version)
                VALUES(%s,%s,%s,%s,1)''', (owner, draft_id, scope, body.note))
        conn.execute('''INSERT INTO workbench_triage_receipts(owner_id,request_id,draft_id,fingerprint,version,branched)
            VALUES(%s,%s,%s,%s,%s,%s)''', (owner, body.request_id, draft_id, fingerprint, version, branched))
        return {'id': draft_id, 'version': version, 'branched': branched}
