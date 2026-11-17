"""Run only against a disposable homa_workflow_test_* database (see README)."""
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def config(self,index=0,**changes):
        node=self.admin.get('/api/process/nodes/'+self.ids[index]).json()['node']
        body=dict(expected_version=node['state_version'],owner_id=self.people['alice'],reviewer_ids=[self.people['bob'],self.people['carol']],
                  fields=[dict(id='summary',label='工作说明',type='textarea',required=True),dict(id='files',label='资料',type='attachment',required=False)],
                  review_mode='all',dependencies=[],collaborator_ids=[])
        body.update(changes)
        return self.admin.put('/api/process/nodes/'+self.ids[index]+'/config',json=body)

    def detail(self,index=0,client=None):
        return (client or self.alice).get('/api/process/nodes/'+self.ids[index]).json()

    def submit(self,index=0,**kwargs):
        node=self.detail(index)['node']
        body=dict(action='submit',expected_version=node['state_version'],answers={'summary':'已核实本次交付'})
        body.update(kwargs)
        return self.alice.post('/api/process/nodes/'+self.ids[index]+'/work',json=body)

    def decide(self,client=None,index=0,decision='approved',comment=''):
        d=self.detail(index)
        return (client or self.bob).post('/api/process/nodes/'+self.ids[index]+'/review',json=dict(
            expected_version=d['node']['state_version'],submission_id=d['submissions'][0]['id'],decision=decision,comment=comment))

    def test_only_admin_configures_and_role_or_headers_cannot_be_spoofed(self):
        self.assertEqual(self.config().status_code,200)
        path='/api/process/nodes/'+self.ids[0]+'/config'
        self.assertEqual(self.client().put(path,json={}).status_code,401)
        self.assertEqual(self.alice.put(path,json={},headers={'X-Admin-Token':'anything','X-User-Id':self.admin_id}).status_code,403)
        self.assertEqual(self.alice.patch('/api/tasks/'+self.ids[0],json={}).status_code,403)
        self.assertEqual(self.bob.post('/api/workflow-auth/members',json={}).status_code,403)
        self.assertEqual(self.admin.post('/api/workflow-auth/members',json=dict(email='promoted@test.example',display_name='ordinary',department='测试',password=self.password,is_admin=True)).status_code,422)
        self.assertEqual(self.admin.patch('/api/tasks/'+self.ids[0]+'/status',json={'status':'completed'}).status_code,409)
        self.assertEqual(self.admin.post('/api/tasks/'+self.ids[0]+'/approval',json={'decision':'approved'}).status_code,409)
        self.assertEqual(self.alice.post('/api/workflow-auth/setup',json=dict(email='evil@test.example',password=self.password,display_name='evil',setup_code=os.environ['WORKFLOW_SETUP_TOKEN'])).status_code,409)

    def test_only_assigned_people_fill_and_direct_completion_is_rejected(self):
        self.assertEqual(self.config().status_code,200)
        node=self.detail()['node']
        path='/api/process/nodes/'+self.ids[0]+'/work'
        self.assertEqual(self.outsider.post(path,json=dict(action='save',expected_version=node['state_version'],answers={})).status_code,403)
        self.assertEqual(self.alice.post(path,json=dict(action='completed',expected_version=node['state_version'])).status_code,422)
        self.assertEqual(self.submit(answers={}).status_code,422)
        self.assertEqual(self.submit().status_code,200)
        self.assertEqual(self.detail()['node']['status'],'pending_approval')
        self.assertEqual(self.decide(self.admin).status_code,403)

    def test_all_reviewers_and_snapshot_are_required(self):
        self.assertEqual(self.config().status_code,200)
        self.assertEqual(self.submit().status_code,200)
        self.assertEqual(self.decide().status_code,200)
        self.assertEqual(self.detail()['node']['status'],'pending_approval')
        self.assertEqual(self.decide().status_code,409)
        self.assertEqual(self.decide(self.carol).status_code,200)
        d=self.detail()
        self.assertEqual(d['node']['status'],'completed')
        self.assertIsNotNone(d['node']['completed_at'])
        self.assertEqual(d['submissions'][0]['answers']['summary'],'已核实本次交付')
        self.assertEqual(len(d['submissions'][0]['decisions']),2)
        self.assertEqual(self.config().status_code,409)
        self.assertEqual(self.client().get('/api/process/nodes/'+self.ids[0]).json()['submissions'],[])

    def test_any_and_sequential_modes(self):
        self.assertEqual(self.config(review_mode='any').status_code,200)
        self.assertEqual(self.submit().status_code,200)
        self.assertEqual(self.decide(self.carol).status_code,200)
        self.assertEqual(self.detail()['node']['status'],'completed')
        self.assertEqual(self.config(1,review_mode='sequential').status_code,200)
        self.assertEqual(self.submit(1).status_code,200)
        self.assertEqual(self.decide(self.carol,1).status_code,409)
        self.assertEqual(self.decide(self.bob,1).status_code,200)
        self.assertEqual(self.decide(self.carol,1).status_code,200)
        self.assertEqual(self.detail(1)['node']['status'],'completed')

    def test_rejection_requires_reason_and_resubmission_keeps_history(self):
        self.config()
        self.submit()
        self.assertEqual(self.decide(decision='rejected').status_code,422)
        self.assertEqual(self.decide(decision='rejected',comment='需要补充新版资料').status_code,200)
        self.assertEqual(self.detail()['node']['status'],'in_progress')
        self.assertEqual(self.submit(answers={'summary':'已补充新版'}).status_code,200)
        d=self.detail()
        self.assertEqual(len(d['submissions']),2)
        self.assertEqual(d['submissions'][0]['decisions'],[])
        self.assertEqual(d['submissions'][1]['decisions'][0]['comment'],'需要补充新版资料')

    def test_configuration_recall_and_stale_versions(self):
        self.config()
        self.submit()
        original=self.detail()
        self.assertEqual(self.config(location='新地点').status_code,409)
        self.assertEqual(self.config(location='新地点',recall_pending=True,fields=[dict(id='new_requirement',label='新增必填',type='text',required=True)]).status_code,200)
        current=self.detail()
        self.assertEqual(current['node']['status'],'in_progress')
        self.assertEqual(current['node']['config']['version'],2)
        old=original['submissions'][0]
        r=self.bob.post('/api/process/nodes/'+self.ids[0]+'/review',json=dict(expected_version=current['node']['state_version'],submission_id=old['id'],decision='approved'))
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.submit(answers={}).status_code,422)
        self.assertEqual(self.submit(answers={'new_requirement':'新版本内容'}).status_code,200)
        self.assertEqual(self.detail()['submissions'][1]['snapshot']['fields'][0]['id'],'summary')

    def test_dependencies_reject_cycles_and_cross_product_nodes(self):
        self.assertEqual(self.config().status_code,200)
        self.assertEqual(self.config(1,dependencies=[self.ids[0]]).status_code,200)
        self.assertEqual(self.config(dependencies=[self.ids[1]]).status_code,409)
        self.assertEqual(self.submit(1).status_code,409)
        self.assertEqual(self.config(dependencies=[str(uuid4())]).status_code,422)
        self.assertEqual(self.config(dependencies=[self.ids[0]]).status_code,422)

    def test_concurrent_submissions_do_not_overwrite(self):
        self.config()
        version=self.detail()['node']['state_version']
        def save(label):
            client=self.client()
            client.cookies.update(self.alice.cookies)
            return client.post('/api/process/nodes/'+self.ids[0]+'/work',json=dict(action='save',expected_version=version,answers={'summary':label})).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(save,['A','B']))
        self.assertEqual(sorted(results),[200,409])
        self.assertIn(self.detail()['answers']['summary'],['A','B'])

    def test_upload_download_and_attachment_isolation(self):
        self.config()
        self.config(1)
        path='/api/process/nodes/'+self.ids[0]+'/attachments'
        self.assertEqual(self.outsider.post(path,files={'file':('secret.txt',b'private')}).status_code,403)
        r=self.alice.post(path,files={'file':('../../report.txt',b'valid deliverable')})
        self.assertEqual(r.status_code,201,r.text)
        file=r.json()
        self.assertEqual(file['title'],'report.txt')
        self.assertEqual(self.alice.get('/api/process/attachments/'+file['id']).content,b'valid deliverable')
        self.assertEqual(self.client().get('/api/process/attachments/'+file['id']).status_code,401)
        self.assertEqual(self.submit(1,answers={'summary':'test','files':[file]}).status_code,422)
        self.assertEqual(self.submit(answers={'summary':'test','files':[file]}).status_code,200)
        self.assertEqual(self.alice.post(path,files={'file':('x.txt',b'x')}).status_code,409)

    def test_reopen_invalidates_started_downstream_without_erasing_snapshots(self):
        self.config(review_mode='any')
        self.config(1,dependencies=[self.ids[0]],review_mode='any')
        for index in range(2):
            self.assertEqual(self.submit(index).status_code,200)
            self.assertEqual(self.decide(index=index).status_code,200)
        d=self.detail()
        response=self.admin.post('/api/process/nodes/'+self.ids[0]+'/work',json=dict(action='reopen',expected_version=d['node']['state_version'],reason='前置资料发生变更'))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['affected'],2)
        self.assertEqual(self.detail(1)['node']['status'],'in_progress')
        self.assertIsNone(self.detail(1)['node']['completed_at'])
        self.assertEqual(len(self.detail(1)['submissions']),1)
        self.assertEqual(self.submit(1).status_code,409)

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

    def test_completed_legacy_node_can_be_reopened_then_configured(self):
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET status='completed',completed_at=now() WHERE id=%s", (self.ids[0],))
            conn.commit()
        self.assertEqual(self.config().status_code,409)
        response=self.admin.post('/api/process/nodes/'+self.ids[0]+'/work',json=dict(action='reopen',expected_version=0,reason='为旧节点补充新的验收项目'))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.config().status_code,200)
