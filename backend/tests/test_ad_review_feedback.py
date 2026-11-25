"""Signed prior evidence on future plans, using authenticated HTTP and real PostgreSQL transactions."""
import hashlib
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1', 'requires disposable PostgreSQL')
class AdFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'))
        from app.db import connection
        from app.main import app
        from fastapi.testclient import TestClient
        from psycopg.types.json import Jsonb
        self.conn, self.json = connection, Jsonb
        self.users, self.clients, self.products = {}, {}, []
        self.today = datetime.now(UTC).date().isoformat()
        self.entity = 'profile:100201/target:300401'
        with connection() as db:
            for role in ('owner','reviewer','bystander'):
                uid = db.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',
                                 (uuid4().hex+'@test.example',role)).fetchone()['id']
                db.execute("INSERT INTO workflow_accounts(user_id,password_hash) VALUES(%s,'unused')",(uid,))
                token = secrets.token_urlsafe(32)
                db.execute("INSERT INTO workflow_sessions VALUES(%s,%s,now()+interval '1 hour')",
                           (hashlib.sha256(token.encode()).hexdigest(),uid))
                self.users[role] = uid
                self.clients[role] = TestClient(app, headers={'X-Workflow-Request':'1'},
                                                cookies={'homa_workflow_session':token})
        self.product = self.new_product('store-A')

    def tearDown(self):
        with self.conn() as db:
            for pid in self.products:
                db.execute('DELETE FROM products WHERE id=%s',(pid,))
            for uid in self.users.values():
                db.execute('DELETE FROM app_users WHERE id=%s',(uid,))
        for client in self.clients.values():
            client.close()

    def new_product(self, account):
        with self.conn() as db:
            pid = db.execute("""INSERT INTO products(model,name,marketplace,data_account_key,business_timezone)
                VALUES(%s,'test','US',%s,'UTC') RETURNING id""",(uuid4().hex,account)).fetchone()['id']
        self.products.append(pid)
        return pid

    def source(self, *, product=None, entity=None, action='调整竞价', site='US', owner='owner', reviewer='reviewer'):
        with self.conn() as db:
            return db.execute("""INSERT INTO workbench_items(product_id,page_id,section_id,site,object_ref,title,
                  owner_id,reviewer_id,values,definition,status,business_timezone)
                 VALUES(%s,'ads','change',%s,'same display name','test change',%s,%s,%s,'{}','approved','UTC')
                 RETURNING id""",(product or self.product,site,self.users[owner],self.users[reviewer],
                         self.json(dict(entity_id=entity or self.entity,change_kind=action,
                                        previous_setting='USD 1',proposed_setting='USD 1.15')))).fetchone()['id']

    def request(self, path, *, role='owner', code=200, **body):
        response = self.clients[role].post(path, json={'request_id':str(uuid4()),**body})
        self.assertEqual(response.status_code,code,response.text)
        return response.json()

    def plan_body(self, **extra):
        return dict(expected='同范围复验',baseline='冻结历史区间',evidence='内部来源01',metric_basis='operational',
                    observation_end=self.today,review_date=self.today,attribution_days=0,
                    control_method='randomized',control_evidence='留出组 A 与 B；随机分配凭证 7',**extra)

    def plan_path(self, sid):
        return f'/api/products/{self.product}/ads-reviews/sources/{sid}/plan'

    def feedback(self, sid, product=None, role='owner', status=200):
        path = f'/api/products/{product or self.product}/ads-review-feedback/sources/{sid}'
        response = self.clients[role].get(path)
        self.assertEqual(response.status_code,status,response.text)
        return response.json()

    def assess(self, review, version, **fields):
        assessment = dict(as_of=self.today,refreshed_on=self.today,execution_ok=True,guardrail_ok=True,
            actual='同期经营贡献改善',interference='已确认无额外促销',guardrails='库存与现金均足',
            evidence='实际报告 03',outcome='effective',incremental=True,effect_direction='positive',
            material_interference='none',confidence='high',control_result='保留随机组报告与经营贡献对照')
        return self.request(f'/api/products/{self.product}/ads-reviews/{review}/assess',role='reviewer',
                            expected_version=version,**{**assessment,**fields})

    def signed(self, **fields):
        sid = self.source()
        self.request(self.plan_path(sid),**self.plan_body())
        self.request(f'/api/workbench/items/{sid}/actions',action='execute',expected_version=1,evidence='真实回执的测试夹具')
        review = self.request(f'/api/products/{self.product}/ads-reviews/sources/{sid}/start')
        return sid,self.assess(review['id'],review['version'],**fields)

    def test_candidates_require_scope_acl_and_matching_future_metric(self):
        eid, prior = self.signed()
        sid = self.source()
        advice = self.feedback(sid)
        self.assertEqual(advice['state'],'candidate')
        self.assertEqual(advice['reference']['review_id'],prior['id'])
        self.assertEqual(advice['calibration']['tested_setting'],'USD 1.15')
        self.feedback(sid,role='bystander',status=404)
        foreign = self.new_product('store-B')
        self.feedback(sid,product=foreign,status=404)
        self.assertEqual(self.feedback(self.source(product=foreign),product=foreign)['state'],'clear')
        self.assertEqual(self.feedback(self.source(owner='bystander',reviewer='owner'),role='bystander')['state'],'clear')
        self.assertEqual(self.feedback(self.source(entity='same display name'))['state'],'identity_missing')
        self.assertEqual(self.feedback(self.source(site='CA'))['state'],'identity_missing')
        self.assertEqual(self.feedback(self.source(entity='profile:100201/keyword:300401'))['state'],'clear')
        self.assertEqual(self.feedback(self.source(action='暂停投放'))['state'],'clear')
        self.assertEqual(self.feedback(self.source(entity='profile:999/target:300401'))['state'],'clear')
        for action in ('新增定向','精准否定','调整广告位'):
            self.assertEqual(self.feedback(self.source(entity='profile:100201/campaign:300401',action=action))['state'],
                             'identity_missing')
        body = self.plan_body(calibration_version=advice['candidate_version'])
        self.request(self.plan_path(sid),code=422,**{**body,'metric_basis':'traffic'})
        self.request(self.plan_path(sid),code=422,**{**body,'control_method':'none','control_evidence':''})
        with self.conn() as db:
            db.execute("UPDATE products SET data_account_key='reassigned-store' WHERE id=%s",(self.product,))
        self.assertEqual(self.feedback(sid)['state'],'clear')
        self.request(self.plan_path(sid),code=409,**body)
        self.assertEqual(self.feedback(eid)['identity']['account_key'],'store-A')

    def test_reopen_invalidates_token_and_reassessment_is_distinct(self):
        _, prior = self.signed()
        sid = self.source()
        token = self.feedback(sid)['candidate_version']
        reopened = self.request(f"/api/products/{self.product}/ads-reviews/{prior['id']}/reopen",
                                expected_version=prior['version'],reason='数据来源有新修订')
        self.assertEqual(self.feedback(sid)['state'],'hold')
        self.request(self.plan_path(sid),code=409,**self.plan_body(calibration_version=token))
        self.assess(prior['id'],reopened['version'])
        self.assertNotEqual(self.feedback(sid)['candidate_version'],token)
        self.request(self.plan_path(sid),code=409,**self.plan_body(calibration_version=token))

    def test_parallel_adoption_frozen_receipt_and_later_pending(self):
        _, prior = self.signed()
        items = [self.source(),self.source()]
        token = self.feedback(items[0])['candidate_version']
        bodies = [dict(request_id=str(uuid4()),**self.plan_body(calibration_version=token)) for _ in items]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda t:self.clients['owner'].post(self.plan_path(t[0]),json=t[1]),zip(items,bodies)))
        self.assertEqual(sorted(r.status_code for r in responses),[200,409])
        i = next(i for i,r in enumerate(responses) if r.status_code==200)
        self.assertTrue(responses[i].json()['plan']['feedback']['applied'])
        self.assertEqual(self.feedback(items[i])['evidence_status'],'current')
        self.assertEqual(self.clients['owner'].post(self.plan_path(items[i]),json=bodies[i]).json(),responses[i].json())
        later = self.source()
        self.assertEqual(self.feedback(later)['state'],'observe')
        self.request(f"/api/products/{self.product}/ads-reviews/{prior['id']}/reopen",
                     expected_version=prior['version'],reason='对照来源回补')
        self.assertEqual(self.feedback(later)['state'],'hold')
        self.assertTrue(self.feedback(items[i])['applied'])
        self.assertTrue(self.feedback(items[i])['frozen'])
        self.assertEqual(self.feedback(items[i])['evidence_status'],'revised')
        self.assertIn('执行前',self.feedback(items[i])['evidence_message'])
        self.request(f'/api/workbench/items/{items[i]}/actions',action='execute',expected_version=1,evidence='已登记回执')
        self.assertIn('保留执行当时',self.feedback(items[i])['evidence_message'])
        with self.conn() as db:
            db.execute("UPDATE products SET data_account_key='another-shop' WHERE id=%s",(self.product,))
        restricted = self.feedback(items[i])
        self.assertEqual(restricted['evidence_status'],'unavailable')
        self.assertIsNone(restricted['reference'])

    def test_negative_guardrail_followup_and_no_detected_effect(self):
        _, prior = self.signed(outcome='ineffective',effect_direction='negative',guardrail_ok=False)
        sid = self.source()
        self.assertEqual(self.feedback(sid)['state'],'hold')
        self.assertIsNone(self.feedback(sid)['candidate_version'])
        reopened = self.request(f"/api/products/{self.product}/ads-reviews/{prior['id']}/reopen",
                                expected_version=prior['version'],reason='更正区间')
        self.assess(prior['id'],reopened['version'],outcome='ineffective',effect_direction='no_detected_effect',incremental=True)
        self.assertEqual(self.feedback(sid)['state'],'observe')
        sf = self.source(entity='profile:204/target:999')
        self.request(self.plan_path(sf),**self.plan_body())
        # Older historical plans in the fixture are deliberately overdue.
        with self.conn() as db:
            yesterday=(datetime.now(UTC).date()-timedelta(days=1)).isoformat()
            db.execute("UPDATE ads_review_plans SET plan=jsonb_set(plan,'{review_date}',to_jsonb(%s::text)) WHERE source_id=%s",
                       (yesterday,sf))
        self.assertEqual(self.feedback(self.source(entity='profile:204/target:999'))['state'],'hold')
