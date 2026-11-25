"""Receiving through authenticated FastAPI endpoints and real PostgreSQL transactions."""
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from uuid import uuid4


BASE = '/api/erp-receiving'


class WarehouseFixture:
    @classmethod
    def setUpClass(cls):
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('Only use the isolated workflow test database')
        from fastapi.testclient import TestClient
        from app.db import connection
        from app.main import app
        from app.workflow_auth import password_hash

        cls.clients, cls.ids = {}, {}
        cls.anonymous = TestClient(app)
        user_ids = []
        def cleanup_warehouse():
            with connection() as conn:
                # All test purchase orders and manual shop registrations use unique fixture identities.
                order_ids = [r['id'] for r in conn.execute('SELECT id FROM erp_receiving_orders WHERE created_by=ANY(%s)', (user_ids,))]
                notice_ids = [r['id'] for r in conn.execute('SELECT id FROM erp_receiving_notices WHERE order_id=ANY(%s)', (order_ids,))]
                scope_ids = [r['id'] for r in conn.execute('''SELECT s.id FROM erp_receiving_scopes s
                    JOIN workbench_shop_scopes w USING(account_ref,site) WHERE w.registered_by=ANY(%s)''', (user_ids,))]
                conn.execute('DELETE FROM erp_receiving_requests WHERE actor_id=ANY(%s)',(user_ids,))
                for table in ('erp_receiving_ledger','erp_receiving_events','erp_receiving_notice_lines'):
                    conn.execute('DELETE FROM '+table+' WHERE notice_id=ANY(%s)',(notice_ids,))
                for table in ('erp_receiving_notices','erp_receiving_order_lines'):
                    conn.execute('DELETE FROM '+table+' WHERE order_id=ANY(%s)',(order_ids,))
                conn.execute('DELETE FROM erp_receiving_orders WHERE id=ANY(%s)',(order_ids,))
                conn.execute('DELETE FROM erp_receiving_members WHERE scope_id=ANY(%s)',(scope_ids,))
                conn.execute('DELETE FROM erp_receiving_scopes WHERE id=ANY(%s)',(scope_ids,))
                conn.execute('DELETE FROM workbench_shop_scopes WHERE registered_by=ANY(%s)',(user_ids,))
                conn.execute('DELETE FROM app_users WHERE id=ANY(%s)',(user_ids,))
        cls.addClassCleanup(cleanup_warehouse)
        password = secrets.token_urlsafe(24)
        hashed = password_hash(password)
        for role in ('admin','warehouse','outside'):
            email = f'{uuid4().hex}@receiving.test'
            with connection() as conn:
                user = conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',
                                    (email, role)).fetchone()['id']
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',
                             (user, hashed, role=='admin'))
            user_ids.append(user)
            client = TestClient(app, headers={'X-Workflow-Request':'1'})
            login = client.post('/api/workflow-auth/login', json={'email':email,'password':password})
            if login.status_code != 200:
                raise AssertionError(login.text)
            cls.clients[role], cls.ids[role] = client, str(user)

    def post(self, path, payload, status=200, who='warehouse'):
        body = {'request_id':str(uuid4()), **payload}
        response = self.clients[who].post(BASE+path, json=body)
        self.assertEqual(response.status_code,status,response.text)
        return response.json()

    def setUp(self):
        self.scope_body = {'account_ref':'shop-'+uuid4().hex,'site':'US','warehouse':'W1',
                           'business_timezone':'America/Los_Angeles','member_ids':[self.ids['warehouse']]}
        self.scope = self.post('/scopes', self.scope_body, 201, 'admin')
        self.po_body = {'number':'PO-test','scope_id':self.scope['id'],'supplier':'检验供应商',
                        'lines':[{'sku':'A','qty':100},{'sku':'B','qty':40}]}
        self.order = self.post('/orders', self.po_body, 201, 'admin')
        self.order_id = self.order['id']
        self.a, self.b = [line['id'] for line in self.order['lines']]
        self.notices = f'/orders/{self.order_id}/notices'

    def notice(self, lines):
        return self.post(self.notices, {'lines':lines}, 201)['notices'][-1]['id']


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1', 'requires a disposable PostgreSQL database')
class ReceivingTests(WarehouseFixture, unittest.TestCase):
    def test_two_deliveries_partial_quality_and_compensation(self):
        first = self.notice([{'line_id':self.a, 'qty':60}])
        second = self.notice([{'line_id':self.a, 'qty':40}, {'line_id':self.b, 'qty':40}])
        self.post(self.notices,{'lines':[{'line_id':self.a,'qty':1}]},409)
        receive=f'/notices/{first}/receive'
        body={'mode':'normal','lines':[{'line_id':self.a,'qty':60}],'request_id':str(uuid4())}
        first_result=self.post(receive,body)
        first_event=first_result['notices'][0]['events'][0]['id']
        self.assertEqual(first_result['notices'][0]['lines'][0]['pending'],60)
        self.assertEqual(len(self.post(receive,body)['ledger']),1)
        self.post(receive, {**body,'mode':'quick'},409)
        self.post(receive, {**body,'request_id':str(uuid4())},409)
        quality=f'/notices/{first}/quality'
        self.post(quality,{'mode':'waive','lines':[{'line_id':self.a,'good':15,'bad':0}]})
        result=self.post(quality,{'mode':'inspect','lines':[{'line_id':self.a,'good':42,'bad':3}],'note':'验货照片已登记'})
        n=result['notices'][0]
        self.assertEqual([n['lines'][0][k] for k in ('received','pending','good','bad')],[60,0,57,3])
        self.post(quality,{'mode':'inspect','lines':[{'line_id':self.a,'good':1,'bad':0}]},409)
        self.post(f'/events/{first_event}/reverse',{'reason':'先回滚质检'},409,'admin')
        for e in reversed(n['events'][1:]):
            self.post('/events/'+e['id']+'/reverse',{'reason':'纠正检验'},200,'admin')
        rolled=self.post(f'/events/{first_event}/reverse',{'reason':'重新点收'},200,'admin')
        self.assertEqual(rolled['notices'][0]['status'],'waiting')
        self.assertEqual([sum(row[k] for row in rolled['ledger']) for k in ('pending_delta','good_delta','bad_delta')],[0,0,0])
        self.post(f'/events/{first_event}/reverse',{'reason':'重复'},409,'admin')
        posted=self.post(f'/notices/{second}/receive',{'mode':'quick','lines':[
            {'line_id':self.a,'qty':35,'good':34,'bad':1},{'line_id':self.b,'qty':20,'good':20,'bad':0}]})
        self.assertEqual([line['unnotified'] for line in posted['lines']],[5,20])
        self.assertEqual(posted['notices'][1]['lines'][0]['pending'],0)

    def test_uncounted_items_release_and_historical_notice_capacity(self):
        n=self.notice([{'line_id':self.a,'qty':100},{'line_id':self.b,'qty':5}])
        received=self.post(f'/notices/{n}/receive',{'mode':'normal','lines':[{'line_id':self.a,'qty':50}]})
        self.assertEqual([line['unnotified'] for line in received['lines']],[50,40])
        later=self.notice([{'line_id':self.a,'qty':50}])
        event=received['notices'][0]['events'][0]['id']
        self.post(f'/events/{event}/reverse',{'reason':'重开会超占'},409,'admin')
        self.post(f'/notices/{later}/cancel',{'reason':'供应商取消剩余发货'})
        again=self.post(f'/events/{event}/reverse',{'reason':'现可重新点收'},200,'admin')
        self.assertEqual(again['notices'][0]['status'],'waiting')
        self.assertEqual(again['lines'][0]['unnotified'],0)

    def test_foreign_rows_and_scoped_grants(self):
        self.assertEqual(self.anonymous.get(BASE+'/orders').status_code,401)
        outside=self.clients['outside']
        self.assertNotIn(self.order_id,[r['id'] for r in outside.get(BASE+'/orders').json()['items']])
        self.assertEqual(outside.get(BASE+'/orders/'+self.order_id).status_code,404)
        self.post(self.notices,{'lines':[{'line_id':self.a,'qty':1}]},404,'outside')
        self.post('/orders',self.po_body,403)
        self.post('/orders',self.po_body,409,'admin')
        second=self.post('/orders',{**self.po_body,'number':'other-PO'},201,'admin')
        n=self.notice([{'line_id':self.a,'qty':10}])
        self.post(f'/notices/{n}/receive',{'mode':'quick','lines':[
            {'line_id':self.a,'qty':4,'good':4,'bad':0},{'line_id':second['lines'][0]['id'],'qty':1,'good':1,'bad':0}]},422)
        self.assertEqual(self.clients['warehouse'].get(BASE+'/orders/'+self.order_id).json()['ledger'],[])
        self.post(f'/notices/{n}/receive',{'mode':'quick','lines':[{'line_id':self.a,'qty':4,'good':3,'bad':0}]},422)
        body={'mode':'normal','lines':[{'line_id':self.a,'qty':4}], 'request_id':str(uuid4())}
        done=self.post(f'/notices/{n}/receive',body)
        event=done['notices'][0]['events'][0]['id']
        self.post('/events/'+event+'/reverse',{'reason':'无管理权限'},403)
        self.post('/scopes',{**self.scope_body,'member_ids':[]},201,'admin')
        self.post(f'/notices/{n}/receive',body,404)
        self.assertEqual(self.clients['warehouse'].get(BASE+'/orders/'+self.order_id).status_code,404)

    def test_competing_notices_and_duplicate_retries(self):
        def insert(request_id):
            return self.clients['warehouse'].post(BASE+self.notices,json={
                'request_id':request_id,'lines':[{'line_id':self.a,'qty':80}]}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(insert,[str(uuid4()),str(uuid4())])),[201,409])
        key=str(uuid4())
        def add_b(_):
            return self.clients['warehouse'].post(BASE+self.notices,json={
                'request_id':key,'lines':[{'line_id':self.b,'qty':25}]}).status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(add_b,range(4))), [201]*4)
        detail=self.clients['warehouse'].get(BASE+'/orders/'+self.order_id).json()
        self.assertEqual(len(detail['notices']),2)
        self.assertEqual([line['unnotified'] for line in detail['lines']],[20,15])

    def test_concurrent_posting_updates_stock_once(self):
        n=self.notice([{'line_id':self.a,'qty':60}])
        def send(path, payload):
            return self.clients['warehouse'].post(BASE+path,json={'request_id':str(uuid4()),**payload}).status_code
        def receive(_):
            return send(f'/notices/{n}/receive',{'mode':'normal','lines':[{'line_id':self.a,'qty':60}]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(receive,range(2))),[200,409])
        def inspect(_):
            return send(f'/notices/{n}/quality',{'mode':'inspect','lines':[{'line_id':self.a,'good':35,'bad':5}]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(inspect,range(2))),[200,409])
        detail=self.clients['warehouse'].get(BASE+'/orders/'+self.order_id).json()
        self.assertEqual(len(detail['ledger']),2)
        self.assertEqual([detail['notices'][0]['lines'][0][k] for k in ('received','pending','good','bad')],[60,20,35,5])
