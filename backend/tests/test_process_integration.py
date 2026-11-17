"""Run only against a disposable homa_workflow_test_* database (see README)."""
import os
import secrets
import unittest
from uuid import uuid4


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION')=='1', 'requires a disposable PostgreSQL test database')
class ProcessIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from urllib.parse import urlsplit
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('Integration tests require a disposable homa_workflow_test_* database')
        from fastapi.testclient import TestClient
        from app.main import app
        from app.db import connection
        cls.app=app
        cls.connection=staticmethod(connection)
        cls.Client=TestClient
        cls.password=secrets.token_urlsafe(24)
        cls.admin=cls.client()
        r=cls.admin.post('/api/workflow-auth/setup',json=dict(email='admin@test.example',display_name='测试管理员',password=cls.password,setup_code=os.environ['WORKFLOW_SETUP_TOKEN']))
        assert r.status_code==200,r.text
        cls.admin_id=r.json()['id']
        cls.people={}
        for key in ('alice','bob','carol','outsider'):
            r=cls.admin.post('/api/workflow-auth/members',json=dict(email=key+'@test.example',display_name=key,department='测试',password=cls.password))
            assert r.status_code==201,r.text
            cls.people[key]=r.json()['id']
            client=cls.client()
            r=client.post('/api/workflow-auth/login',json=dict(email=key+'@test.example',password=cls.password))
            assert r.status_code==200,r.text
            setattr(cls,key,client)

    @classmethod
    def client(cls):
        return cls.Client(cls.app,headers={'X-Workflow-Request':'1'})

    def setUp(self):
        with self.connection() as conn:
            self.product=conn.execute("INSERT INTO products(model,name,marketplace) VALUES(%s,'集成测试','US') RETURNING id", ('test-'+uuid4().hex,)).fetchone()['id']
            template=conn.execute("SELECT id FROM workflow_templates LIMIT 1").fetchone()['id']
            workflow=conn.execute('INSERT INTO product_workflows(product_id,workflow_template_id,template_version) VALUES(%s,%s,1) RETURNING id',(self.product,template)).fetchone()['id']
            self.ids=[]
            for index in range(2):
                self.ids.append(str(conn.execute("INSERT INTO tasks(product_workflow_id,stage_name,title,sequence,module_code) VALUES(%s,'测试',%s,%s,'review') RETURNING id", (workflow,'测试节点'+str(index),index)).fetchone()['id']))
            conn.commit()

    def historical_records(self):
        """Seed old production-shaped rows through SQL, never through retired writes."""
        from psycopg.types.json import Jsonb
        from uuid import UUID
        task_id=self.ids[0]
        fields=[dict(id='summary',label='旧版交付说明',type='textarea',required=True)]
        snapshot=dict(fields=fields,reviewer_ids=[self.people['bob']],owner_id=self.people['alice'],
                      collaborator_ids=[],review_mode='all',location='旧版认证资料室',planned_start='2026-10-01')
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET owner_id=%s,status='completed',approval_required=true,evidence_required=true,revision=3,completed_at=now() WHERE id=%s",(self.people['alice'],task_id))
            conn.execute("INSERT INTO process_configs(task_id,version,fields,reviewer_ids,collaborator_ids,review_mode,location,planned_start,updated_by) VALUES(%s,1,%s,%s,'{}','all',%s,'2026-10-01',%s)",
                         (task_id,Jsonb(fields),[UUID(self.people['bob'])],snapshot['location'],self.admin_id))
            conn.execute('INSERT INTO process_states(task_id,version,draft) VALUES(%s,5,%s)',(task_id,Jsonb({'summary':'旧版尚未提交的补充草稿'})))
            conn.execute('INSERT INTO process_config_history(task_id,version,snapshot,actor_id) VALUES(%s,1,%s,%s)',(task_id,Jsonb(snapshot),self.admin_id))
            submission=conn.execute('INSERT INTO process_submissions(task_id,task_revision,config_version,snapshot,answers,submitted_by) VALUES(%s,3,1,%s,%s,%s) RETURNING id',
                (task_id,Jsonb(snapshot),Jsonb({'summary':'旧版已验收的 FCC 资料'}),self.people['alice'])).fetchone()['id']
            conn.execute("INSERT INTO process_decisions(submission_id,reviewer_id,decision,comment) VALUES(%s,%s,'approved','旧版资料核对通过')",(submission,self.people['bob']))
            evidence=conn.execute("INSERT INTO task_evidence(task_id,evidence_type,uri,note,uploaded_by) VALUES(%s,'url','https://example.com/fcc-original','旧版正本资料',%s) RETURNING id",(task_id,self.people['alice'])).fetchone()['id']
            approval=conn.execute("INSERT INTO approvals(task_id,approver_id,decision,comment,decided_at,task_revision) VALUES(%s,%s,'approved','旧版最终验收通过',now(),3) RETURNING id",(task_id,self.people['bob'])).fetchone()['id']
            document=conn.execute("INSERT INTO product_documents(product_id,task_id,title,url,version,preparation_status,evidence_id,note) VALUES(%s,%s,'旧版 FCC 正本','https://example.com/fcc-original','v1','prepared',%s,'迁移前资料索引') RETURNING id",(self.product,task_id,evidence)).fetchone()['id']
            conn.commit()
        return {'submission':str(submission),'evidence':str(evidence),'approval':str(approval),'document':str(document)}

    def legacy_snapshot(self):
        result={}
        tables=['process_configs','process_states','process_config_history','process_submissions','task_evidence','approvals','product_documents']
        with self.connection() as conn:
            for table in tables:
                result[table]=[r['data'] for r in conn.execute('SELECT row_to_json(r) data FROM '+table+' r WHERE task_id=%s',(self.ids[0],)).fetchall()]
            result['task']=conn.execute('SELECT row_to_json(t) data FROM tasks t WHERE id=%s',(self.ids[0],)).fetchone()['data']
            result['decisions']=[r['data'] for r in conn.execute('SELECT row_to_json(d) data FROM process_decisions d JOIN process_submissions s ON s.id=d.submission_id WHERE s.task_id=%s',(self.ids[0],)).fetchall()]
        return result

    def retired_requests(self, client):
        """All bodies are valid so failures demonstrate retirement, not parsing errors."""
        task=self.ids[0]
        config=dict(expected_version=0,owner_id=self.people['alice'],reviewer_ids=[self.people['bob']],
                    fields=[dict(id='summary',label='旧字段',type='textarea',required=True)])
        calls=[
            ('status-complete',lambda:client.patch('/api/tasks/'+task+'/status',json={'status':'completed'})),
            ('status-skip',lambda:client.patch('/api/tasks/'+task+'/status',json={'status':'not_applicable'})),
            ('approval',lambda:client.post('/api/tasks/'+task+'/approval',json={'decision':'approved','comment':'绕过申请'})),
            ('evidence',lambda:client.post('/api/tasks/'+task+'/evidence',json={'evidence_type':'note','uri':'试图追加旧证据'})),
            ('document',lambda:client.post('/api/products/'+str(self.product)+'/documents',json={'task_id':task,'title':'试图追加索引','url':'https://example.com/should-not-write','version':'v2'})),
            ('task-config',lambda:client.patch('/api/tasks/'+task,json={'title':'试图覆盖任务','dependencies':[]})),
            ('process-config',lambda:client.put('/api/process/nodes/'+task+'/config',json=config)),
            ('process-save',lambda:client.post('/api/process/nodes/'+task+'/work',json={'action':'save','expected_version':1,'answers':{'summary':'试图覆盖'}})),
            ('process-reopen',lambda:client.post('/api/process/nodes/'+task+'/work',json={'action':'reopen','expected_version':1,'reason':'试图绕过新审批重开'})),
            ('process-review',lambda:client.post('/api/process/nodes/'+task+'/review',json={'expected_version':1,'submission_id':str(uuid4()),'decision':'approved'})),
            ('process-enable',lambda:client.post('/api/process/products/'+str(self.product)+'/enable')),
            ('process-upload',lambda:client.post('/api/process/nodes/'+task+'/attachments',files={'file':('legacy.txt',b'should not be written')})),
        ]
        for name,call in calls:
            with self.subTest(endpoint=name):
                response=call()
                self.assertIn(response.status_code,(403,409),response.text)

    def test_original_history_is_available_from_new_and_legacy_detail(self):
        ids=self.historical_records()
        before=self.legacy_snapshot()
        response=self.alice.get('/api/approvals/tasks/'+self.ids[0])
        self.assertEqual(response.status_code,200,response.text)
        data=response.json()
        self.assertIsNone(data['case'])
        legacy=data['legacy']
        self.assertEqual(legacy['submissions'][0]['id'],ids['submission'])
        self.assertEqual(legacy['submissions'][0]['answers']['summary'],'旧版已验收的 FCC 资料')
        self.assertEqual(legacy['submissions'][0]['decisions'][0]['comment'],'旧版资料核对通过')
        self.assertEqual(legacy['evidence'][0]['id'],ids['evidence'])
        self.assertEqual(legacy['approvals'][0]['id'],ids['approval'])
        self.assertEqual(legacy['product_documents'][0]['id'],ids['document'])
        self.assertEqual(legacy['config']['location'],'旧版认证资料室')
        self.assertEqual(legacy['state']['draft']['summary'],'旧版尚未提交的补充草稿')
        self.assertEqual(legacy['config_history'][0]['version'],1)
        old=self.alice.get('/api/process/nodes/'+self.ids[0])
        self.assertEqual(old.status_code,200,old.text)
        self.assertEqual(old.json()['answers']['summary'],'旧版尚未提交的补充草稿')
        self.assertEqual(old.json()['submissions'][0]['id'],ids['submission'])
        self.assertEqual(old.json()['node']['status'],'completed')
        self.assertEqual(self.legacy_snapshot(),before)

    def test_anonymous_new_and_legacy_views_hide_old_answers_and_history(self):
        self.historical_records()
        anonymous=self.client()
        data=anonymous.get('/api/approvals/tasks/'+self.ids[0]).json()
        self.assertEqual(data['legacy'],{})
        self.assertEqual(data['history'],[])
        self.assertTrue(data['config']['stages'])
        old=anonymous.get('/api/process/nodes/'+self.ids[0]).json()
        self.assertEqual(old['answers'],{})
        self.assertEqual(old['submissions'],[])
        self.assertEqual(old['config_history'],[])

    def test_retired_writes_cannot_bypass_an_unstarted_sop(self):
        with self.connection() as conn:
            conn.execute('UPDATE tasks SET owner_id=%s WHERE id=%s',(self.people['alice'],self.ids[0]))
            conn.commit()
        before=self.legacy_snapshot()
        self.retired_requests(self.alice)
        self.retired_requests(self.admin)
        self.assertEqual(self.legacy_snapshot(),before)
        with self.connection() as conn:
            self.assertIsNone(conn.execute('SELECT id FROM approval_cases WHERE task_id=%s',(self.ids[0],)).fetchone())
            self.assertIsNone(conn.execute('SELECT task_id FROM process_configs WHERE task_id=%s',(self.ids[0],)).fetchone())

    def test_retired_writes_preserve_existing_completed_records(self):
        self.historical_records()
        before=self.legacy_snapshot()
        self.retired_requests(self.alice)
        self.retired_requests(self.admin)
        self.assertEqual(self.legacy_snapshot(),before)

    def test_only_admin_configures_and_member_headers_cannot_promote(self):
        path='/api/approvals/tasks/'+self.ids[0]+'/config'
        self.assertEqual(self.client().put(path,json={}).status_code,401)
        self.assertEqual(self.alice.put(path,json={},headers={'X-Admin-Token':'anything','X-User-Id':self.admin_id}).status_code,403)
        self.assertEqual(self.alice.patch('/api/tasks/'+self.ids[0],json={}).status_code,403)
        self.assertEqual(self.bob.post('/api/workflow-auth/members',json={}).status_code,403)
        self.assertEqual(self.admin.post('/api/workflow-auth/members',json=dict(email='promoted@test.example',display_name='ordinary',department='测试',password=self.password,is_admin=True)).status_code,422)
        self.assertEqual(self.alice.post('/api/workflow-auth/setup',json=dict(email='evil@test.example',password=self.password,display_name='evil',setup_code=os.environ['WORKFLOW_SETUP_TOKEN'])).status_code,409)

    def test_unauthenticated_retired_writes_require_login(self):
        anonymous=self.client()
        self.assertEqual(anonymous.patch('/api/tasks/'+self.ids[0]+'/status',json={'status':'not_applicable'}).status_code,401)
        self.assertEqual(anonymous.post('/api/process/nodes/'+self.ids[0]+'/work',json={'action':'save','expected_version':0}).status_code,401)

    def test_cookie_csrf_and_logout(self):
        client=self.client()
        r=client.post('/api/workflow-auth/login',json=dict(email='alice@test.example',password=self.password))
        self.assertEqual(r.status_code,200)
        self.assertIn('HttpOnly',r.headers['set-cookie'])
        self.assertIn('SameSite=strict',r.headers['set-cookie'])
        self.assertEqual(client.post('/api/workflow-auth/logout',headers={'Origin':'https://foreign.example'}).status_code,403)
        self.assertEqual(client.post('/api/workflow-auth/logout',headers={'X-Workflow-Request':'0'}).status_code,403)
        self.assertEqual(client.post('/api/workflow-auth/logout').status_code,200)
        self.assertIsNone(client.get('/api/workflow-auth/session').json()['user'])
