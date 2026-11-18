"""Session-only upgrade HTTP checks against the existing disposable test database."""
import os
import json
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import secrets
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION')=='1','requires disposable integration database')
class UpdateAPIIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from urllib.parse import urlsplit
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):raise RuntimeError('Requires disposable database')
        from app.main import app
        from app.db import connection
        from app.workflow_auth import password_hash
        from fastapi.testclient import TestClient
        cls.app=app;cls.Client=TestClient;cls.clients={};cls.ids={}
        password=secrets.token_urlsafe(24);hashed=password_hash(password)
        with connection() as conn:
            for role in ['admin','member']:
                user=conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',('update-'+role+'@test.example','升级测试-'+role)).fetchone()
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',(user['id'],hashed,role=='admin'))
                cls.ids[role]=str(user['id'])
            conn.commit()
        for role in cls.ids:
            client=TestClient(app,headers={'X-Workflow-Request':'1'})
            response=client.post('/api/workflow-auth/login',json={'email':'update-'+role+'@test.example','password':password})
            assert response.status_code==200,response.text
            cls.clients[role]=client

    def setUp(self):
        from app import updater
        self.updater=updater;self.directory=TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        for name,filename in [('STATE_FILE','state.json'),('REQUEST_FILE','update.request'),('HEARTBEAT_FILE','update-worker.json'),('LOCK_FILE','update.lock')]:
            p=patch.object(updater,name,Path(self.directory.name)/filename);p.start();self.addCleanup(p.stop)
        env=patch.dict(os.environ,{'UPDATE_ENABLED':'true','UPDATE_ADMIN_TOKEN':'','APP_COMMIT':'a'*40});env.start();self.addCleanup(env.stop)
        updater.HEARTBEAT_FILE.write_text(json.dumps({'status':'ready','updated_at':datetime.now(timezone.utc).isoformat(),'repository':updater.REPOSITORY,'branch':'main'}))

    def test_administrator_click_queues_without_password_or_body(self):
        result=self.clients['admin'].post('/api/system/update')
        self.assertEqual(result.status_code,202,result.text);self.assertEqual(result.json()['status'],'queued')
        request=json.loads(self.updater.REQUEST_FILE.read_text());self.assertEqual(request['actor_id'],self.ids['admin'])
        self.assertEqual(request['request_id'],result.json()['request_id'])
        progress=self.clients['admin'].get('/api/system/update-status')
        self.assertEqual(progress.json()['request_id'],request['request_id'])
        duplicate=self.clients['admin'].post('/api/system/update')
        self.assertEqual(duplicate.status_code,409);self.assertEqual(duplicate.json()['detail']['job']['request_id'],request['request_id'])

    def test_anonymous_and_member_cannot_upgrade_even_with_old_token_header(self):
        self.assertEqual(self.Client(self.app,headers={'X-Workflow-Request':'1','X-Admin-Token':'anything'}).post('/api/system/update').status_code,401)
        self.assertEqual(self.clients['member'].post('/api/system/update',headers={'X-Admin-Token':'anything'}).status_code,403)
        self.assertFalse(self.updater.REQUEST_FILE.exists())

    def test_csrf_guard_and_missing_worker_do_not_enqueue(self):
        self.assertEqual(self.clients['admin'].post('/api/system/update',headers={'Origin':'https://foreign.example'}).status_code,403)
        self.updater.HEARTBEAT_FILE.unlink()
        r=self.clients['admin'].post('/api/system/update');self.assertEqual(r.status_code,503);self.assertEqual(r.json()['detail']['code'],'worker_unavailable')
        self.assertFalse(self.updater.REQUEST_FILE.exists())

    def test_public_status_exposes_safe_progress_without_private_worker_data(self):
        self.updater.STATE_FILE.write_text(json.dumps({'status':'failed','request_id':'public-safe-job','backup_path':'/private/data','detail':'token=never-public','error_code':'health_failed'}))
        client=self.Client(self.app)
        self.assertEqual(client.get('/api/system/version').status_code,200)
        r=client.get('/api/system/update-status');self.assertEqual(r.status_code,200)
        self.assertNotIn('never-public',r.text);self.assertNotIn('/private/data',r.text)
        self.assertEqual(r.json()['error_code'],'health_failed')
