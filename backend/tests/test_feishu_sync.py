"""Live FastAPI/PG for HOMA, deterministic substitute for the Feishu service."""
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from unittest.mock import patch
from uuid import uuid4


class Bitable:
    def __init__(self):
        self.rows = {}
        self.tokens = {}
        self.field_names = []
        self.calls = []
        self.drop_create_reply = False
        self.hide_search = False
        self.duplicate_match = False
        self.ignore_update = False

    def request(self, method, path, body=None, token=None):
        from app.feishu_transport import RemoteError
        self.calls.append((method,path))
        if path.startswith('/auth/'):
            return {'code':0,'tenant_access_token':'test-token'}
        assert token == 'test-token'
        if '/fields?' in path:
            items=[{'field_name':key,'type':1} for key in self.field_names]
            return {'code':0,'data':{'items':items,'has_more':False}}
        if '/records/search?' in path:
            key=body['filter']['conditions'][0]['value'][0]
            items=[] if self.hide_search else [dict(record_id=r,fields={'本地ID':f['本地ID']}) for r,f in self.rows.items() if f['本地ID']==key]
            if self.duplicate_match:
                items*=2
            return {'code':0,'data':{'items':items,'has_more':False}}
        if method=='POST' and '?client_token=' in path:
            client_token=path.split('=')[1]
            row=self.tokens.setdefault(client_token,'rec'+str(len(self.tokens)))
            self.rows.setdefault(row,dict(body['fields']))
            if self.drop_create_reply:
                self.drop_create_reply=False
                raise RemoteError('测试网络中断，等待重试。')
            return {'code':0,'data':{'record':{'record_id':row}}}
        row=path.rsplit('/',1)[1]
        if row not in self.rows:
            raise RemoteError('原记录在飞书中不存在。')
        if method=='PUT' and not self.ignore_update:
            self.rows[row].update(body['fields'])
        return {'code':0,'data':{'record':{'record_id':row,'fields':{
            k:[{'type':'text','text':v}] for k,v in self.rows[row].items()}}}}


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION')=='1','requires disposable workflow database')
class FeishuSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('A disposable workflow database is required')
        from fastapi.testclient import TestClient
        from app.db import connection
        from app.main import app
        from app.workflow_auth import password_hash
        cls.db=staticmethod(connection)
        cls.anon=TestClient(app)
        cls.accounts={}
        cls.clients={}
        cls.addClassCleanup(cls.anon.close)

        def release_test_accounts():
            with connection() as conn:
                conn.execute('DELETE FROM workflow_accounts WHERE user_id = ANY(%s)', (list(cls.accounts.values()),))

        cls.addClassCleanup(release_test_accounts)
        password=secrets.token_urlsafe(24)
        hashed=password_hash(password)
        for role in ('owner','reviewer','other','admin'):
            email=f'{role}{uuid4().hex}@test.example'
            with connection() as conn:
                ident=conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',(email,role)).fetchone()['id']
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',(ident,hashed,role=='admin'))
            cls.accounts[role]=ident
            client=TestClient(app,headers={'X-Workflow-Request':'1'})
            cls.addClassCleanup(client.close)
            assert client.post('/api/workflow-auth/login',json={'email':email,'password':password}).status_code==200
            cls.clients[role]=client

    def setUp(self):
        from app import feishu_transport as remote
        self.upstream=Bitable()
        self.upstream.field_names=list(remote.FIELDS)
        self.enterContext(patch.object(remote,'request',side_effect=self.upstream.request))
        self.enterContext(patch.dict(os.environ,{
            'HOMA_FEISHU_APP_ID':'cliFake','HOMA_FEISHU_APP_SECRET':'test-secret',
            'HOMA_FEISHU_BASE_TOKEN':'baseA','HOMA_FEISHU_TABLE_ID':'tbl'+uuid4().hex}))
        with self.db() as conn:
            product=conn.execute("INSERT INTO products(model,name,marketplace) VALUES(%s,'例','US') RETURNING id",('F'+uuid4().hex,)).fetchone()['id']
        self.owner=self.clients['owner']
        created=self.owner.post('/api/workbench/items',json={'request_id':str(uuid4()),'product_id':str(product),
            'page_id':'overview','section_id':'risk_triage','site':'US','object_ref':'SKU-10',
            'title':'核对交期','source_note':'PRIVATE RAW NOTE','values':{
                'risk_kind':'供货风险','last_action_date':'2026-11-24','evidence_summary':'PRIVATE DOCUMENT'}})
        self.assertEqual(created.status_code,201,created.text)
        item=created.json()
        self.item_api='/api/workbench/items/'+item['id']
        self.path='/api/workbench/feishu/'+item['id']
        assigned=self.clients['admin'].patch(self.item_api,json={'expected_version':item['version'],'reviewer_id':str(self.accounts['reviewer'])})
        self.assertEqual(assigned.status_code,200,assigned.text)
        self.version=assigned.json()['version']

    def action(self, role, name):
        result=self.clients[role].post(self.item_api+'/actions',json={'expected_version':self.version,'action':name})
        self.assertEqual(result.status_code,200,result.text)
        self.version=result.json()['version']

    def sync(self):
        resp=self.owner.post(self.path+'/sync')
        self.assertEqual(resp.status_code,200,resp.text)
        return resp.json()

    def test_opt_in_and_private_scope(self):
        self.assertEqual(self.anon.get(self.path).status_code,401)
        self.assertEqual(self.owner.post(self.path+'/sync').status_code,409)
        self.action('owner','submit')
        other=self.clients['other']
        self.assertEqual(other.get(self.path).status_code,404)
        self.assertEqual(other.post(self.path+'/sync').status_code,404)
        with patch.dict(os.environ,{'HOMA_FEISHU_APP_SECRET':''}):
            self.assertEqual(self.owner.get(self.path).json()['status'],'unconfigured')
            self.assertEqual(self.owner.post(self.path+'/sync').status_code,409)
        self.assertFalse(self.upstream.calls)
        self.assertEqual(self.clients['reviewer'].post(self.path+'/sync').json()['status'],'synced')
        status=self.clients['admin'].get(self.path).json()
        self.assertEqual(status['version'],self.version)
        fields=next(iter(self.upstream.rows.values()))
        self.assertEqual(fields['事项'],'核对交期')
        self.assertNotIn('PRIVATE',str(fields))
        self.assertEqual(set(fields),set(self.upstream.field_names))
        self.assertNotIn('test-secret',self.owner.get('/api/workbench/feishu').text)

    def test_lost_create_response_then_concurrent_retries_and_revision(self):
        self.action('owner','submit')
        self.upstream.drop_create_reply=True
        self.assertEqual(self.sync()['status'],'failed')
        self.assertEqual(len(self.upstream.rows),1)
        self.upstream.hide_search=True   # emulate delayed search indexing after create
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts=list(pool.map(lambda _:self.sync(),range(2)))
        self.assertTrue(all(a['status']=='synced' for a in attempts))
        self.assertEqual(len(self.upstream.tokens),1)
        self.assertEqual(len(self.upstream.rows),1)
        record_link=attempts[0]['record_url']
        self.action('reviewer','approve')
        self.assertEqual(self.owner.get(self.path).json()['status'],'stale')
        revised=self.sync()
        self.assertEqual(revised['record_url'],record_link)
        self.assertEqual(revised['version'],self.version)
        self.assertEqual(next(iter(self.upstream.rows.values()))['状态'],'待执行')
        with patch.dict(os.environ,{'HOMA_FEISHU_TABLE_ID':'otherTable'}):
            self.assertEqual(self.owner.get(self.path).json()['status'],'not_synced')

    def test_schema_collision_and_readback_protect_master_table(self):
        self.action('owner','submit')
        self.upstream.field_names.remove('状态')
        self.assertIn('状态',self.sync()['message'])
        self.assertFalse(self.upstream.rows)
        self.upstream.field_names.append('状态')
        confirmed=self.sync()
        row=next(iter(self.upstream.rows.values()))
        self.upstream.duplicate_match=True
        self.assertEqual(self.sync()['status'],'failed')
        self.upstream.duplicate_match=False
        row['事项']='飞书中的旧标题'
        self.upstream.ignore_update=True
        failed=self.sync()
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed['last_synced_at'],confirmed['last_synced_at'])
        self.upstream.ignore_update=False
        self.assertEqual(self.sync()['status'],'synced')
        row['本地ID']='人为改变关联'
        self.assertEqual(self.sync()['status'],'failed')
        self.assertEqual(row['本地ID'],'人为改变关联')
