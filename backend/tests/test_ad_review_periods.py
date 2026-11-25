"""Exercise period queues against PostgreSQL event histories and real session cookies."""
import os
import unittest
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from test_erp_receiving import WarehouseFixture


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1', 'requires disposable PostgreSQL')
class AdReviewPeriodTests(WarehouseFixture, unittest.TestCase):
    def setUp(self):
        from app.db import connection
        from psycopg.types.json import Jsonb
        self.db = connection
        self.zone = ZoneInfo('America/Los_Angeles')

        def dt(day):
            return datetime(2024,1,day,23,45,tzinfo=self.zone)

        with connection() as conn:
            self.product = conn.execute("""INSERT INTO products(model,name,marketplace,business_timezone)
                VALUES(%s,'周期广告产品','US','America/Los_Angeles') RETURNING id""",(uuid4().hex,)).fetchone()['id']
            self.addCleanup(self.remove_product)

            def product_item(title, parent=None):
                return conn.execute("""INSERT INTO workbench_items(product_id,page_id,section_id,site,object_ref,title,
                    owner_id,reviewer_id,definition,parent_id,status) VALUES(%s,'ads','change','US',%s,%s,%s,%s,'{}',%s,'executed')
                    RETURNING id""",(self.product,uuid4().hex,title,self.ids['warehouse'],self.ids['admin'],parent)).fetchone()['id']

            def work_event(item,day,version,action,title,status,evidence=''):
                snapshot=Jsonb(dict(title=title,status=status,owner_id=str(self.ids['warehouse']),
                                    reviewer_id=str(self.ids['admin'])))
                conn.execute("""INSERT INTO workbench_events(item_id,version,actor_id,action,evidence,snapshot,created_at,request_hash)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'period-test')""",(item,version,self.ids['warehouse'],action,evidence,snapshot,dt(day)))

            self.source=product_item('较晚标题')
            self.other=product_item('待开始')
            self.no_proof=product_item('无执行凭证')
            work_event(self.source,4,1,'execute','原执行标题','executed','原单凭证 X')
            work_event(self.source,10,2,'edit','较晚标题','executed')
            work_event(self.other,7,1,'execute','无复盘启动','executed','凭证 Y')
            work_event(self.no_proof,7,1,'execute','不进分母','executed','   ')
            conn.execute("""INSERT INTO ads_review_plans(source_id,basis,plan,recorded_at)
                VALUES(%s,'prospective',%s,%s)""",(self.source,Jsonb({'review_date':'2024-01-06'}),dt(4)))
            self.review=conn.execute("""INSERT INTO ads_decision_reviews(source_id,execution,created_at)
                VALUES(%s,'{}',%s) RETURNING id""",(self.source,dt(5))).fetchone()['id']
            self.child=product_item('后续修订标题',self.source)
            work_event(self.child,8,1,'create','原策略候选','draft')
            work_event(self.child,10,2,'verify','后续修订标题','closed')

            def event(day,version,action,payload):
                conn.execute("""INSERT INTO ads_review_events(review_id,version,actor_id,action,payload,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s)""",(self.review,version,self.ids['admin'],action,Jsonb(payload),dt(day)))

            event(5,1,'start',{})
            event(8,2,'assess',{'outcome':'inconclusive','as_of':'2024-01-07','next_review_date':'2024-01-09'})
            event(9,3,'link',{'id':str(self.child),'kind':'strategy_candidate'})
            event(10,4,'reopen',{'reason':'新凭证'})
        self.path=f'/api/products/{self.product}/ads-review-periods'

    def remove_product(self):
        with self.db() as conn:
            conn.execute('DELETE FROM products WHERE id=%s',(self.product,))

    def period(self,day,period='day',role='warehouse',**query):
        response=self.clients[role].get(self.path,params={'as_of':f'2024-01-{day:02}','period':period,**query})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    @staticmethod
    def metrics(report):
        return {m['key']:(m['n'],m['N']) for m in report['metrics']}

    def test_as_of_late_assessment_reopen_scope_and_time_zone(self):
        early=self.period(7)
        self.assertEqual(early['timezone'],'America/Los_Angeles')
        self.assertEqual(self.metrics(early)['started'],(1,2))
        self.assertEqual(self.metrics(early)['due_open'],(1,1))
        self.assertEqual(self.metrics(early)['insufficient'],(0,0))
        self.assertEqual({r['kind'] for r in early['items']},{'not_started','due','open'})
        self.assertNotIn('较晚标题',str(early))
        signed=self.period(8)
        self.assertEqual(self.metrics(signed)['insufficient'],(1,1))
        self.assertEqual(self.metrics(signed)['conclusive'],(0,1))
        self.assertNotIn('strategy_candidate',{r['kind'] for r in signed['items']})
        week=self.period(9,'week')
        self.assertEqual((week['period_start'],week['period_end']),('2024-01-08','2024-01-09'))
        self.assertEqual(self.metrics(week)['candidates'],(1,1))
        candidate=next(r for r in week['items'] if r['kind']=='strategy_candidate')
        self.assertEqual((candidate['title'],candidate['owner_id'],candidate['status']),
                         ('原策略候选',str(self.ids['warehouse']),'draft'))
        self.assertIsNone(candidate['owner_name'])
        self.assertTrue(any(r['kind']=='next_review' for r in week['items']))
        reopened=self.period(10,'month')
        self.assertEqual(reopened['period_start'],'2024-01-01')
        self.assertEqual(self.metrics(reopened)['insufficient'],(0,0))
        self.assertEqual(self.metrics(reopened)['linked_open'],(0,1))
        self.assertEqual({r['title'] for r in reopened['items'] if r['kind']=='open'},{'较晚标题'})
        self.assertEqual(len({r['key'] for r in reopened['items']}),len(reopened['items']))

    def test_permissions_zero_denominators_pagination_and_future(self):
        self.assertEqual(self.anonymous.get(self.path).status_code,401)
        hidden=self.period(9,'month','outside')
        self.assertEqual(hidden['items'],[])
        self.assertTrue(all(m['N']==m['n']==0 for m in hidden['metrics']))
        admin=self.period(9,'month','admin')
        page=self.period(9,'month',limit=1)
        self.assertEqual(page['metrics'],admin['metrics'])
        self.assertEqual(page['total'],len(admin['items']))
        pages=[]
        while True:
            pages.extend(r['key'] for r in page['items'])
            if page['next_offset'] is None:
                break
            page=self.period(9,'month',limit=1,offset=page['next_offset'])
        self.assertEqual(pages,[r['key'] for r in admin['items']])
        future=datetime.now(self.zone).date()+timedelta(days=1)
        self.assertEqual(self.clients['warehouse'].get(self.path,params={'as_of':future}).status_code,422)
        self.assertEqual(self.clients['warehouse'].get(self.path,params={'period':'year'}).status_code,422)
        current=self.clients['warehouse'].get(self.path).json()
        self.assertEqual(current['as_of'],str(datetime.now(self.zone).date()))
        self.assertTrue(next(r for r in current['items'] if r['kind']=='open')['owner_name'])
        with self.db() as conn:
            conn.execute('UPDATE workbench_items SET owner_id=%s WHERE id=%s',(self.ids['outside'],self.source))
        self.assertNotIn(str(self.source),str(self.period(9,'month')['items']))

    def test_genuine_execution_assessment_and_follow_up_enter_queue(self):
        def post(who,path,**body):
            response=self.clients[who].post(path,json=dict(request_id=str(uuid4()),**body))
            self.assertLess(response.status_code,300,response.text)
            return response.json()

        api='/api/workbench/items'
        values={'entity_id':'CMP-P','change_kind':'调整预算','previous_setting':'10 USD','proposed_setting':'12 USD',
                'change_basis':'经营计划','stop_condition':'到达预算停止'}
        item=post('warehouse',api,product_id=str(self.product),page_id='ads',section_id='change',site='US',
                  title='真实周期流程',object_ref='CMP-P',values=values)
        path=api+'/'+item['id']
        self.clients['admin'].patch(path,json={'reviewer_id':self.ids['admin'],'expected_version':1}).raise_for_status()
        post('warehouse',path+'/actions',action='submit',expected_version=2)
        post('admin',path+'/actions',action='approve',expected_version=3)
        base=f'/api/products/{self.product}/ads-reviews'
        source_path=base+'/sources/'+item['id']
        today=str(datetime.now(self.zone).date())
        post('warehouse',source_path+'/plan',expected='提高CTR',baseline='原报告 P1',evidence='方案 P2',review_date=today,
             observation_end=today,attribution_days=0,metric_basis='traffic')
        post('warehouse',path+'/actions',action='execute',expected_version=4,evidence='原单人工执行凭证 P3')

        def rows():
            result=self.clients['admin'].get(self.path,params={'period':'month'})
            self.assertEqual(result.status_code,200,result.text)
            return {r['kind']:r for r in result.json()['items'] if r['source_id']==item['id']}
        self.assertEqual(set(rows()),{'not_started','due'})
        review=post('warehouse',source_path+'/start')
        self.assertEqual(set(rows()),{'open','due'})
        review_path=base+'/'+review['id']
        post('admin',review_path+'/assess',expected_version=1,as_of=today,refreshed_on=today,execution_ok=True,
             guardrail_ok=True,actual='来源报告 P4',interference='另有促销',material_interference='uncontrolled',
             confidence='low',guardrails='库存可用',evidence='P4 对账',outcome='inconclusive',incremental=False)
        self.assertEqual(set(rows()),{'conclusion','insufficient','owner_pending'})
        self.assertIn('同期干扰',rows()['insufficient']['reason'])
        child=post('warehouse',api,product_id=str(self.product),page_id='ads',section_id='change',site='US',
                   parent_id=item['id'],title='核对 P4',object_ref='CMP-P-FOLLOW',values=values)
        post('warehouse',review_path+'/links',kind='follow_up',item_id=child['id'],expected_version=2)
        current=rows()
        self.assertNotIn('owner_pending',current)
        self.assertEqual(current['follow_up']['item_id'],child['id'])
        self.assertEqual(current['follow_up']['owner_id'],self.ids['warehouse'])
        post('admin',review_path+'/reopen',reason='报告 P4 有后续修订',expected_version=3)
        self.assertEqual(set(rows()),{'open','due','follow_up'})

    def test_query_eligibility_uses_recorded_settings(self):
        from psycopg.types.json import Jsonb
        with self.db() as conn:
            conn.execute("""UPDATE workbench_items SET section_id='query_harvest',
                values=%s WHERE id=%s""",(Jsonb({'query_action':'建立精准承接'}),self.source))
            for n,action in [(1,'继续观察'),(2,'建立精准承接')]:
                conn.execute("""UPDATE workbench_events SET snapshot=jsonb_set(snapshot,'{values}',%s)
                    WHERE item_id=%s AND version=%s""",(Jsonb({'query_action':action}),self.source,n))
        self.assertNotIn(str(self.source),str(self.period(9,'month')['items']))
        self.assertIn(str(self.source),str(self.period(10,'month')['items']))

    def test_executed_planned_but_unstarted_is_a_due_obligation(self):
        from psycopg.types.json import Jsonb

        with self.db() as conn:
            conn.execute("""INSERT INTO ads_review_plans(source_id,basis,plan,recorded_at)
                VALUES(%s,'prospective',%s,%s)""", (self.other,Jsonb({'review_date':'2024-01-08'}),
                    datetime(2024,1,4,12,tzinfo=self.zone)))
            # A third witnessed execution has no registered plan or deadline.
            conn.execute("UPDATE workbench_events SET evidence='执行单 C' WHERE item_id=%s",(self.no_proof,))
        report=self.period(9,'month')
        metrics=self.metrics(report)
        self.assertEqual(metrics['due_closed'],(1,2))
        self.assertEqual(metrics['due_open'],(1,1))
        self.assertEqual(metrics['started'],(1,3))
        due=[r for r in report['items'] if r['kind']=='due']
        self.assertEqual([(r['source_id'],r['status'],r['review_id'],r['owner_id']) for r in due],
                         [(str(self.other),'not_started',None,str(self.ids['warehouse']))])
        unscheduled=[r for r in report['items'] if r['source_id']==str(self.no_proof)]
        self.assertEqual([(r['kind'],r['due_date']) for r in unscheduled],[('not_started',None)])
        prior=self.period(7,'week')
        self.assertEqual(self.metrics(prior)['due_closed'],(0,1))
        self.assertNotIn(str(self.other),[r['source_id'] for r in prior['items'] if r['kind']=='due'])

    def unresolved_weekly_case(self):
        with self.db() as conn:
            # The historical signing was inconclusive and set no next date.
            # The only real link until day 17 is a strategy candidate.
            conn.execute("UPDATE ads_review_events SET payload=payload-'next_review_date' WHERE review_id=%s AND action='assess'", (self.review,))
            conn.execute("DELETE FROM ads_review_events WHERE review_id=%s AND action='reopen'", (self.review,))
        pending=[r for r in self.period(15,'week')['items'] if r['kind']=='owner_pending']
        self.assertEqual([(r['source_id'],r['owner_id'],r['due_date']) for r in pending],
                         [(str(self.source),str(self.ids['warehouse']),None)])
        return pending[0]['key']

    def test_owner_obligation_survives_week_until_explicit_stop(self):
        from psycopg.types.json import Jsonb
        key=self.unresolved_weekly_case()
        self.assertNotIn('conclusion',[r['kind'] for r in self.period(15,'week')['items']])
        with self.db() as conn:
            for day,version,act,payload in ((17,4,'reopen',{'reason':'补记业务处置依据'}),
                    (18,5,'assess',{'outcome':'ineffective','execution_ok':True,'guardrail_ok':False,
                                    'stop_reason':'本产品停止销售，已核对剩余库存和预算。'})):
                conn.execute("""INSERT INTO ads_review_events(review_id,version,actor_id,action,payload,created_at)
                    VALUES(%s,%s,%s,%s,%s,%s)""",(self.review,version,self.ids['admin'],act,Jsonb(payload),
                          datetime(2024,1,day,11,tzinfo=self.zone)))
        self.assertIn(key,[r['key'] for r in self.period(16,'week')['items']])
        self.assertNotIn(key,[r['key'] for r in self.period(19,'week')['items']])

    def test_real_follow_up_discharges_only_after_link_event(self):
        from psycopg.types.json import Jsonb
        key=self.unresolved_weekly_case()
        with self.db() as conn:
            child=conn.execute("""INSERT INTO workbench_items(product_id,parent_id,page_id,section_id,
                site,object_ref,title,owner_id,definition) VALUES(%s,%s,'ads','change','US','P','后续风险处置',%s,'{}')
                RETURNING id""",(self.product,self.source,self.ids['warehouse'])).fetchone()['id']
            snap=Jsonb({'title':'后续风险处置','status':'draft','owner_id':str(self.ids['warehouse'])})
            when=datetime(2024,1,17,9,tzinfo=self.zone)
            conn.execute("""INSERT INTO workbench_events(item_id,version,actor_id,action,snapshot,request_hash,created_at)
                VALUES(%s,1,%s,'create',%s,'test',%s)""",(child,self.ids['warehouse'],snap,when))
            conn.execute("""INSERT INTO ads_review_events(review_id,version,actor_id,action,payload,created_at)
                VALUES(%s,4,%s,'link',%s,%s)""",(self.review,self.ids['warehouse'],
                        Jsonb({'kind':'follow_up','id':str(child)}),when))
        self.assertIn(key,[r['key'] for r in self.period(16,'week')['items']])
        report=self.period(18,'week')
        self.assertNotIn(key,[r['key'] for r in report['items']])
        self.assertTrue(any(r['kind']=='follow_up' and r['item_id']==str(child) for r in report['items']))
