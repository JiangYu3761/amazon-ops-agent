"""Single-workspace, operator-initiated exports of submitted workbench items."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from . import feishu_transport as remote
from .db import connection
from .workbench_definitions import page_definition
from .workflow_auth import require_user

router=APIRouter(prefix='/api/workbench/feishu', tags=['Feishu master table'])
LABELS={'draft':'草稿','pending_review':'待审核','approved':'待执行','executed':'待复核','closed':'已归档'}


def selected(conn, item_id, actor, lock=False):
    sql = '''SELECT w.id,w.title,w.page_id,w.status,w.version,u.display_name AS owner,p.model
        FROM workbench_items w JOIN app_users u ON w.owner_id=u.id
        LEFT JOIN products p ON p.id=w.product_id WHERE w.id=%s AND
        (%s OR w.owner_id=%s OR w.reviewer_id=%s)'''
    item=conn.execute(sql+(' FOR UPDATE OF w' if lock else ''),(item_id,actor['is_admin'],actor['id'],actor['id'])).fetchone()
    if not item:
        raise HTTPException(404,'未找到可操作的工作单')
    return item


def receipt(conn, target, item):
    if target:
        return conn.execute('SELECT * FROM workbench_feishu_sync WHERE destination=%s AND item_id=%s',
                            (target.identity,item['id'])).fetchone()


def describe(target, item, stored):
    state = 'not_synced'
    message = '此单尚未同步到飞书总表。'
    stored = stored or {}
    if not target:
        state, message = 'unconfigured', '请管理员配置飞书应用和目标总表。'
    elif stored.get('last_error'):
        state, message = 'failed', stored['last_error']
    elif stored.get('confirmed_version')==item['version']:
        state, message = 'synced', '已从飞书回读并核对记录。'
    elif stored.get('confirmed_version'):
        state, message = 'stale', '工作单已有新版，请同步后核对。'
    if item['status']=='draft':
        message = '草稿保留在工作台，提交后可同步。'
    link=target.url if target else None
    return {'configured':bool(target),'status':state,'message':message,'version':stored.get('confirmed_version'),
            'last_synced_at':stored.get('confirmed_at'),'table_url':link,
            'record_url':link+'&record='+stored['record_id'] if link and stored.get('record_id') else None,
            'can_sync':bool(target) and item['status']!='draft'}


@router.get('')
def master_table(actor=Depends(require_user)):
    target=remote.configured_target()
    return {'configured':bool(target),'table_url':target.url if target else None,'fields':list(remote.FIELDS)}


@router.get('/{item_id}')
def status(item_id: UUID, actor=Depends(require_user)):
    target=remote.configured_target()
    with connection() as conn:
        item=selected(conn,item_id,actor)
        return describe(target,item,receipt(conn,target,item))


@router.post('/{item_id}/sync')
def sync(item_id: UUID, actor=Depends(require_user)):
    target=remote.configured_target()
    with connection() as conn:
        item=selected(conn,item_id,actor,lock=True)
        if item['status']=='draft':
            raise HTTPException(409,'请先提交工作单，再同步到飞书。')
        if not target:
            raise HTTPException(409,'管理员尚未配置有效的飞书应用和总表。')
        stored=receipt(conn,target,item) or {}
        fields={'本地ID':'homa:workbench:'+str(item_id),'类型':'HOMA 工作单','事项':item['title'],
                '产品':item['model'] or '', '负责人':item['owner'],'状态':LABELS[item['status']],
                '板块':page_definition(item['page_id'])['title']}
        def verify_association(record_id):
            conflict = conn.execute('''SELECT 1 FROM workbench_feishu_sync
                WHERE destination=%s AND record_id=%s AND item_id<>%s''',
                (target.identity,record_id,item_id)).fetchone()
            if conflict:
                raise remote.RemoteError('飞书记录已关联另一张工作单，请核对总表的本地ID。')
        try:
            record=remote.synchronize(target,fields,stored.get('record_id'),verify_association)
            conn.execute('''INSERT INTO workbench_feishu_sync(item_id,destination,record_id,confirmed_version,confirmed_at,last_actor)
                VALUES(%s,%s,%s,%s,now(),%s) ON CONFLICT(item_id,destination) DO UPDATE SET
                record_id=EXCLUDED.record_id,confirmed_version=EXCLUDED.confirmed_version,confirmed_at=now(),
                last_error=NULL,last_actor=EXCLUDED.last_actor,last_attempt_at=now()''',
                (item_id,target.identity,record,item['version'],actor['id']))
        except remote.RemoteError as error:
            conn.execute('''INSERT INTO workbench_feishu_sync(item_id,destination,last_error,last_actor)
                VALUES(%s,%s,%s,%s) ON CONFLICT(item_id,destination) DO UPDATE SET
                last_error=EXCLUDED.last_error,last_actor=EXCLUDED.last_actor,last_attempt_at=now()''',
                (item_id,target.identity,str(error),actor['id']))
        return describe(target,item,receipt(conn,target,item))
