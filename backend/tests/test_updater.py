"""Queue and recovery checks never invoke Git, Docker or the deployment worker."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import fcntl
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app import updater


def timestamp(seconds_ago=0):
    return (datetime.now(timezone.utc)-timedelta(seconds=seconds_ago)).isoformat()


class UpdateRequestTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        root=Path(self.directory.name)
        for name,filename in [('STATE_FILE','state.json'),('REQUEST_FILE','update.request'),('HEARTBEAT_FILE','update-worker.json'),('LOCK_FILE','update.lock')]:
            p=patch.object(updater,name,root/filename);p.start();self.addCleanup(p.stop)
        env=patch.dict(os.environ,{'UPDATE_ENABLED':'true','APP_COMMIT':'a'*40});env.start();self.addCleanup(env.stop)
        self.heartbeat()

    def heartbeat(self,age=0,**values):
        updater.HEARTBEAT_FILE.write_text(json.dumps({'status':'ready','updated_at':timestamp(age),'repository':updater.REPOSITORY,'branch':'main','active_request_id':None,'deployed_commit':'a'*40,**values}))

    def test_default_enable_requires_a_live_worker(self):
        with patch.dict(os.environ,{}):
            os.environ.pop('UPDATE_ENABLED',None)
            self.assertTrue(updater.version_info()['update_enabled'])
            updater.HEARTBEAT_FILE.unlink()
            self.assertFalse(updater.version_info()['update_enabled'])
            self.assertTrue(updater.version_info()['update_configured'])

    def test_disabled_deployment_does_not_queue(self):
        with patch.dict(os.environ,{'UPDATE_ENABLED':'false'}):
            with self.assertRaises(HTTPException) as error:updater.start_update()
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(error.exception.detail['code'],'updates_disabled')
        self.assertFalse(updater.STATE_FILE.exists());self.assertFalse(updater.REQUEST_FILE.exists())

    def test_missing_or_stale_worker_is_unavailable(self):
        for age in [60,None]:
            if age is None:updater.HEARTBEAT_FILE.unlink(missing_ok=True)
            else:self.heartbeat(age)
            with self.assertRaises(HTTPException) as error:updater.start_update()
            self.assertEqual(error.exception.status_code,503)
            self.assertEqual(error.exception.detail['code'],'worker_unavailable')
            self.assertFalse(updater.REQUEST_FILE.exists())

    def test_queues_correlated_request_without_an_upgrade_password(self):
        with patch.dict(os.environ,{'UPDATE_ADMIN_TOKEN':''}):state=updater.start_update({'id':'authenticated-admin'})
        request=json.loads(updater.REQUEST_FILE.read_text())
        self.assertEqual(state['status'],'queued');self.assertEqual(state['request_id'],request['request_id'])
        self.assertEqual(request['actor_id'],'authenticated-admin');self.assertEqual(request['branch'],'main')
        self.assertEqual(request['current_commit'],'a'*40);self.assertIsNone(state['target_commit'])
        self.assertNotIn('actor_id',updater.read_state())

    def test_concurrent_requests_only_create_one_job(self):
        def attempt(_):
            try:updater.start_update();return 202
            except HTTPException as error:return error.status_code
        with ThreadPoolExecutor(max_workers=6) as pool:statuses=list(pool.map(attempt,range(6)))
        self.assertEqual(statuses.count(202),1);self.assertEqual(statuses.count(409),5)
        before=updater.REQUEST_FILE.read_text()
        with self.assertRaises(HTTPException) as error:updater.start_update()
        self.assertEqual(error.exception.detail['job']['status'],'queued')
        self.assertEqual(updater.REQUEST_FILE.read_text(),before)

    def test_worker_lock_prevents_duplicate_queue_and_false_recovery(self):
        updater.STATE_FILE.write_text(json.dumps({'status':'updating','request_id':'running-job','started_at':timestamp(4000),'updated_at':timestamp(4000)}))
        with updater.LOCK_FILE.open('a') as worker:
            fcntl.flock(worker,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(HTTPException) as error:updater.start_update()
            self.assertEqual(error.exception.status_code,409)
            self.assertEqual(updater.read_state()['status'],'updating')
            fcntl.flock(worker,fcntl.LOCK_UN)
        self.assertEqual(updater.read_state()['status'],'failed')
        self.assertEqual(updater.read_state()['error_code'],'update_timeout')

    def test_unclaimed_queue_expires_and_can_be_retried(self):
        updater.STATE_FILE.write_text(json.dumps({'status':'queued','request_id':'old-job','started_at':timestamp(60),'updated_at':timestamp(60)}))
        updater.REQUEST_FILE.write_text(json.dumps({'request_id':'old-job','requested_at':timestamp(60)}))
        state=updater.read_state();self.assertEqual(state['status'],'failed');self.assertEqual(state['error_code'],'request_timeout')
        self.assertFalse(updater.REQUEST_FILE.exists())
        retry=updater.start_update();self.assertEqual(retry['status'],'queued');self.assertNotEqual(retry['request_id'],'old-job')

    def test_crashed_worker_gets_explicit_failed_state(self):
        updater.STATE_FILE.write_text(json.dumps({'status':'checking','request_id':'crashed-job','started_at':timestamp(120),'updated_at':timestamp(120)}))
        updater.HEARTBEAT_FILE.unlink()
        state=updater.read_state();self.assertEqual(state['status'],'failed');self.assertEqual(state['error_code'],'worker_interrupted')
        self.assertEqual(json.loads(updater.STATE_FILE.read_text())['status'],'failed')

    def test_public_progress_does_not_expose_paths_secrets_or_raw_errors(self):
        updater.STATE_FILE.write_text(json.dumps({'status':'failed','request_id':'safe-job','current_commit':'a'*40,'target_commit':'b'*40,
            'detail':'token=private-value /srv/private-backups/database.sql','error':'https://user:private-value@example.test',
            'backup_path':'/srv/private-backups/database.sql','actor_id':'private-person','error_code':'build_failed'}))
        public=updater.read_state();text=json.dumps(public)
        for private in ['private-value','private-backups','private-person','backup_path']:self.assertNotIn(private,text)
        self.assertEqual(public['error_code'],'build_failed');self.assertEqual(public['target_commit'],'b'*40)

    def test_unknown_checkout_head_is_not_claimed_as_deployed(self):
        self.heartbeat(current_commit='b'*40,deployed_commit=None)
        with patch.dict(os.environ,{'APP_COMMIT':'unknown'}),patch.object(updater,'_git',return_value='b'*40):
            self.assertIsNone(updater.version_info()['commit'])
        self.heartbeat(current_commit='b'*40,deployed_commit='a'*40)
        with patch.dict(os.environ,{'APP_COMMIT':'unknown'}):self.assertEqual(updater.version_info()['commit'],'a'*40)

    def test_queue_write_failure_marks_failed(self):
        original=os.open
        def fail(path,*args,**kwargs):
            if Path(path)==updater.REQUEST_FILE:raise OSError('private storage detail')
            return original(path,*args,**kwargs)
        with patch.object(os,'open',side_effect=fail):
            with self.assertRaises(HTTPException) as error:updater.start_update()
        self.assertEqual(error.exception.status_code,500)
        self.assertEqual(updater.read_state()['error_code'],'queue_write_failed')
        self.assertNotIn('private storage detail',str(error.exception.detail))

    def test_invalid_state_is_explicit_and_recoverable(self):
        updater.STATE_FILE.write_text('not json')
        self.assertEqual(updater.read_state()['error_code'],'invalid_state')
        self.assertEqual(updater.start_update()['status'],'queued')

    def test_interrupted_active_transaction_is_not_overwritten_by_api(self):
        old={'request_id':'old-active-job','status':'updating','started_at':timestamp(4000),'updated_at':timestamp(4000),
             'current_commit':'a'*40,'target_commit':'b'*40,'previous_images':{'api':'private-previous-image'},'backup_path':'/private/backup.sql'}
        updater.STATE_FILE.write_text(json.dumps(old));original=updater.STATE_FILE.read_bytes()
        active=updater.REQUEST_FILE.parent/'update.active.json'
        active.write_text(json.dumps({'request_id':'old-active-job','requested_at':timestamp(4000)}));active_bytes=active.read_bytes()
        self.heartbeat(active_request_id=None)
        status=updater.read_state()
        self.assertEqual(status['status'],'recovering');self.assertEqual(status['request_id'],'old-active-job')
        self.assertEqual(updater.STATE_FILE.read_bytes(),original)
        with self.assertRaises(HTTPException) as error:updater.start_update()
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(error.exception.detail['code'],'update_recovering')
        self.assertEqual(updater.STATE_FILE.read_bytes(),original);self.assertEqual(active.read_bytes(),active_bytes)
        self.assertFalse(updater.REQUEST_FILE.exists())
        # The worker settles recovery before releasing its durable active marker.
        updater.STATE_FILE.write_text(json.dumps({**old,'status':'failed','error_code':'worker_interrupted'}))
        settled=updater.STATE_FILE.read_bytes()
        with self.assertRaises(HTTPException):updater.start_update()
        self.assertEqual(updater.STATE_FILE.read_bytes(),settled)
        active.unlink()
        result=updater.start_update()
        self.assertEqual(result['status'],'queued');self.assertNotEqual(result['request_id'],'old-active-job')
