"""Workspace business records with real PostgreSQL locks and authenticated requests."""
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from uuid import uuid4


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1', 'requires disposable workflow database')
class WorkbenchIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('A disposable workflow database is required')
        from fastapi.testclient import TestClient
        from app.db import connection
        from app.main import app
        from app.workflow_auth import password_hash
        cls.connection = staticmethod(connection)
        cls.anon = TestClient(app)
        cls.clients, cls.ids = {}, {}
        password = secrets.token_urlsafe(24)
        hashed = password_hash(password)
        for role in ('admin', 'owner', 'reviewer', 'other'):
            email = f'wb-{role}-{uuid4().hex}@test.example'
            with connection() as conn:
                member = conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',
                                      (email, role)).fetchone()['id']
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',
                             (member, hashed, role == 'admin'))
            client = TestClient(app, headers={'X-Workflow-Request':'1'})
            response = client.post('/api/workflow-auth/login', json={'email':email, 'password':password})
            assert response.status_code == 200, response.text
            cls.clients[role] = client
            cls.ids[role] = str(member)

    def setUp(self):
        with self.connection() as conn:
            self.product = str(conn.execute("INSERT INTO products(model,name,marketplace) VALUES(%s,'样品','US') RETURNING id",
                                            ('wb-' + uuid4().hex,)).fetchone()['id'])
        self.api = '/api/workbench/items'
        self.body = {'product_id':self.product, 'page_id':'overview', 'section_id':'risk_triage',
                     'object_ref':'sample-sku', 'title':'被阻塞事项', 'site':'US', 'request_id':str(uuid4()),
                     'values':{'risk_kind':'供货风险', 'last_action_date':'2026-11-24', 'evidence_summary':'物流票据'}}
        self.owner = self.clients['owner']

    def test_acl_review_evidence_history_and_return(self):
        self.assertEqual(self.anon.get(self.api).status_code, 401)
        self.assertEqual(self.owner.post(self.api, json={**self.body,'reviewer_id':self.ids['reviewer']}).status_code, 403)
        source = '供应商确认后再决定数量。' * 15 + '需在 11 月 25 日前安排下一步。'
        response = self.owner.post(self.api, json={**self.body,'source_note':source})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['source_note'],source)
        self.assertEqual(response.json()['history'][0]['snapshot']['source_note'],source)
        row = response.json()
        path = self.api + '/' + row['id']
        version = row['version']
        self.assertEqual(self.owner.patch(path,json={'expected_version':version,'source_note':'替换原文'}).status_code,422)
        self.assertEqual(self.owner.post(path+'/actions',json={'expected_version':version,'action':'submit'}).status_code,422)
        self.assertEqual(self.clients['other'].patch(path,json={'expected_version':version,'title':'新标题'}).status_code,403)
        self.assertEqual(self.clients['admin'].patch(path,json={'expected_version':version,'reviewer_id':self.ids['owner']}).status_code,422)
        result = self.clients['admin'].patch(path,json={'expected_version':version,'reviewer_id':self.ids['reviewer']})
        self.assertEqual(result.status_code, 200, result.text)
        version = result.json()['version']

        def action(actor, name, code=200, **kwargs):
            nonlocal version
            body = {'expected_version':version,'action':name,**kwargs}
            result = self.clients[actor].post(path+'/actions',json=body)
            self.assertEqual(result.status_code, code, result.text)
            if code == 200:
                version = result.json()['version']
                repeat = self.clients[actor].post(path+'/actions',json=body)
                self.assertEqual(repeat.status_code, 200, repeat.text)
                self.assertEqual(repeat.json()['version'], version)
        action('owner','submit')
        action('admin','approve',403)
        action('reviewer','return',422)
        action('reviewer','return',note='补充回执')
        action('owner','submit')
        action('reviewer','approve')
        action('owner','execute',422)
        action('owner','execute',evidence='采购回执 PO-210')
        action('reviewer','verify',note='逐项核实到货')
        result = self.owner.get(path).json()
        self.assertEqual(result['status'],'closed')
        self.assertEqual([e['version'] for e in result['history']], list(range(1,version+1)))
        self.assertEqual(result['history'][-2]['evidence'],'采购回执 PO-210')
        self.assertEqual(result['history'][-1]['snapshot']['status'],'closed')
        self.assertEqual(self.owner.patch(path,json={'expected_version':version,'title':'越过复核'}).status_code,409)
        listed = self.owner.get(self.api,params={'product_id':self.product,'page_id':'overview'}).json()['items']
        self.assertEqual([item['id'] for item in listed],[result['id']])

    def test_simultaneous_signal_adoption_and_stale_edits(self):
        signal = 'supply:' + uuid4().hex
        bodies = [{**self.body,'signal_key':signal,'request_id':str(uuid4())} for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.clients[role].post, self.api, json=body)
                       for role,body in zip(('owner','other'),bodies)]
            records = [f.result() for f in futures]
        for record in records:
            self.assertEqual(record.status_code, 201, record.text)
        row = records[0].json()
        self.assertEqual(row['id'], records[1].json()['id'])
        self.assertEqual(len(row['history']),1)
        self.assertEqual(self.owner.post(self.api,json={**bodies[0],'title':'同键不同内容'}).status_code,409)
        result = self.owner.post(self.api,json={**self.body,'signal_key':signal,'object_ref':'另一个SKU'})
        self.assertEqual(result.status_code,201,result.text)
        self.assertNotEqual(result.json()['id'],row['id'])
        self.assertEqual(self.owner.post(self.api,json={**self.body,'request_id':str(uuid4()),
                         'signal_key':signal,'object_ref':''}).status_code,422)
        path = self.api + '/' + row['id']
        owner = self.owner if row['owner_id'] == self.ids['owner'] else self.clients['other']
        self.assertEqual(owner.patch(path,json={'expected_version':1,'object_ref':'替换来源对象'}).status_code,422)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(owner.patch, path, json={'expected_version':1,'title':title}) for title in ('a','b')]
            self.assertEqual(sorted(f.result().status_code for f in futures),[200,409])
        self.assertEqual(len(owner.get(path).json()['history']),2)
        # Closing releases the business key. Previously seen requests still refer to their original item.
        action_version = 2
        self.clients['admin'].patch(path,json={'expected_version':action_version,'reviewer_id':self.ids['reviewer']}).raise_for_status()
        for actor,act in ((owner,'submit'), (self.clients['reviewer'],'approve'), (owner,'execute'),
                          (self.clients['reviewer'],'verify')):
            action_version += 1
            actor.post(path+'/actions',json={'expected_version':action_version,'action':act,
                       'evidence':'回执-432','note':'核验通过'}).raise_for_status()
        for role, body in zip(('owner','other'),bodies):
            repeat = self.clients[role].post(self.api,json=body)
            self.assertEqual(repeat.json()['id'], row['id'])
        new = self.owner.post(self.api,json={**bodies[0],'request_id':str(uuid4())})
        self.assertEqual(new.status_code,201,new.text)
        self.assertNotEqual(new.json()['id'],row['id'])

    def test_cross_page_followups_trace_source_and_deduplicate_per_object(self):
        from app.workbench_definitions import pages
        parent = self.owner.post(self.api, json=self.body).json()
        target = pages()['inventory']['sections'][0]['id']
        next_item = {**self.body, 'parent_id': parent['id'], 'page_id':'inventory', 'section_id':target,
                     'title':'跟进采购', 'object_ref':'LOT-77', 'values':{}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.clients[role].post, self.api,
                                   json={**next_item, 'request_id': str(uuid4())})
                       for role in ('owner','other')]
            responses = [future.result() for future in futures]
        for response in responses:
            self.assertEqual(response.status_code, 201, response.text)
        child = responses[0].json()
        self.assertEqual(child['id'], responses[1].json()['id'])
        self.assertEqual(child['parent_ref']['number'], parent['number'])
        self.assertEqual(child['history'][0]['snapshot']['parent_id'], parent['id'])
        parent_path = self.api + '/' + parent['id']
        source = self.owner.get(parent_path).json()
        self.assertEqual([r['id'] for r in source['child_refs']], [child['id']])
        self.assertEqual(source['version'], parent['version'])
        self.assertEqual(source['status'], parent['status'])
        with self.connection() as conn:
            foreign = conn.execute("INSERT INTO products(model,name,marketplace) VALUES(%s,'其他产品','US') RETURNING id",
                                   ('wb-'+uuid4().hex,)).fetchone()['id']
        response = self.owner.post(self.api, json={**next_item, 'product_id':str(foreign),'request_id':str(uuid4())})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.owner.patch(parent_path,json={'expected_version':1,'parent_id':parent['id']}).status_code,422)
        other = self.owner.post(self.api,json={**next_item, 'object_ref':'LOT-88', 'request_id':str(uuid4())}).json()
        self.assertNotEqual(other['id'], child['id'])
        conflict = self.owner.patch(self.api+'/'+other['id'],json={'expected_version':1,'object_ref':'LOT-77'})
        self.assertEqual(conflict.status_code,409,conflict.text)

    def test_input_scope_and_draft_validation(self):
        for changes in ({'site':'DE'},{'page_id':'unknown'},{'section_id':'unknown'},
                        {'task_id':str(uuid4())},{'values':{'unexpected':1}},
                        {'values':{'risk_kind':'other'}},{'values':{'last_action_date':'2025-02-29'}}):
            response = self.owner.post(self.api,json={**self.body,**changes})
            self.assertEqual(response.status_code,422,response.text)
        row = self.owner.post(self.api,json={**self.body,'values':{}}).json()
        path = self.api + '/' + row['id']
        self.clients['admin'].patch(path,json={'expected_version':1,'reviewer_id':self.ids['reviewer']}).raise_for_status()
        response = self.owner.post(path+'/actions',json={'expected_version':2,'action':'submit'})
        self.assertEqual(response.status_code,422,response.text)

    def test_rule_snapshot_blocks_contradictory_submission_and_sop_is_independent(self):
        from psycopg.types.json import Jsonb
        with self.connection() as conn:
            template = conn.execute('SELECT id FROM workflow_templates LIMIT 1').fetchone()['id']
            def sop(product):
                workflow = conn.execute('''INSERT INTO product_workflows(product_id,workflow_template_id,template_version)
                    VALUES(%s,%s,1) RETURNING id''',(product,template)).fetchone()['id']
                return str(conn.execute("""INSERT INTO tasks(product_workflow_id,stage_name,title,sequence)
                    VALUES(%s,'执行','独立SOP',1) RETURNING id""",(workflow,)).fetchone()['id'])
            task = sop(self.product)
            other = conn.execute("INSERT INTO products(model,name,marketplace) VALUES(%s,'同名样品','US') RETURNING id",
                                 ('wb-'+uuid4().hex,)).fetchone()['id']
            foreign = sop(other)
        self.assertEqual(self.owner.post(self.api,json={**self.body,'task_id':foreign}).status_code,422)
        row = self.owner.post(self.api,json={**self.body,'task_id':task}).json()
        # A versioned template may have older business rules than the live form.
        definition = row['definition']
        definition['checks'] = [{'op':'block_if','keys':['risk_kind'],'when':'供货风险',
                                 'label':'风险复核','message':'先确认供货依据'}]
        with self.connection() as conn:
            conn.execute('UPDATE workbench_items SET definition=%s WHERE id=%s',(Jsonb(definition),row['id']))
        path = self.api + '/' + row['id']
        self.clients['admin'].patch(path,json={'expected_version':1,'reviewer_id':self.ids['reviewer']}).raise_for_status()
        response = self.owner.post(path+'/actions',json={'expected_version':2,'action':'submit'})
        self.assertEqual(response.status_code,422)
        self.assertIn('先确认供货依据',response.text)
        with self.connection() as conn:
            self.assertEqual(conn.execute('SELECT status FROM tasks WHERE id=%s',(task,)).fetchone()['status'],'not_started')

    def test_shop_scope_without_product_review_and_separate_stores(self):
        seller = 'seller-' + uuid4().hex
        manual = '运营二店-' + uuid4().hex
        with self.connection() as conn:
            source = conn.execute("""INSERT INTO operating_connections
                (name,provider,account_key,region,marketplace,marketplace_id,currency,time_zone,env_prefix)
                VALUES('店铺A','sp_sales',%s,'NA','US','ATVPDKIKX0DER','USD','America/Los_Angeles','PRIVATE_ONLY')
                RETURNING id""", (seller,)).fetchone()
        choices = self.owner.get('/api/workbench/shop-scopes').json()['scopes']
        self.assertIn({'account_ref': seller, 'site':'US', 'business_timezone':'America/Los_Angeles',
                       'connection_id':str(source['id'])}, choices)
        self.assertNotIn('PRIVATE_ONLY',str(choices))
        self.assertEqual(self.anon.get('/api/workbench/shop-scopes').status_code,401)
        base = {'product_id':None, 'page_id':'shop','section_id':'notice','object_ref':'PF-123',
                'title':'受限销售通知','signal_key':'notice-PF-123',
                'values':{'notice_ref':'PF-123','notice_date':'2026-11-10','affected_scope':'北美销售权限',
                          'next_follow_up':'2026-11-15','notice_evidence':'平台绩效通知已脱敏'}}
        def create(**kwargs):
            return self.owner.post(self.api,json={**base,'request_id':str(uuid4()),**kwargs})

        selected = {'connection_id':str(source['id'])}
        explicit = {'account_ref':manual,'site':'US','business_timezone':'America/New_York'}
        for invalid in ({**selected,'account_ref':manual}, {**selected,'site':'JP'},
                        {**explicit,'business_timezone':'No/Such_Zone'}, {**explicit,'site':''},
                        {**explicit,'page_id':'overview','section_id':'risk_triage','values':{}},
                        {**explicit,'task_id':str(uuid4())}, {**explicit,'product_id':self.product}):
            result = create(**invalid)
            self.assertEqual(result.status_code,422,result.text)

        a = create(**selected)
        b = create(**explicit)
        for response in (a,b):
            self.assertEqual(response.status_code,201,response.text)
            self.assertIsNone(response.json()['product_id'])
        a,b = a.json(),b.json()
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(a['account_ref'],seller)
        self.assertEqual(b['history'][0]['snapshot']['account_ref'],manual)
        self.assertEqual(b['history'][0]['snapshot']['business_timezone'],explicit['business_timezone'])
        self.assertIn({**explicit,'connection_id':None},self.owner.get('/api/workbench/shop-scopes').json()['scopes'])
        self.assertEqual(create(**selected).json()['id'],a['id'])
        self.assertEqual(create(**explicit).json()['id'],b['id'])
        for invalid in ({**explicit,'business_timezone':'Europe/London'},
                        {**explicit,'account_ref':seller}):
            result = create(**invalid)
            self.assertEqual(result.status_code,422,result.text)

        child = {'parent_id':b['id'],'section_id':'case','object_ref':'CASE-923','values':{},'signal_key':None}
        for wrong_scope in (selected, {**explicit,'site':'CA'}, {'product_id':self.product}):
            result = create(**wrong_scope,**child)
            self.assertEqual(result.status_code,422,result.text)
        next_item = create(**explicit,**child)
        self.assertEqual(next_item.status_code,201,next_item.text)
        self.assertEqual(next_item.json()['parent_ref']['account_ref'],manual)
        a_path = self.api + '/' + a['id']
        b_path = self.api + '/' + b['id']
        self.assertEqual(self.owner.get(a_path).json()['child_refs'],[])
        self.assertEqual(self.owner.get(b_path).json()['child_refs'][0]['id'],next_item.json()['id'])
        same_market_other_store = self.owner.get(self.api,params={'scope':'shop','account_ref':seller,'site':'US'}).json()['items']
        self.assertEqual([r['id'] for r in same_market_other_store],[a['id']])
        market_filter = self.owner.get(self.api,params={'scope':'shop','account_ref':seller,'site':'CA'}).json()['items']
        self.assertEqual(market_filter,[])
        self.assertNotIn(b['id'],[r['id'] for r in self.owner.get(self.api,params={'product_id':self.product}).json()['items']])
        self.assertEqual(self.owner.patch(b_path,json={'expected_version':1,'site':'JP'}).status_code,422)
        self.assertEqual(self.owner.patch(b_path,json={'expected_version':1,'task_id':str(uuid4())}).status_code,422)
        self.clients['admin'].patch(b_path,json={'expected_version':1,'reviewer_id':self.ids['reviewer']}).raise_for_status()
        for version,(role,action) in enumerate((('owner','submit'),('reviewer','approve'),('owner','execute'),
                                                ('reviewer','verify')), start=2):
            result = self.clients[role].post(b_path+'/actions',json={'expected_version':version,'action':action,
                                             'evidence':'Case 号-923','note':'审核凭证与范围'})
            self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['history'][-1]['snapshot']['status'],'closed')
        self.assertEqual(result.json()['history'][-1]['snapshot']['account_ref'],manual)
