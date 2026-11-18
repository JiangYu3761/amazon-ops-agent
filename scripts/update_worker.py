"""Trusted, single-flight updater for JiangYu3761/amazon-ops-agent/main.

The worker owns the shared flock while checking, backing up and deploying. API
process restarts do not own the job. Its heartbeat is written by a separate thread.
"""
import argparse
import fcntl
import gzip
import hashlib
import re
import tarfile
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4

REPOSITORY='JiangYu3761/amazon-ops-agent'
BRANCH='main'
MESSAGES={
 'checking':'正在检查 GitHub main 分支', 'up_to_date':'当前已是 main 最新版本',
 'backing_up':'正在备份数据库和上传资料', 'updating':'正在构建并执行数据库迁移',
 'restarting':'正在重启服务并检查运行状态', 'rolling_back':'更新未完成，正在恢复旧代码与服务',
 'succeeded':'更新完成，服务检查通过', 'fetch_failed':'无法读取 GitHub 最新代码',
 'invalid_repository':'部署目录必须使用指定仓库的 HTTPS origin 和 main 分支',
 'non_fast_forward':'远程历史不是当前版本的快进更新，已停止',
 'dirty_checkout':'部署目录存在本地改动，已停止以保留这些改动',
 'backup_failed':'数据库或附件备份失败，尚未切换代码',
 'build_failed':'新版本构建失败', 'migration_failed':'数据库迁移失败，备份已保留',
 'health_failed':'新版本运行检查未通过', 'rollback_failed':'旧服务恢复未完成，需管理员检查部署日志和备份',
 'update_timeout':'更新超过执行时限，已停止并尝试恢复旧服务',
 'worker_interrupted':'检测到上次工作进程中断，已恢复或保留恢复信息',
 'docker_unavailable':'Docker 服务不可用', 'invalid_request':'更新请求格式无效',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        os.chmod(name,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path):
    try:
        value=json.loads(Path(path).read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


class UpdateFailure(Exception):
    def __init__(self,code):
        self.code=code
        super().__init__(MESSAGES.get(code,code))


class Worker:
    def __init__(self):
        self.app=Path(os.getenv('APP_DIR',os.getcwd())).resolve()
        self.state_path=Path(os.getenv('UPDATE_STATE_FILE','/var/run/amazon-agent/update-state.json'))
        self.request_path=Path(os.getenv('UPDATE_REQUEST_FILE',str(self.state_path.parent/'update.request')))
        self.active_path=self.request_path.parent/'update.active.json'
        self.heartbeat_path=Path(os.getenv('UPDATE_HEARTBEAT_FILE',str(self.request_path.parent/'update-worker.json')))
        self.lock_path=Path(os.getenv('UPDATE_LOCK_FILE',str(self.request_path.parent/'update.lock')))
        self.backup_dir=Path(os.getenv('BACKUP_DIR',str(self.app/'backups')))
        self.health_url=os.getenv('APP_HEALTH_URL','http://api:8000/health')
        self.timeout=max(1,int(os.getenv('UPDATE_TIMEOUT_SECONDS','1800')))
        self.health_attempts=max(1,min(60,int(os.getenv('UPDATE_HEALTH_ATTEMPTS','30'))))
        self.health_interval=max(0,float(os.getenv('UPDATE_HEALTH_INTERVAL','2')))
        self.available=False;self.reason='docker_unavailable';self.current=None;self.deployed_commit=None;self.source_matches=False;self.deployment_verified=False
        self.active_request_id=None;self.state={};self.deadline=None;self.log_path=None
        self.stop=threading.Event()
        self.script_path=Path(__file__).resolve()
        self.script_hash=hashlib.sha256(self.script_path.read_bytes()).hexdigest()

    def heartbeat(self):
        atomic_json(self.heartbeat_path,dict(status='ready' if self.available else 'unavailable',
            updated_at=now(),pid=os.getpid(),repository=REPOSITORY,branch=BRANCH,
            deployed_commit=self.deployed_commit,active_request_id=self.active_request_id,
            error_code=None if self.available else self.reason,detail=None if self.available else MESSAGES.get(self.reason,'升级工作进程尚未就绪')))

    def heartbeat_loop(self):
        while not self.stop.is_set():
            try:self.heartbeat()
            except OSError:pass
            self.stop.wait(2)

    def remaining(self,cap=1800):
        if self.deadline is None:return min(cap,30)
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise UpdateFailure('update_timeout')
        return max(.1,min(cap,remaining))

    def run(self,args,code='build_failed',output=None,cap=1800,raw=False):
        """Never send command output to the public state/HTTP response."""
        try:
            with tempfile.TemporaryFile() as errors:
                if output is not None:
                    descriptor=os.open(output,os.O_CREAT|os.O_WRONLY|os.O_EXCL,0o600)
                    with os.fdopen(descriptor,'wb') as out:
                        result=subprocess.run(args,cwd=self.app,stdout=out,stderr=errors,timeout=self.remaining(cap))
                    stdout=b''
                else:
                    result=subprocess.run(args,cwd=self.app,stdout=subprocess.PIPE,stderr=errors,timeout=self.remaining(cap))
                    stdout=result.stdout
                if self.log_path:
                    errors.seek(0)
                    with self.log_path.open('ab') as log:
                        shutil.copyfileobj(errors,log)
                if result.returncode:raise UpdateFailure(code)
                return stdout if raw else (stdout.decode('utf-8','replace').strip() if output is None else '')
        except subprocess.TimeoutExpired:
            raise UpdateFailure('update_timeout') from None
        except (OSError,ValueError):
            raise UpdateFailure(code) from None

    def git(self,*args,code='invalid_repository',raw=False):
        return self.run(['git','-c','safe.directory='+str(self.app),*args],code=code,cap=120,raw=raw)

    def compose(self,*args,**kwargs):
        return self.run(['docker','compose',*args],**kwargs)

    def assert_repository(self):
        raw=self.git('config','--get','remote.origin.url')
        try:
            parsed=urlsplit(raw);port=parsed.port
        except ValueError:raise UpdateFailure('invalid_repository') from None
        if (parsed.scheme!='https' or parsed.hostname!='github.com' or parsed.username or parsed.password
            or port not in (None,443) or parsed.query or parsed.fragment
            or parsed.path.rstrip('/').removesuffix('.git').lower()!='/'+REPOSITORY.lower()):
            raise UpdateFailure('invalid_repository')
        if self.git('symbolic-ref','--short','HEAD')!=BRANCH:
            raise UpdateFailure('invalid_repository')
        if not (self.app/'docker-compose.yml').is_file():raise UpdateFailure('invalid_repository')
        self.current=self.git('rev-parse','--verify','HEAD^{commit}')

    def readiness(self):
        try:
            self.assert_repository()
            self.run(['docker','info'],code='docker_unavailable',cap=5)
            self.probe_deployed()
            self.available=not (self.active_path.exists() and self.active_request_id is None)
            self.reason=None if self.available else 'worker_interrupted'
        except UpdateFailure as failure:
            self.available=False;self.reason=failure.code
        self.heartbeat()

    def api_url(self,path):
        return self.health_url.rsplit('/health',1)[0]+path

    def source_fingerprint(self):
        # Compare against committed HEAD, not dirty working files: an uncommitted
        # API build must never be attributed to a Git commit it does not contain.
        digest=hashlib.sha256()
        paths=self.git('ls-tree','-r','--name-only','HEAD','--','backend/app').splitlines()
        for path in sorted(p for p in paths if p.endswith('.py')):
            name=path.removeprefix('backend/')
            digest.update(name.encode()+b'\0'+self.git('show','HEAD:'+path,raw=True)+b'\0')
        digest.update(b'catalog.json\0'+self.git('show','HEAD:frontend/catalog.json',raw=True)+b'\0')
        return digest.hexdigest()

    def probe_deployed(self):
        # Last-known values are not fresh proof that the running API still matches.
        self.deployed_commit=None;self.source_matches=False;self.deployment_verified=False
        try:
            result=json.loads(self.run(['curl','-fsS','--max-time','5',self.api_url('/api/system/version')],code='health_failed',cap=6))
            fingerprint_provided='source_fingerprint' in result
            self.source_matches=bool(result.get('source_fingerprint') and result['source_fingerprint']==self.source_fingerprint())
            commit=result.get('commit')
            valid_commit=isinstance(commit,str) and bool(re.fullmatch('[a-fA-F0-9]{40}',commit))
            if valid_commit:
                # An explicitly older deployed version also covers the web image;
                # equal API bytes alone cannot prove that frontend was redeployed.
                if commit!=self.current or not fingerprint_provided or self.source_matches:
                    self.deployed_commit=commit
                self.deployment_verified=commit==self.current and (not fingerprint_provided or self.source_matches)
            elif self.source_matches:
                # Only an API with no known version can use byte identity fallback.
                self.deployed_commit=self.current;self.deployment_verified=True
        except (UpdateFailure,OSError,ValueError,AttributeError):
            self.source_matches=False

    def publish(self,status,**updates):
        self.state.update(updates,status=status,updated_at=now(),detail=MESSAGES.get(status,MESSAGES.get(updates.get('error_code'),'升级执行失败')))
        atomic_json(self.state_path,self.state)

    def ensure_clean(self):
        if self.git('status','--porcelain','--untracked-files=normal'):
            raise UpdateFailure('dirty_checkout')

    def images(self):
        snapshots=[]
        for service in ('api','web'):
            cid=self.compose('ps','-q',service,code='backup_failed',cap=30)
            if not cid:raise UpdateFailure('backup_failed')
            info=self.run(['docker','inspect','--format','{{.Image}}|{{.Config.Image}}',cid],code='backup_failed',cap=30)
            parts=info.split('|',1)
            if len(parts)!=2 or not all(parts):raise UpdateFailure('backup_failed')
            snapshots.append(dict(service=service,image_id=parts[0],reference=parts[1]))
        return snapshots

    def backup(self):
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8]
        prefix=self.backup_dir/('update-'+stamp)
        raw=Path(str(prefix)+'.sql.partial');database=Path(str(prefix)+'.sql.gz');files=Path(str(prefix)+'-files.tar.gz')
        self.compose('exec','-T','db','pg_dump','-U',os.getenv('POSTGRES_USER','amazon_agent'),os.getenv('POSTGRES_DB','amazon_agent'),code='backup_failed',output=raw)
        if not raw.stat().st_size:raise UpdateFailure('backup_failed')
        try:
            descriptor=os.open(database,os.O_CREAT|os.O_WRONLY|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as private_file,raw.open('rb') as src,gzip.GzipFile(fileobj=private_file,mode='wb') as dst:
                shutil.copyfileobj(src,dst)
        finally:raw.unlink(missing_ok=True)
        os.chmod(database,0o600)
        script="""import os,sys,tarfile
from pathlib import Path
root=Path('/var/run/amazon-agent')
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as out:
 uploads=Path(os.getenv('WORKFLOW_UPLOAD_DIR',str(root/'workflow-uploads')))
 if uploads.exists():out.add(uploads,arcname='workflow-uploads')
 code=Path(os.getenv('WORKFLOW_SETUP_TOKEN_FILE',str(root/'workflow-setup-token')))
 if code.exists():out.add(code,arcname='workflow-setup-token')
"""
        self.compose('exec','-T','api','python','-c',script,code='backup_failed',output=files)
        if not files.stat().st_size:raise UpdateFailure('backup_failed')
        try:
            with tarfile.open(files,'r:gz') as archive:archive.getmembers()
        except (tarfile.TarError,OSError):raise UpdateFailure('backup_failed') from None
        os.chmod(files,0o600)
        # Retained privately for deployment recovery; public API whitelists safe fields.
        self.state['backups']={'database':str(database),'files':str(files)}

    def healthy(self):
        for attempt in range(self.health_attempts):
            try:
                result=self.run(['curl','-fsS','--max-time','5',self.health_url],code='health_failed',cap=6)
                if json.loads(result).get('status')!='ok':raise ValueError()
                workspace=json.loads(self.run(['curl','-fsS','--max-time','5',self.api_url('/api/workspace')],code='health_failed',cap=6))
                if isinstance(workspace.get('products'),list) and isinstance(workspace.get('tasks'),list):return True
            except (UpdateFailure,ValueError,AttributeError):
                if self.deadline and time.monotonic()>=self.deadline:break
            if attempt+1<self.health_attempts:time.sleep(self.health_interval)
        return False

    def restore(self):
        # A recovery operation gets its own bounded deadline after a job timeout.
        self.deadline=time.monotonic()+120
        self.publish('rolling_back')
        old=self.state.get('current_commit')
        if not old:return 'not_needed'
        current=self.git('rev-parse','--verify','HEAD^{commit}')
        if current!=old:
            self.ensure_clean()
            self.git('reset','--hard',old,code='rollback_failed')
        self.current=old
        for image in self.state.get('previous_images',[]):
            self.run(['docker','tag',image['image_id'],image['reference']],code='rollback_failed',cap=30)
        env_commit=os.environ.get('APP_COMMIT')
        os.environ['APP_COMMIT']=self.state.get('previous_deployed_commit') or 'unknown'
        try:self.compose('up','-d','--no-build','--no-deps','--force-recreate','api','web',code='rollback_failed',cap=90)
        finally:
            if env_commit is None:os.environ.pop('APP_COMMIT',None)
            else:os.environ['APP_COMMIT']=env_commit
        if not self.healthy():raise UpdateFailure('rollback_failed')
        self.probe_deployed()
        return 'restored'

    def execute(self,request):
        self.deadline=time.monotonic()+self.timeout
        self.active_request_id=request['request_id']
        self.state={k:request[k] for k in ('request_id','actor_id') if k in request}
        self.state.update(started_at=now(),repository=REPOSITORY,branch=BRANCH,current_commit=None,target_commit=None)
        switched=False
        try:
            self.publish('checking')
            self.backup_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            self.log_path=self.backup_dir/('update-'+uuid4().hex+'.log')
            self.log_path.touch(mode=0o600)
            self.assert_repository()
            self.probe_deployed()
            self.state['current_commit']=self.current
            self.state['previous_deployed_commit']=self.deployed_commit
            self.ensure_clean()
            self.git('fetch','--no-tags','origin',BRANCH,code='fetch_failed')
            target=self.git('rev-parse','--verify','FETCH_HEAD^{commit}',code='fetch_failed')
            self.state.update(target_commit=target,last_checked_at=now())
            if target==self.current and self.deployment_verified:
                self.publish('up_to_date');return 0
            self.git('merge-base','--is-ancestor',self.current,target,code='non_fast_forward')
            self.state['previous_images']=self.images()
            self.publish('backing_up')
            self.backup()
            # Recheck after backup, before replacing any checkout content.
            self.ensure_clean()
            self.publish('updating')
            if target!=self.current:self.git('reset','--hard',target,code='build_failed')
            switched=True;self.current=target
            os.environ['APP_COMMIT']=target
            self.compose('build','api','web',code='build_failed')
            self.run(['sh','scripts/migrate.sh'],code='migration_failed')
            self.publish('restarting')
            self.compose('up','-d','--no-build','--no-deps','--force-recreate','api','web',code='health_failed')
            if not self.healthy():
                raise UpdateFailure('update_timeout' if self.deadline and time.monotonic()>=self.deadline else 'health_failed')
            self.probe_deployed()
            if not self.deployment_verified:raise UpdateFailure('health_failed')
            self.publish('succeeded');return 0
        except (UpdateFailure,OSError,ValueError) as error:
            code=error.code if isinstance(error,UpdateFailure) else 'backup_failed'
            recovery='not_needed'
            if switched:
                try:recovery=self.restore()
                except (UpdateFailure,OSError):recovery='failed'
            self.publish('failed',error_code='rollback_failed' if recovery=='failed' else code,recovery_status=recovery)
            return 1
        finally:
            self.active_request_id=None;self.deadline=None

    def recover_interrupted(self):
        self.state=read_json(self.state_path)
        request=read_json(self.active_path)
        if self.state.get('request_id')!=request.get('request_id'):
            self.state={'request_id':request.get('request_id','interrupted'),'started_at':now()}
            self.publish('failed',error_code='worker_interrupted',recovery_status='not_needed')
            self.active_path.unlink(missing_ok=True)
            return
        self.state.setdefault('request_id',request.get('request_id','interrupted'))
        if self.state.get('status') in {'succeeded','up_to_date'} or (self.state.get('status')=='failed' and 'recovery_status' in self.state):
            self.active_path.unlink(missing_ok=True)
            return
        recovery='not_needed'
        try:
            current=self.git('rev-parse','--verify','HEAD^{commit}')
            changed_code=bool(self.state.get('current_commit') and current!=self.state['current_commit'])
            changed_runtime=bool(self.state.get('previous_images') and self.state.get('status') in {'updating','restarting','rolling_back'})
            if changed_code or changed_runtime:
                recovery=self.restore()
        except (UpdateFailure,OSError):recovery='failed'
        self.publish('failed',error_code='worker_interrupted' if recovery!='failed' else 'rollback_failed',recovery_status=recovery)
        self.deadline=None
        self.active_path.unlink(missing_ok=True)

    def process_once(self,direct=False):
        self.lock_path.parent.mkdir(parents=True,exist_ok=True)
        with self.lock_path.open('a+') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return 0
            try:
                if self.active_path.exists():self.recover_interrupted()
                if direct:
                    request={'request_id':uuid4().hex,'requested_at':now()}
                    atomic_json(self.active_path,request)
                elif self.request_path.exists():
                    os.replace(self.request_path,self.active_path)
                    request=read_json(self.active_path)
                else:return 0
                if not isinstance(request.get('request_id'),str) or not request['request_id'] or len(request['request_id'])>200:
                    self.state={'request_id':None,'started_at':now()}
                    self.publish('failed',error_code='invalid_request');return 1
                previous=read_json(self.state_path)
                if previous.get('request_id')==request['request_id'] and previous.get('status') in {'succeeded','up_to_date','failed'}:
                    return 0
                return self.execute(request)
            finally:
                self.active_path.unlink(missing_ok=True)
                fcntl.flock(lock,fcntl.LOCK_UN)

    def serve(self,once=False,direct=False):
        self.readiness()
        thread=threading.Thread(target=self.heartbeat_loop,daemon=True);thread.start()
        try:
            if once:return self.process_once(direct)
            while True:
                self.process_once()
                # Reload only after the transaction released its lock and active
                # marker. The worker container stays alive throughout deployment.
                if hashlib.sha256(self.script_path.read_bytes()).hexdigest()!=self.script_hash:
                    self.stop.set();thread.join(timeout=3)
                    self.available=False;self.reason='worker_interrupted';self.heartbeat()
                    os.execv(sys.executable,[sys.executable,str(self.script_path)])
                self.readiness()
                self.stop.wait(2)
        finally:
            self.stop.set();thread.join(timeout=3)
            # A deliberately stopped worker is not falsely advertised as available.
            self.available=False;self.reason='worker_interrupted';self.heartbeat()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--direct',action='store_true')
    args=parser.parse_args()
    return Worker().serve(args.once,args.direct)


if __name__=='__main__':
    raise SystemExit(main())
