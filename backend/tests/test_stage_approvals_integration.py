"""Real stage-handoff tests; only the disposable integration runner may enable them."""
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION')=='1','requires disposable approval database')
class StageApprovalIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from urllib.parse import urlsplit
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('Never run against a business database')
        from fastapi.testclient import TestClient
        from app.main import app
        from app.db import connection
        from app.workflow_auth import password_hash
        cls.app=app;cls.Client=TestClient;cls.connection=staticmethod(connection)
        password=secrets.token_urlsafe(24);hashed=password_hash(password)
        cls.ids={};cls.clients={}
        with connection() as conn:
            for key in ['admin','applicant','handler','reviewer','second','outsider']:
                row=conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',('stage-'+key+'@test.example','审批测试-'+key)).fetchone()
                cls.ids[key]=str(row['id'])
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',(row['id'],hashed,key=='admin'))
            conn.commit()
        for key in cls.ids:
            client=TestClient(app,headers={'X-Workflow-Request':'1'})
            result=client.post('/api/workflow-auth/login',json={'email':'stage-'+key+'@test.example','password':password})
            assert result.status_code==200,result.text
            cls.clients[key]=client

    def setUp(self):
        with self.connection() as conn:
            self.product=conn.execute("INSERT INTO products(model,name,marketplace) VALUES(%s,'审批测试产品','US') RETURNING id",('approval-'+uuid4().hex,)).fetchone()['id']
            template=conn.execute('SELECT id FROM workflow_templates LIMIT 1').fetchone()['id']
            workflow=conn.execute('INSERT INTO product_workflows(product_id,workflow_template_id,template_version) VALUES(%s,%s,1) RETURNING id',(self.product,template)).fetchone()['id']
            self.task=str(conn.execute("INSERT INTO tasks(product_workflow_id,stage_name,title,sequence,module_code,sop_key,evidence_required,approval_required) VALUES(%s,'产品与合规','FCC 认证',1,'compliance','fcc-certification',true,true) RETURNING id",(workflow,)).fetchone()['id'])
            conn.commit()
        self.path='/api/approvals/tasks/'+self.task

    def get(self,actor='applicant'):
        response=self.clients[actor].get(self.path)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def configure(self,**updates):
        d=self.get('admin');stages=[]
        keys=['key','name','type','fields','assignee_ids','reviewer_ids','collaborator_ids','review_mode','planned_start','planned_end','return_to']
        for raw in d['config']['stages']:
            s={k:v for k,v in raw.items() if k in keys}
            s['assignee_ids']=[self.ids['applicant'] if s['type']=='application' else self.ids['handler']]
            s['reviewer_ids']=[self.ids['reviewer']] if s['type']=='approval' else []
            s['planned_start']='2026-11-17';s['planned_end']='2026-11-30'
            stages.append(s)
        body={'expected_version':d['config']['version'],'fields':d['config']['fields'],'stages':stages}
        body.update(updates)
        return self.clients['admin'].put(self.path+'/config',json=body)

    def answers(self,fields):
        result={}
        for f in fields:
            if not f.get('required'):continue
            kind=f['type']
            result[f['id']]=({'number':0,'date':'2026-11-16','select':(f.get('options') or ['核对完成'])[0],
                'multiselect':(f.get('options') or ['核对完成'])[:1],'person':self.ids['handler'],
                'attachment':[{'kind':'link','title':'测试交付材料','url':'https://example.com/approval-evidence','version':'v1'}]}).get(kind,'已核实 '+f['label'])
        return result

    def action(self,action=None,actor=None,**updates):
        d=self.get();case=d['case'];stage=next((s for s in case['stages'] if s['key']==case['current_stage_key']),None) if case else d['config']['stages'][0]
        actor=actor or ('reviewer' if stage['type']=='approval' else 'applicant' if stage['type']=='application' else 'handler')
        action=action or ('approve' if stage['type']=='approval' else 'submit' if stage['type']=='application' else 'complete')
        payload={'expected_version':case['version'] if case else 0,'stage_key':stage['key'],'action':action,'answers':self.answers(stage['fields']),'comment':'已核对本次提交'}
        if stage['type']=='application':payload['application_answers']=self.answers(d['config']['fields'])
        payload.update(updates)
        return self.clients[actor].post(self.path+'/actions',json=payload)

    def must_action(self,*args,**kwargs):
        r=self.action(*args,**kwargs);self.assertEqual(r.status_code,200,r.text);return r

    def test_preview_has_complete_fcc_template_without_an_instance(self):
        d=self.get()
        self.assertIsNone(d['case']);self.assertEqual(len(d['config']['stages']),7)
        self.assertEqual([s['key'] for s in d['config']['stages']],['application','manager_review','testing','draft_feedback','draft_review','original','archive_review'])
        self.assertTrue(d['config']['fields']);self.assertFalse(d['permissions']['configure'])
        self.assertEqual(d['config']['stages'][4]['return_to'],'draft_feedback')

    def test_full_fcc_handoff_and_only_final_acceptance_completes_task(self):
        self.assertEqual(self.configure().status_code,200)
        self.must_action();self.assertEqual(self.get()['case']['current_stage_key'],'manager_review')
        self.assertEqual(self.action(actor='admin').status_code,403)
        self.must_action();self.assertEqual(self.get()['case']['current_stage_key'],'testing')
        self.must_action('accept');self.assertEqual(self.get()['case']['stages'][2]['status'],'in_progress')
        self.must_action(external_event_at='2026-11-16T09:30:00+08:00')
        self.must_action();self.must_action();self.must_action()
        d=self.get();self.assertEqual(d['case']['current_stage_key'],'archive_review');self.assertNotEqual(d['task']['status'],'completed')
        self.must_action()
        d=self.get();self.assertEqual(d['case']['status'],'completed');self.assertEqual(d['task']['status'],'completed')
        self.assertTrue(all(s['completed_at'] for s in d['case']['stages']))
        self.assertTrue(any(e['action']=='accepted' for e in d['history']))
        self.assertEqual(d['case']['stages'][2]['external_event_at'],'2026-11-16T09:30:00+08:00')

    def test_rejection_targets_draft_feedback_and_preserves_round_history(self):
        self.configure()
        for _ in range(4):self.must_action()
        self.assertEqual(self.get()['case']['current_stage_key'],'draft_review')
        self.must_action('reject',comment='补充型号及测试结论')
        d=self.get();self.assertEqual(d['case']['current_stage_key'],'draft_feedback')
        self.assertEqual(d['case']['stages'][3]['status'],'returned')
        rejected=next(e for e in d['history'] if e['action']=='rejected')
        self.assertEqual(rejected['payload']['comment'],'补充型号及测试结论')
        self.must_action();self.must_action()
        self.assertEqual(self.get()['case']['current_stage_key'],'original')
        rounds=[e for e in self.get()['history'] if e['stage_key']=='draft_feedback' and e['action']=='handled']
        self.assertEqual(len(rounds),2)

    def test_admin_only_configuration_and_legacy_write_guards(self):
        self.assertEqual(self.clients['outsider'].put(self.path+'/config',json={}).status_code,403)
        self.assertEqual(self.configure().status_code,200)
        self.assertEqual(self.action(actor='outsider').status_code,403)
        self.assertEqual(self.clients['admin'].post('/api/process/nodes/'+self.task+'/work',json={'action':'reopen','expected_version':0,'reason':'bypass'}).status_code,409)
        self.assertEqual(self.clients['admin'].patch('/api/tasks/'+self.task+'/status',json={'status':'completed'}).status_code,409)
        self.assertEqual(self.clients['admin'].post('/api/tasks/'+self.task+'/evidence',json={'evidence_type':'note','uri':'bypass'}).status_code,409)

    def test_original_plan_survives_forecast_and_configuration_changes(self):
        self.configure();self.must_action('save',forecast_end='2026-12-02',delay_reason='等待补样',next_action='安排寄样')
        first=self.get()['case']['stages'][0]
        self.assertEqual(first['baseline_end'],'2026-11-30');self.assertEqual(first['forecast_end'],'2026-12-02')
        workspace=self.clients['admin'].get('/api/workspace').json()
        self.assertTrue(next(t for t in workspace['tasks'] if t['id']==self.task)['approval_summary']['overdue'])
        d=self.get();stages=[]
        for s in d['config']['stages']:
            stages.append({k:v for k,v in s.items() if k in ['key','name','type','fields','assignee_ids','reviewer_ids','collaborator_ids','review_mode','planned_start','planned_end','return_to']})
        stages[0]['planned_end']='2026-12-03'
        response=self.configure(stages=stages,reason='机构排期调整')
        self.assertEqual(response.status_code,200,response.text)
        stage=self.get()['case']['stages'][0];self.assertEqual(stage['baseline_end'],'2026-11-30')
        self.assertTrue(any(e['action']=='configured' and e['payload']['reason']=='机构排期调整' for e in self.get()['history']))

    def test_in_progress_configuration_requires_explicit_recall(self):
        self.configure();self.must_action();self.must_action()
        self.assertEqual(self.configure().status_code,409)
        response=self.configure(recall_pending=True,reason='调整办理与审核分工')
        self.assertEqual(response.status_code,200,response.text)
        d=self.get();self.assertEqual(d['case']['status'],'draft');self.assertEqual(d['case']['current_stage_key'],'application')
        snapshots=[e for e in d['history'] if e['action']=='configured' and e['payload'].get('previous_case')]
        self.assertEqual(snapshots[0]['payload']['previous_case']['current_stage_key'],'testing')

    def test_concurrent_save_rejects_stale_version(self):
        self.configure();payload={'expected_version':0,'stage_key':'application','action':'save','answers':{},'application_answers':{}}
        def save(_):return self.clients['applicant'].post(self.path+'/actions',json=payload).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(save,range(2)))
        self.assertEqual(sorted(results),[200,409])

    def test_attachment_isolation_and_authenticated_download(self):
        self.configure()
        r=self.clients['applicant'].post(self.path+'/attachments',data={'stage_key':'application'},files={'file':('../../FCC.txt',b'FCC test')})
        self.assertEqual(r.status_code,201,r.text);file=r.json();self.assertEqual(file['title'],'FCC.txt')
        self.assertEqual(self.clients['reviewer'].get('/api/approvals/attachments/'+file['id']).content,b'FCC test')
        self.assertEqual(self.Client(self.app).get('/api/approvals/attachments/'+file['id']).status_code,401)
        self.must_action()
        d=self.get();stage=d['case']['stages'][1]
        values=self.answers(stage['fields']);attachment_field=next((f for f in stage['fields'] if f['type']=='attachment'),None)
        if attachment_field:
            values[attachment_field['id']]=[file]
            self.assertEqual(self.action(answers=values).status_code,422)
        else:
            self.assertEqual(self.clients['applicant'].post(self.path+'/attachments',data={'stage_key':'application'},files={'file':('late.txt',b'x')}).status_code,409)

    def test_first_review_locks_answers_for_other_reviewers(self):
        d=self.get();stages=[]
        for s in d['config']['stages']:
            item={k:v for k,v in s.items() if k in ['key','name','type','fields','review_mode','planned_start','planned_end','return_to']}
            item['assignee_ids']=[self.ids['applicant'] if s['type']=='application' else self.ids['handler']]
            item['reviewer_ids']=[self.ids['reviewer'],self.ids['second']] if s['type']=='approval' else []
            stages.append(item)
        self.assertEqual(self.configure(stages=stages).status_code,200)
        self.must_action();self.must_action()
        d=self.get();self.assertEqual(d['case']['current_stage_key'],'manager_review')
        stage=d['case']['stages'][1];changed=self.answers(stage['fields']);key=next(k for k,v in changed.items() if isinstance(v,str));changed[key]='篡改已审核内容'
        self.assertEqual(self.action(actor='second',answers=changed).status_code,409)
        self.must_action(actor='second')
        self.assertEqual(self.get()['case']['current_stage_key'],'testing')

    def test_configuration_cannot_return_to_cc_or_finish_with_unreviewed_work(self):
        from app.approvals import Configure
        from pydantic import ValidationError
        common={'expected_version':0,'fields':[]}
        with self.assertRaises(ValidationError):
            Configure(**common,stages=[{'key':'a','name':'申请','type':'application'},{'key':'b','name':'审核','type':'approval'},{'key':'c','name':'后续办理','type':'handling'}])
        with self.assertRaises(ValidationError):
            Configure(**common,stages=[{'key':'a','name':'申请','type':'application'},{'key':'b','name':'抄送','type':'cc'},{'key':'c','name':'最终审核','type':'approval','return_to':'b'}])

    def test_completed_legacy_is_readonly_until_administrator_reopens(self):
        with self.connection() as conn:
            conn.execute("UPDATE tasks SET status='completed',completed_at=now() WHERE id=%s",(self.task,));conn.commit()
        self.assertEqual(self.configure().status_code,409)
        self.assertEqual(self.action('save').status_code,409)
        r=self.clients['admin'].post(self.path+'/actions',json={'expected_version':0,'action':'reopen','comment':'补充新的认证流程'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.configure().status_code,200)

    def test_migrated_compliance_ids_survive_repeated_sop_completion(self):
        product='20000000-0000-0000-0000-000000000001'
        def ids():
            with self.connection() as conn:
                return [(r['sop_key'],str(r['id'])) for r in conn.execute("SELECT t.sop_key,t.id FROM tasks t JOIN product_workflows pw ON pw.id=t.product_workflow_id WHERE pw.product_id=%s AND t.sop_key IN ('fcc-certification','dangerous-goods','label-confirmation') ORDER BY t.sop_key",(product,)).fetchall()]
        before=ids();self.assertEqual(len(before),3)
        first=self.clients['admin'].post('/api/products/'+product+'/workflow')
        self.assertEqual(first.status_code,200,first.text)
        second=self.clients['admin'].post('/api/products/'+product+'/workflow')
        self.assertEqual(second.status_code,200,second.text)
        self.assertEqual(second.json()['added'],0)
        self.assertEqual(ids(),before)

    def test_any_and_sequential_review_modes_on_stage_engine(self):
        for mode in ['any','sequential']:
            if mode=='sequential':
                with self.connection() as conn:
                    source=conn.execute('SELECT product_workflow_id FROM tasks WHERE id=%s',(self.task,)).fetchone()
                    self.task=str(conn.execute("INSERT INTO tasks(product_workflow_id,stage_name,title,sequence,module_code) VALUES(%s,'合规','另一审批测试',2,'compliance') RETURNING id",(source['product_workflow_id'],)).fetchone()['id']);conn.commit()
                self.path='/api/approvals/tasks/'+self.task
            d=self.get();selected=[d['config']['stages'][0],d['config']['stages'][-1]];stages=[]
            for raw in selected:
                s={k:v for k,v in raw.items() if k in ['key','name','type','fields']}
                s.update(assignee_ids=[self.ids['applicant']],reviewer_ids=[self.ids['reviewer'],self.ids['second']] if s['type']=='approval' else [],review_mode=mode)
                if s['type']=='approval':s['return_to']=selected[0]['key']
                stages.append(s)
            self.assertEqual(self.configure(stages=stages).status_code,200)
            self.must_action()
            if mode=='sequential':
                self.assertEqual(self.action(actor='second').status_code,409)
                self.must_action(actor='reviewer');self.assertEqual(self.get()['case']['status'],'in_progress')
            self.must_action(actor='second')
            self.assertEqual(self.get()['case']['status'],'completed')

    def test_anonymous_detail_exposes_only_template_not_case_or_email(self):
        self.configure();self.must_action('save')
        response=self.Client(self.app).get(self.path)
        self.assertEqual(response.status_code,200)
        d=response.json();self.assertTrue(d['has_case']);self.assertIsNone(d['case'])
        self.assertEqual(d['history'],[]);self.assertEqual(d['legacy'],{})
        self.assertTrue(all('email' not in p for p in d['people']))
        self.assertEqual(len(d['config']['stages']),7)
