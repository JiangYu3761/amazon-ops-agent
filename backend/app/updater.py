"""Queue an authenticated administrator's update and expose bounded safe progress."""
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException


STATE_FILE = Path(os.getenv('UPDATE_STATE_FILE', '/tmp/amazon-agent-update.json'))
REQUEST_FILE = Path(os.getenv('UPDATE_REQUEST_FILE', '/tmp/amazon-agent-update.request'))
HEARTBEAT_FILE = Path(os.getenv('UPDATE_HEARTBEAT_FILE', str(REQUEST_FILE.parent / 'update-worker.json')))
LOCK_FILE = Path(os.getenv('UPDATE_LOCK_FILE', str(REQUEST_FILE.parent / 'update.lock')))
REPOSITORY = 'JiangYu3761/amazon-ops-agent'
RUNNING = {'queued','checking','backing_up','updating','restarting','rolling_back','recovering'}
STATUS_DETAILS = {
    'idle':'尚未检查新版本', 'queued':'已提交升级请求，等待服务自动检查 GitHub',
    'checking':'正在检查 GitHub main 分支', 'backing_up':'正在备份业务数据',
    'updating':'正在构建并应用更新', 'restarting':'正在重启服务并检查健康状态',
    'rolling_back':'正在恢复上一版本', 'recovering':'上次升级事务尚未结算，正在等待升级服务完成恢复', 'succeeded':'升级成功',
    'up_to_date':'已经是最新版本', 'failed':'升级失败，请查看部署服务日志',
}
ERROR_DETAILS = {
    'worker_unavailable':'升级服务未连接，请等待服务恢复后重试',
    'worker_interrupted':'升级服务曾中断，本次任务已终止，可在服务恢复后重新检查',
    'request_timeout':'升级请求未被服务接收，本次任务已终止',
    'update_timeout':'升级超过执行时限，本次任务已终止，请检查恢复结果',
    'invalid_state':'升级记录无法读取，请等待服务恢复后重新检查',
    'queue_write_failed':'无法保存升级请求，请检查服务存储状态',
    'fetch_failed':'无法从 GitHub 获取新版本，请检查网络或仓库访问状态',
    'non_fast_forward':'服务器版本与 GitHub 存在分叉，已停止自动替换',
    'dirty_checkout':'服务器程序目录存在未提交修改，已停止自动替换',
    'backup_failed':'业务数据备份失败，已停止升级',
    'build_failed':'新版构建失败，请检查服务恢复结果',
    'migration_failed':'数据库迁移失败，请检查服务恢复结果',
    'health_failed':'新版健康检查失败，请检查服务恢复结果',
    'rollback_failed':'上一版本恢复未完成，请查看部署服务日志',
    'update_failed':'升级失败，请查看部署服务日志',
    'updates_disabled':'当前部署已停用系统升级',
}


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _age(value):
    try:
        timestamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if timestamp.tzinfo is None: timestamp=timestamp.replace(tzinfo=timezone.utc)
        return max(0,(datetime.now(timezone.utc)-timestamp).total_seconds())
    except (ValueError,TypeError):
        return float('inf')


def _seconds(name,default):
    try: return max(1,int(os.getenv(name,str(default))))
    except ValueError: return default


def _read_json(path):
    try:
        value=json.loads(path.read_text('utf-8'))
        return value if isinstance(value,dict) else None
    except (OSError,ValueError):
        return None


def _git(*args):
    try:
        return subprocess.check_output(['git',*args],text=True,timeout=8,stderr=subprocess.DEVNULL).strip()
    except (OSError,subprocess.SubprocessError):
        return None


def _commit(value):
    return value if isinstance(value,str) and re.fullmatch(r'[a-fA-F0-9]{7,64}',value) else None


def _worker_info():
    heartbeat=_read_json(HEARTBEAT_FILE) or {}
    age=_age(heartbeat.get('updated_at'))
    available=bool(heartbeat.get('status')=='ready' and age<=_seconds('UPDATE_HEARTBEAT_MAX_AGE',15)
                   and heartbeat.get('repository')==REPOSITORY and heartbeat.get('branch')=='main')
    return available,heartbeat


def source_fingerprint():
    digest=hashlib.sha256()
    app_dir=Path(__file__).resolve().parent
    try:
        for path in sorted(app_dir.rglob('*.py'),key=lambda p:p.relative_to(app_dir).as_posix()):
            digest.update(('app/'+path.relative_to(app_dir).as_posix()).encode()+b'\0'+path.read_bytes()+b'\0')
        catalog=Path(os.getenv('CATALOG_FILE',str(app_dir.parents[1]/'frontend'/'catalog.json')))
        digest.update(b'catalog.json\0'+catalog.read_bytes()+b'\0')
        return digest.hexdigest()
    except OSError:
        return None


def version_info():
    available,heartbeat=_worker_info()
    configured=os.getenv('UPDATE_ENABLED','true').lower()=='true'
    commit=_commit(os.getenv('APP_COMMIT')) or (_commit(heartbeat.get('deployed_commit')) if available else None)
    return {'version':os.getenv('APP_VERSION','development'),'commit':commit,'branch':'main','repository':REPOSITORY,
            'source_fingerprint':source_fingerprint(),'update_enabled':configured and available,'update_configured':configured,'worker_available':available,
            'availability_reason':'' if configured and available else ERROR_DETAILS['worker_unavailable' if configured else 'updates_disabled']}


def verify_admin_token(token):
    """Legacy local helper; HTTP updates use the existing administrator session."""
    expected=os.getenv('UPDATE_ADMIN_TOKEN','')
    if not expected or not token or not secrets.compare_digest(token,expected):
        raise HTTPException(403,'Administrator authorization required')


def _write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',prefix=path.name+'.',suffix='.tmp',dir=path.parent,delete=False) as out:
            temporary=Path(out.name)
            json.dump(value,out,ensure_ascii=False)
            out.flush();os.fsync(out.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


@contextmanager
def _queue_lock():
    LOCK_FILE.parent.mkdir(parents=True,exist_ok=True)
    with LOCK_FILE.open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise HTTPException(409,{'message':'已有升级任务正在执行','code':'update_busy','job':_public_state(_raw_state())})
        try: yield
        finally: fcntl.flock(lock,fcntl.LOCK_UN)


def _raw_state():
    raw=_read_json(STATE_FILE)
    if raw is not None:return raw
    return {'status':'failed','error_code':'invalid_state'} if STATE_FILE.exists() else {'status':'idle'}


def _public_state(raw):
    """Worker logs, absolute backup paths, actor IDs and raw command errors stay private."""
    info=version_info()
    status=raw.get('status')
    status=status if isinstance(status,str) and status in STATUS_DETAILS else 'failed'
    result={**info,'status':status}
    for key in ('request_id','started_at','updated_at','last_checked_at','finished_at'):
        value=raw.get(key)
        if isinstance(value,str) and len(value)<=100:result[key]=value
    result['current_commit']=_commit(raw.get('current_commit')) or info['commit']
    result['target_commit']=_commit(raw.get('target_commit'))
    code=raw.get('error_code')
    code=code if isinstance(code,str) else None
    if code in ERROR_DETAILS:result['error_code']=code
    result['detail']=ERROR_DETAILS.get(code,STATUS_DETAILS.get(status,STATUS_DETAILS['failed']))
    return result


def _active_file():
    return REQUEST_FILE.parent/'update.active.json'


def _recovery_view(raw):
    # The worker owns recovery snapshots. This is a transient public view only.
    return _public_state({**raw,'status':'recovering','error_code':None})


def _recover_locked(raw):
    """Only unclaimed jobs without a worker recovery transaction may be expired."""
    if _active_file().exists():return raw
    if raw.get('status') not in RUNNING:return raw
    age=_age(raw.get('updated_at') or raw.get('started_at'))
    available,heartbeat=_worker_info()
    code=None
    if raw['status']=='queued' and age>_seconds('UPDATE_QUEUE_TIMEOUT',30):code='request_timeout'
    elif raw['status']!='queued':
        if age>_seconds('UPDATE_JOB_TIMEOUT',1800):code='update_timeout'
        elif age>_seconds('UPDATE_INTERRUPTION_GRACE',30) and (not available or heartbeat.get('active_request_id')!=raw.get('request_id')):
            code='worker_interrupted'
    if code:
        raw={**raw,'status':'failed','error_code':code,'detail':ERROR_DETAILS[code],'updated_at':_timestamp(),'finished_at':_timestamp()}
        _write_json(STATE_FILE,raw)
        queued=_read_json(REQUEST_FILE)
        if queued and queued.get('request_id')==raw.get('request_id'):REQUEST_FILE.unlink(missing_ok=True)
    return raw


def read_state():
    raw=_raw_state()
    if _active_file().exists() or raw.get('status') in RUNNING:
        try:
            with _queue_lock():
                raw=_raw_state()
                if _active_file().exists():return _recovery_view(raw)
                raw=_recover_locked(raw)
        except HTTPException as error:
            if error.status_code!=409:raise
        except OSError:
            raw={**raw,'status':'failed','error_code':'queue_write_failed'}
    return _public_state(raw)


def start_update(actor=None):
    if os.getenv('UPDATE_ENABLED','true').lower()!='true':
        raise HTTPException(409,{'message':ERROR_DETAILS['updates_disabled'],'code':'updates_disabled'})
    try:
        with _queue_lock():
            current=_raw_state()
            if _active_file().exists():
                raise HTTPException(409,{'message':'上次升级事务正在恢复，请等待服务完成恢复','code':'update_recovering','job':_recovery_view(current)})
            current=_recover_locked(current)
            if current.get('status') in RUNNING:
                raise HTTPException(409,{'message':'已有升级任务正在执行','code':'update_busy','job':_public_state(current)})
            available,_=_worker_info()
            if not available:raise HTTPException(503,{'message':ERROR_DETAILS['worker_unavailable'],'code':'worker_unavailable'})
            queued=_read_json(REQUEST_FILE)
            if REQUEST_FILE.exists():
                if queued and _age(queued.get('requested_at'))<=_seconds('UPDATE_QUEUE_TIMEOUT',30):
                    raise HTTPException(409,{'message':'升级请求已经提交，正在等待服务接收','code':'update_busy'})
                REQUEST_FILE.unlink()
            metadata=version_info();timestamp=_timestamp();request_id=uuid4().hex
            state={'request_id':request_id,'status':'queued','started_at':timestamp,'updated_at':timestamp,
                   'current_commit':metadata['commit'],'target_commit':None,'branch':'main','detail':STATUS_DETAILS['queued'],
                   'last_checked_at':current.get('last_checked_at')}
            request={'request_id':request_id,'requested_at':timestamp,'current_commit':metadata['commit'],'branch':'main'}
            if actor and actor.get('id'):request['actor_id']=str(actor['id'])
            _write_json(STATE_FILE,state)
            try:
                # Worker takes the same lock before opening/claiming this file.
                fd=os.open(REQUEST_FILE,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w',encoding='utf-8') as out:
                    json.dump(request,out);out.flush();os.fsync(out.fileno())
            except OSError:
                state.update(status='failed',error_code='queue_write_failed',detail=ERROR_DETAILS['queue_write_failed'],updated_at=_timestamp(),finished_at=_timestamp())
                _write_json(STATE_FILE,state)
                raise HTTPException(500,{'message':ERROR_DETAILS['queue_write_failed'],'code':'queue_write_failed'})
            return _public_state(state)
    except OSError:
        raise HTTPException(503,{'message':ERROR_DETAILS['queue_write_failed'],'code':'queue_write_failed'})
