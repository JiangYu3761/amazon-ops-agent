"""Authenticated workspace business records, independent of linked SOP approval state."""
from datetime import date
import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from .db import connection
from .workbench_definitions import page_definition, section_definition, validate_values
from .workbench_rules import evaluate_checks
from .workbench_scope import resolve_shop_scope, shop_choices
from .workflow_auth import require_user

router = APIRouter(prefix='/api/workbench', tags=['Professional workbench'])


class NewItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    product_id: UUID | None = None
    account_ref: str | None = Field(default=None, max_length=120)
    business_timezone: str | None = Field(default=None, max_length=100)
    connection_id: UUID | None = None
    page_id: str = Field(min_length=1, max_length=100)
    section_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default='', max_length=240)
    site: str = Field(default='', max_length=100)
    object_ref: str = Field(default='', max_length=500)
    values: dict[str, Any] = Field(default_factory=dict, max_length=100)
    focus_date: date | None = None
    reviewer_id: UUID | None = None
    task_id: UUID | None = None
    request_id: UUID
    signal_key: str | None = Field(default=None, max_length=200)
    parent_id: UUID | None = None
    source_note: str = Field(default='', max_length=6000)


class Change(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=1)
    request_id: UUID | None = None


class EditItem(Change):
    title: str | None = Field(default=None, max_length=240)
    site: str | None = Field(default=None, max_length=100)
    object_ref: str | None = Field(default=None, max_length=500)
    values: dict[str, Any] | None = Field(default=None, max_length=100)
    focus_date: date | None = None
    reviewer_id: UUID | None = None
    task_id: UUID | None = None


class ItemAction(Change):
    action: Literal['submit', 'approve', 'return', 'execute', 'verify']
    note: str = Field(default='', max_length=4000)
    evidence: str = Field(default='', max_length=4000)


def _item(conn, item_id, lock=False):
    row = conn.execute('SELECT * FROM workbench_items WHERE id=%s' +
                       (' FOR UPDATE' if lock else ''), (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, '业务单不存在')
    return row


# A product keeps its own scope. A shop is identified by its seller account and marketplace.
_SCOPE = '''product_id IS NOT DISTINCT FROM %s AND account_ref IS NOT DISTINCT FROM %s
            AND (product_id IS NOT NULL OR site=%s)'''


def _detail(conn, row):
    history = conn.execute('''SELECT e.version,e.action,e.actor_id,u.display_name actor_name,
            e.note,e.evidence,e.snapshot,e.created_at
        FROM workbench_events e JOIN app_users u ON u.id=e.actor_id
        WHERE e.item_id=%s AND e.version<=%s ORDER BY e.version''', (row['id'], row['version'])).fetchall()
    scope = (row['product_id'], row['account_ref'], row['site'])
    reference = '''SELECT id,number,page_id,section_id,title,status,object_ref,product_id,
                          owner_id,created_at,account_ref,site,business_timezone FROM workbench_items WHERE ''' + _SCOPE
    parent = None
    if row['parent_id']:
        parent = conn.execute(reference + ' AND id=%s', (*scope, row['parent_id'])).fetchone()
    children = conn.execute(reference + ' AND parent_id=%s ORDER BY created_at,id',
                            (*scope, row['id'])).fetchall()
    return {**row, 'history': history, 'parent_ref': parent, 'child_refs': children}


def _product(conn, product_id):
    product = conn.execute('SELECT id,marketplace,business_timezone FROM products WHERE id=%s', (product_id,)).fetchone()
    if not product:
        raise HTTPException(404, '产品不存在')
    return product


def _site(product, site):
    market = product['marketplace'].strip()
    if site and site.strip().casefold() != market.casefold():
        raise HTTPException(422, '业务单站点与所选产品的站点不一致')
    return market


def _reviewer(conn, actor, owner_id, reviewer_id):
    if not actor['is_admin']:
        raise HTTPException(403, '请联系流程管理员分配审核人')
    if reviewer_id is None:
        return
    if reviewer_id == owner_id:
        raise HTTPException(422, '经办人与审核人需由不同账户担任')
    if not conn.execute('''SELECT 1 FROM workflow_accounts a JOIN app_users u ON a.user_id=u.id
            WHERE a.user_id=%s AND u.is_active''', (reviewer_id,)).fetchone():
        raise HTTPException(422, '审核人须为有效的流程协作账户')


def _task(conn, product_id, task_id):
    if task_id and not product_id:
        raise HTTPException(422, '店铺级业务单不能关联产品 SOP')
    if task_id and not conn.execute('''SELECT 1 FROM tasks t JOIN product_workflows w
           ON w.id=t.product_workflow_id WHERE t.id=%s AND w.product_id=%s''',
           (task_id, product_id)).fetchone():
        raise HTTPException(422, '请选择同一产品的 SOP 任务')


def _hash(operation, item_id, body):
    content = [operation, str(item_id or ''), body.model_dump(mode='json', exclude_unset=True)]
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _replay(conn, actor, body, fingerprint, item_id=None):
    # Lock the receipt key before the item row on every mutation to serialize concurrent retries.
    if body.request_id:
        key = str(actor['id']) + ':' + str(body.request_id)
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (key,))
        prior = conn.execute('SELECT item_id,request_hash FROM workbench_request_receipts WHERE actor_id=%s AND request_id=%s',
                             (actor['id'], body.request_id)).fetchone()
        if prior:
            if prior['request_hash'] != fingerprint:
                raise HTTPException(409, '同一请求编号已用于其他业务操作')
            return _detail(conn, _item(conn, prior['item_id']))
    if item_id:
        _item(conn, item_id, lock=True)
        # Older clients can retry an identical action using its original expected version.
        prior = conn.execute('''SELECT item_id FROM workbench_events WHERE item_id=%s AND version=%s
                AND actor_id=%s AND request_hash=%s''',
                (item_id, body.expected_version + 1, actor['id'], fingerprint)).fetchone()
        if prior:
            return _detail(conn, _item(conn, item_id))
    return None


def _receipt(conn, item_id, actor, body, fingerprint):
    if body.request_id:
        conn.execute('INSERT INTO workbench_request_receipts(actor_id,request_id,item_id,request_hash) VALUES(%s,%s,%s,%s)',
                     (actor['id'], body.request_id, item_id, fingerprint))


def _event(conn, row, actor, action, body, fingerprint, note='', evidence=''):
    snapshot = {key: row[key] for key in ('status', 'values', 'title', 'site', 'object_ref',
                                           'owner_id', 'reviewer_id', 'task_id', 'parent_id', 'source_note',
                                           'product_id', 'account_ref', 'business_timezone', 'connection_id', 'focus_date')}
    conn.execute('''INSERT INTO workbench_events
        (item_id,version,actor_id,action,note,evidence,snapshot,request_id,request_hash)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (row['id'], row['version'], actor['id'], action, note, evidence,
         Jsonb(jsonable_encoder(snapshot)), body.request_id, fingerprint))
    _receipt(conn, row['id'], actor, body, fingerprint)
    return _detail(conn, row)


def _version(row, body):
    if row['version'] != body.expected_version:
        raise HTTPException(409, '这张业务单已更新，请重新读取最新版本再保存')


@router.get('/shop-scopes')
def list_shop_scopes(actor=Depends(require_user)):
    with connection() as conn:
        return {'scopes': shop_choices(conn)}


@router.get('/items')
def list_items(product_id: UUID | None = None, page_id: str | None = None, section_id: str | None = None,
               scope: Literal['shop','product'] | None = None, account_ref: str | None = None, site: str | None = None,
               limit: int = Query(100, ge=1, le=250), offset: int = Query(0, ge=0),
               actor=Depends(require_user)):
    # All authenticated members belong to the same workspace, matching the existing product model.
    filters, params = [], []
    if (product_id or scope == 'product') and (scope == 'shop' or account_ref is not None):
        raise HTTPException(422, '请分别选择产品范围或店铺范围')
    if scope:
        filters.append('product_id IS NULL' if scope == 'shop' else 'product_id IS NOT NULL')
    if page_id:
        page_definition(page_id)
        if section_id:
            section_definition(page_id, section_id)
    for name, value in (('product_id', product_id), ('page_id', page_id), ('section_id', section_id),
                        ('account_ref', account_ref), ('site', site)):
        if value is not None:
            filters.append(name + '=%s')
            params.append(value)
    where = ' WHERE ' + ' AND '.join(filters) if filters else ''
    with connection() as conn:
        if product_id:
            _product(conn, product_id)
        rows = conn.execute('''SELECT id,number,product_id,page_id,section_id,title,site,object_ref,signal_key,parent_id,
                account_ref,business_timezone,connection_id,owner_id,reviewer_id,task_id,focus_date,values,status,version,created_at,updated_at
                FROM workbench_items''' + where + ' ORDER BY updated_at DESC,id DESC LIMIT %s OFFSET %s',
                (*params, limit + 1, offset)).fetchall()
    return {'items': rows[:limit], 'has_more': len(rows) > limit,
            'next_offset': offset + limit if len(rows) > limit else None}


@router.get('/focus')
def calendar_focus(start_date: date, end_date: date, product_id: UUID | None = None, actor=Depends(require_user)):
    if not 0 <= (end_date - start_date).days <= 62:
        raise HTTPException(422, '请选择不超过 63 天的日历范围')
    # Workbench items are already shared with signed-in members of this workspace.
    # Read the source items directly so rescheduling cannot create a second calendar event.
    with connection() as conn:
        rows = conn.execute('''SELECT id,number,title,page_id,section_id,product_id,account_ref,
                site,owner_id,status,focus_date FROM workbench_items
            WHERE focus_date BETWEEN %s AND %s AND (%s::uuid IS NULL OR product_id=%s)
            ORDER BY focus_date,number''', (start_date, end_date, product_id, product_id)).fetchall()
    return {'items': rows}


@router.get('/items/{item_id}')
def get_item(item_id: UUID, actor=Depends(require_user)):
    with connection() as conn:
        return _detail(conn, _item(conn, item_id))


@router.post('/items', status_code=201)
def create_item(body: NewItem, actor=Depends(require_user)):
    fingerprint = _hash('create', None, body)
    with connection() as conn:
        replay = _replay(conn, actor, body, fingerprint)
        if replay:
            return replay
        section = section_definition(body.page_id, body.section_id)
        if body.product_id:
            if body.account_ref or body.connection_id or body.business_timezone:
                raise HTTPException(422, '产品单的站点和业务时区取自产品主档')
            product = _product(conn, body.product_id)
            account_ref, site, zone = None, _site(product, body.site), product['business_timezone']
        else:
            if body.page_id != 'shop':
                raise HTTPException(422, '只有店铺与账户页面可以按店铺办理')
            account_ref, site, zone = resolve_shop_scope(conn, body.account_ref, body.site, body.business_timezone,
                                                        body.connection_id, register_by=actor['id'])
        item_scope = (body.product_id, account_ref, site)
        validate_values(section['fields'], body.values)
        if body.reviewer_id:
            _reviewer(conn, actor, actor['id'], body.reviewer_id)
        _task(conn, body.product_id, body.task_id)
        signal_key = (body.signal_key or '').strip() or None
        object_ref = body.object_ref.strip()
        if body.parent_id:
            if not object_ref:
                raise HTTPException(422, '后续工作需填写具体业务对象')
            if not conn.execute('SELECT id FROM workbench_items WHERE id=%s AND ' + _SCOPE,
                                (body.parent_id, *item_scope)).fetchone():
                raise HTTPException(422, '来源业务单须属于同一产品或同一店铺站点')
        if signal_key:
            if not object_ref:
                raise HTTPException(422, '来源线索须绑定具体业务对象')
            scope = (*item_scope, signal_key, body.page_id, body.section_id, object_ref)
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))',
                         (json.dumps(jsonable_encoder(scope)),))
            prior = conn.execute('SELECT * FROM workbench_items WHERE ' + _SCOPE + ''' AND signal_key=%s
                AND page_id=%s AND section_id=%s AND object_ref=%s AND status<>'closed' FOR UPDATE''', scope).fetchone()
            if prior:
                if body.parent_id and body.parent_id != prior['parent_id']:
                    raise HTTPException(409, '此线索已关联其他来源业务单')
                _receipt(conn, prior['id'], actor, body, fingerprint)
                return _detail(conn, prior)
        if body.parent_id:
            scope = (body.parent_id, body.page_id, body.section_id, object_ref)
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,2))',
                         (json.dumps(jsonable_encoder(scope)),))
            prior = conn.execute('''SELECT * FROM workbench_items WHERE parent_id=%s AND page_id=%s
                AND section_id=%s AND object_ref=%s AND status<>'closed' FOR UPDATE''', scope).fetchone()
            if prior:
                if signal_key and prior['signal_key'] != signal_key:
                    raise HTTPException(409, '同一工作已有未结后续单，关联的线索不同')
                _receipt(conn, prior['id'], actor, body, fingerprint)
                return _detail(conn, prior)
        row = conn.execute('''INSERT INTO workbench_items(product_id,page_id,section_id,title,site,object_ref,
                owner_id,reviewer_id,task_id,values,definition,signal_key,parent_id,source_note,
                account_ref,business_timezone,connection_id,focus_date)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *''',
            (body.product_id, body.page_id, body.section_id, body.title.strip(), site,
             body.object_ref.strip(), actor['id'], body.reviewer_id, body.task_id, Jsonb(body.values),
             Jsonb(section), signal_key, body.parent_id, body.source_note, account_ref, zone, body.connection_id, body.focus_date)).fetchone()
        if not row:
            raise HTTPException(409, '此对象已有关联业务单，请刷新后核对最新记录')
        return _event(conn, row, actor, 'create', body, fingerprint)


@router.patch('/items/{item_id}')
def patch_item(item_id: UUID, body: EditItem, actor=Depends(require_user)):
    fingerprint = _hash('save', item_id, body)
    with connection() as conn:
        replay = _replay(conn, actor, body, fingerprint, item_id)
        if replay:
            return replay
        row = _item(conn, item_id, lock=True)
        _version(row, body)
        if row['status'] != 'draft':
            raise HTTPException(409, '仅草稿可编辑；待审业务单由审核人退回后可以修改')
        changes = body.model_dump(exclude_unset=True, exclude={'expected_version','request_id'})
        if not changes:
            return _detail(conn, row)
        if changes.get('reviewer_id', row['reviewer_id']) != row['reviewer_id']:
            _reviewer(conn, actor, row['owner_id'], changes['reviewer_id'])
        if actor['id'] != row['owner_id']:
            if not actor['is_admin'] or set(changes) != {'reviewer_id'}:
                raise HTTPException(403, '业务内容只能由经办人填写，管理员可单独分配审核人')
        if any(value is None and key not in ('reviewer_id','task_id','focus_date') for key,value in changes.items()):
            raise HTTPException(422, '请用空文本或空表单清空相应字段')
        if 'task_id' in changes:
            _task(conn, row['product_id'], changes['task_id'])
        if 'site' in changes:
            if row['product_id']:
                changes['site'] = _site(_product(conn, row['product_id']), changes['site'])
            elif changes['site'].strip().upper() == row['site']:
                changes['site'] = row['site']
            else:
                raise HTTPException(422, '店铺站点已固定，请在原店铺范围办理')
        if 'values' in changes:
            validate_values(row['definition']['fields'], changes['values'])
            changes['values'] = Jsonb(changes['values'])
        for key in ('title','object_ref'):
            if key in changes:
                changes[key] = changes[key].strip()
        # Identifiers only come from EditItem's explicitly enumerated fields.
        setters = ','.join(key + '=%s' for key in changes)
        if row['signal_key'] and changes.get('object_ref', row['object_ref']) != row['object_ref']:
            raise HTTPException(422, '来源线索的业务对象已固定，请在原对象上办理')
        if row['parent_id'] and 'object_ref' in changes and not changes['object_ref']:
            raise HTTPException(422, '后续工作需填写具体业务对象')
        try:
            row = conn.execute('UPDATE workbench_items SET ' + setters +
                ',version=version+1,updated_at=now() WHERE id=%s RETURNING *', (*changes.values(), item_id)).fetchone()
        except UniqueViolation as error:
            if error.diag.constraint_name != 'workbench_open_followup':
                raise
            raise HTTPException(409, '同一来源和对象已有未结后续单') from error
        return _event(conn, row, actor, 'save', body, fingerprint)


TRANSITIONS = {
    'submit': ('draft', 'pending_review', 'owner_id'),
    'approve': ('pending_review', 'approved', 'reviewer_id'),
    'return': ('pending_review', 'draft', 'reviewer_id'),
    'execute': ('approved', 'executed', 'owner_id'),
    'verify': ('executed', 'closed', 'reviewer_id'),
}


@router.post('/items/{item_id}/actions')
def act(item_id: UUID, body: ItemAction, actor=Depends(require_user)):
    fingerprint = _hash('action', item_id, body)
    with connection() as conn:
        replay = _replay(conn, actor, body, fingerprint, item_id)
        if replay:
            return replay
        row = _item(conn, item_id, lock=True)
        _version(row, body)
        old, new, role = TRANSITIONS[body.action]
        if actor['id'] != row[role]:
            raise HTTPException(403, '此动作须由本单指定的经办人或独立审核人执行')
        if row['status'] != old:
            raise HTTPException(409, '当前状态不能执行此动作')
        if body.action == 'submit':
            if not row['reviewer_id']:
                raise HTTPException(422, '请先联系管理员分配独立审核人')
            if not all(row[key].strip() for key in ('site','object_ref','title')):
                raise HTTPException(422, '提交前须填写站点、业务对象和标题')
            validate_values(row['definition']['fields'], row['values'], submitting=True)
            for finding in evaluate_checks(row['definition'], row['values']):
                if finding['level'] == 'block':
                    raise HTTPException(422, finding['detail'])
        note, evidence = body.note.strip(), body.evidence.strip()
        if body.action in ('return','verify') and not note:
            raise HTTPException(422, '请填写退回原因或复核结论')
        if body.action == 'execute' and not evidence:
            raise HTTPException(422, '登记执行须填写平台回执、凭证链接或可核实的来源')
        row = conn.execute('''UPDATE workbench_items SET status=%s,version=version+1,updated_at=now()
                             WHERE id=%s RETURNING *''', (new, item_id)).fetchone()
        return _event(conn, row, actor, body.action, body, fingerprint, note, evidence)
