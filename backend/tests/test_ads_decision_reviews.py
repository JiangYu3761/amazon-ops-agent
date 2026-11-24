"""Exercise the genuine workbench execution flow and review permissions on disposable PostgreSQL."""
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4


def request(**values):
    return {'request_id':str(uuid4()), **values}


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION')=='1','requires a disposable database')
class AdsReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('Disposable database required')
        from fastapi.testclient import TestClient
        from app.db import connection
        from app.main import app
        from app.workflow_auth import password_hash
        cls.db=staticmethod(connection)
        cls.anonymous=TestClient(app)
        cls.clients,cls.ids={},{}
        password=secrets.token_urlsafe(24)
        hashed=password_hash(password)
        def cleanup_accounts():
            with connection() as conn:
                conn.execute('DELETE FROM app_users WHERE id=ANY(%s::uuid[])',(list(cls.ids.values()),))
        cls.addClassCleanup(cleanup_accounts)
        for who in ('owner','reviewer','other','admin'):
            with connection() as conn:
                email=uuid4().hex+'@ads.example'
                uid=conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',(email,who)).fetchone()['id']
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',(uid,hashed,who=='admin'))
            client=TestClient(app,headers={'X-Workflow-Request':'1'})
            client.post('/api/workflow-auth/login',json={'email':email,'password':password}).raise_for_status()
            cls.clients[who],cls.ids[who]=client,str(uid)

    def setUp(self):
        with self.db() as conn:
            self.product=str(conn.execute("""INSERT INTO products(model,name,marketplace,business_timezone)
                VALUES(%s,'广告复盘样品','US','UTC') RETURNING id""",(uuid4().hex,)).fetchone()['id'])
        def cleanup_product():
            with self.db() as conn:
                conn.execute('DELETE FROM products WHERE id=%s',(self.product,))
        self.addCleanup(cleanup_product)
        self.base=f'/api/products/{self.product}/ads-reviews'
        self.today=datetime.now(timezone.utc).date()
        self.plan=request(expected='点击率提升',baseline='调整前周同范围 CTR 1.2%，报告 BL-1',
                          evidence='审批附件 PL-2',review_date=str(self.today),observation_end=str(self.today),
                          attribution_days=0,metric_basis='traffic',control_method='randomized',control_evidence='分组登记 RND-4')
        self.result=request(expected_version=1,as_of=str(self.today),refreshed_on=str(self.today),
            execution_ok=True,guardrail_ok=True,actual='试验 2.1%，对照 1.3%',interference='无同期开价',
            material_interference='none',confidence='high',guardrails='预算及利润下限通过',evidence='复核文件 R-3',
            outcome='effective',incremental=True,control_result='曝光随机分组，报告 C1 与 T1')
        self.wb='/api/workbench/items'

    def post(self,role,path,body,code=200):
        r=self.clients[role].post(path,json=body)
        self.assertEqual(r.status_code,code,r.text)
        return r.json()

    def source(self,approve=True):
        source=self.post('owner',self.wb,request(product_id=self.product,page_id='ads',section_id='change',site='US',
            title='广告出价调整',object_ref='CMP-105',
            values={'entity_id':'CMP-105','change_kind':'调整预算','previous_setting':'10 USD','proposed_setting':'12 USD',
                    'change_basis':'已批准策略','stop_condition':'成本超过 20 USD 暂停'}),201)
        path=self.wb+'/'+source['id']
        s=self.clients['admin'].patch(path,json=request(expected_version=1,reviewer_id=self.ids['reviewer']))
        self.assertEqual(s.status_code,200,s.text)
        if approve:
            self.post('owner',path+'/actions',request(action='submit',expected_version=2))
            source=self.post('reviewer',path+'/actions',request(action='approve',expected_version=3))
        return source

    def execute(self,source):
        return self.post('owner',self.wb+'/'+source['id']+'/actions',
                         request(action='execute',expected_version=source['version'],evidence='本地测试平台回执 TRACE-105'))

    def test_preregister_assess_reopen_links_and_scope(self):
        source=self.source()
        path=self.base+'/sources/'+source['id']
        self.post('reviewer',path+'/plan',self.plan,403)
        frozen=self.post('owner',path+'/plan',self.plan)['plan']
        self.assertEqual(frozen['source_version'],4)
        self.assertEqual(self.post('owner',path+'/plan',self.plan)['plan'],frozen)
        self.post('owner',path+'/plan',dict(self.plan,expected='事后替换'),409)
        self.post('owner',path+'/start',request(),409)
        self.execute(source)
        start=request()
        with ThreadPoolExecutor(2) as pool:
            answers=list(pool.map(lambda _:self.post('owner',path+'/start',start),range(2)))
        first=answers[0]
        self.assertEqual(first['id'],answers[1]['id'])
        self.assertEqual(first['basis'],'prospective')
        self.assertIn('TRACE-105',first['execution']['evidence'])
        self.assertEqual(first['plan'],frozen)
        detail=self.base+'/'+first['id']
        self.assertEqual(self.anonymous.get(detail).status_code,401)
        self.assertEqual(self.clients['other'].get(detail).status_code,404)
        self.assertEqual(self.clients['other'].get(self.base+'/sources').json()['items'],[])
        self.assertEqual(self.clients['other'].get(self.base).json()['items'],[])
        self.assertEqual(self.clients['admin'].get(detail).status_code,200)
        self.assertEqual(self.clients['owner'].get(detail.replace(self.product,str(uuid4()))).status_code,404)
        self.assertEqual(self.clients['reviewer'].get(detail).headers['cache-control'],'no-store')
        self.post('owner',detail+'/assess',self.result,403)
        self.post('admin',detail+'/assess',self.result,403)
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.post('reviewer',detail+'/assess',self.result),range(2)))
        self.assertEqual([x['version'] for x in results],[2,2])
        self.assertTrue(results[0]['assessment']['mature'])
        self.assertEqual(results[0]['assessment']['verification'],'reviewer_attested')
        self.assertEqual(self.clients['owner'].get(self.base,params={'status':'closed'}).json()['items'][0]['id'],first['id'])
        child=self.post('owner',self.wb,request(product_id=self.product,page_id='ads',section_id='change',
            object_ref='CMP-106',title='申请下一轮策略',parent_id=source['id']),201)
        self.post('owner',detail+'/links',request(expected_version=2,kind='follow_up',item_id=source['id']),422)
        linked=self.post('owner',detail+'/links',request(expected_version=2,kind='strategy_candidate',item_id=child['id']))
        self.assertEqual((linked['links'][0]['owner_id'],linked['links'][0]['id']),(self.ids['owner'],child['id']))
        rev=self.post('owner',detail+'/reopen',request(expected_version=3,reason='广告报告补录'))
        self.assertEqual([e['action'] for e in rev['history']],['start','assess','link','reopen'])
        self.assertIsNone(rev['assessment'])
        self.assertEqual(rev['history'][-1]['payload']['prior_assessment']['actual'],self.result['actual'])
        corrected=dict(self.result,request_id=str(uuid4()),expected_version=4,actual='校正后样本不足',
                       outcome='inconclusive',incremental=False)
        self.assertEqual(self.post('reviewer',detail+'/assess',corrected)['version'],5)
        self.assertEqual(len(self.clients['owner'].get(detail).json()['links']),1)

    def test_retrospective_maturity_and_outcome_limits(self):
        source=self.source()
        path=self.base+'/sources/'+source['id']
        self.execute(source)
        self.post('owner',path+'/plan',self.plan,409)
        self.post('owner',path+'/start',request(),422)
        self.post('owner',path+'/start',dict(self.plan,metric_basis='attributed'),422)
        first=self.post('owner',path+'/start',dict(self.plan,attribution_days=7,metric_basis='attributed'))
        detail=self.base+'/'+first['id']
        self.assertEqual(first['basis'],'retrospective')
        self.assertEqual(first['matures_on'],str(self.today+timedelta(days=7)))
        self.post('reviewer',detail+'/assess',self.result,422)
        self.post('reviewer',detail+'/assess',dict(self.result,as_of=str(self.today+timedelta(days=10))),422)
        waiting=dict(self.result,outcome='inconclusive',incremental=False,execution_ok=False)
        result=self.post('reviewer',detail+'/assess',waiting)
        self.assertFalse(result['assessment']['mature'])
        self.assertEqual(result['assessment']['outcome'],'inconclusive')

    def test_concurrent_distinct_assessments_and_evidence_gates(self):
        source=self.execute(self.source())
        path=self.base+'/sources/'+source['id']
        row=self.post('owner',path+'/start',self.plan)
        detail=self.base+'/'+row['id']
        self.post('reviewer',detail+'/assess',self.result,422)  # No hindsight claim of incrementality.
        positive=dict(self.result,incremental=False)
        for changes in ({'execution_ok':False},{'guardrail_ok':False},{'material_interference':'uncontrolled'},
                        {'material_interference':'controlled','control_result':''},{'confidence':'low'},
                        {'refreshed_on':None},{'as_of':str(self.today-timedelta(days=1))}):
            self.post('reviewer',detail+'/assess',dict(positive,**changes),422)
        bodies=[dict(positive,request_id=str(uuid4()),actual=f'签署结果 {i}') for i in range(2)]
        with ThreadPoolExecutor(2) as pool:
            responses=list(pool.map(lambda b:self.clients['reviewer'].post(detail+'/assess',json=b),bodies))
        self.assertEqual(sorted(r.status_code for r in responses),[200,409])
        self.assertEqual(len(self.clients['reviewer'].get(detail).json()['history']),2)

    def test_postponed_execution_and_missing_witness(self):
        source=self.source()
        path=self.base+'/sources/'+source['id']
        stale=dict(self.plan,observation_end=str(self.today-timedelta(days=2)))
        self.post('owner',path+'/plan',stale,422)
        self.execute(source)
        r=self.post('owner',path+'/start',stale)
        self.post('reviewer',self.base+'/'+r['id']+'/assess',dict(self.result,incremental=False),422)
        bad=self.source()
        with self.db() as conn:
            conn.execute("UPDATE workbench_items SET status='executed' WHERE id=%s",(bad['id'],))
        self.post('owner',self.base+'/sources/'+bad['id']+'/start',self.plan,409)


    def test_negative_experiment_is_retained_when_guardrail_fails(self):
        source=self.source()
        path=self.base+'/sources/'+source['id']
        self.post('owner',path+'/plan',self.plan)
        self.execute(source)
        row=self.post('owner',path+'/start',request())
        result=dict(self.result,outcome='ineffective',guardrail_ok=False,guardrails='同组成本超限',
                    actual='实验组低于对照组',effect_direction='negative')
        self.post('reviewer',self.base+'/'+row['id']+'/assess',dict(result,effect_direction=None),422)
        signed=self.post('reviewer',self.base+'/'+row['id']+'/assess',result)['assessment']
        self.assertTrue(signed['incremental'])
        self.assertEqual(signed['effect_direction'],'negative')
        self.assertFalse(signed['guardrail_ok'])

    def test_investigation_and_observation_are_excluded(self):
        source=self.source()
        with self.db() as conn:
            conn.execute("UPDATE workbench_items SET section_id='investigation' WHERE id=%s",(source['id'],))
        self.assertEqual(self.clients['owner'].get(self.base+'/sources').json()['items'],[])
        self.post('owner',self.base+'/sources/'+source['id']+'/plan',self.plan,404)
        with self.db() as conn:
            conn.execute("""UPDATE workbench_items SET section_id='query_harvest',
                            values='{"query_action":"保留并继续观察"}' WHERE id=%s""",(source['id'],))
        self.assertEqual(self.clients['owner'].get(self.base+'/sources').json()['items'],[])

    def test_unresolved_result_stays_with_owner_until_real_follow_up_or_reason(self):
        source=self.execute(self.source())
        row=self.post('owner',self.base+'/sources/'+source['id']+'/start',self.plan)
        detail=self.base+'/'+row['id']
        unresolved=dict(self.result,outcome='inconclusive',execution_ok=False,incremental=False)
        tomorrow=str(self.today+timedelta(days=1))
        self.post('reviewer',detail+'/assess',dict(unresolved,stop_reason='停止跟踪',next_review_date=tomorrow),422)
        signed=self.post('reviewer',detail+'/assess',unresolved)
        self.assertEqual(signed['disposition'],'owner_pending')
        self.assertEqual(signed['assessment']['disposition'],'owner_pending')
        self.assertEqual(self.clients['owner'].get(self.base).json()['items'][0]['disposition'],'owner_pending')
        for kind,expected in [('strategy_candidate','owner_pending'),('follow_up','linked')]:
            child=self.post('owner',self.wb,request(product_id=self.product,page_id='ads',section_id='change',
                object_ref=kind, title='安排后续工作',parent_id=source['id']),201)
            signed=self.post('owner',detail+'/links',request(expected_version=signed['version'],kind=kind,item_id=child['id']))
            self.assertEqual(signed['disposition'],expected)
            self.assertEqual(signed['assessment']['disposition'],'owner_pending')
        opened=self.post('owner',detail+'/reopen',request(expected_version=signed['version'],reason='补报需复核'))
        stopped=self.post('reviewer',detail+'/assess',dict(unresolved,request_id=str(uuid4()),
                        expected_version=opened['version'],stop_reason='产品停止销售，审核人确认终止观察'))
        self.assertEqual(stopped['disposition'],'stopped')
        self.assertEqual(stopped['history'][-1]['payload']['stop_reason'],'产品停止销售，审核人确认终止观察')
