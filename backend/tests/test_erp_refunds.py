"""Local settlement ledger integration tests on an isolated PostgreSQL database."""
import hashlib
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import uuid4


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1', 'requires disposable PostgreSQL')
class OrderLineFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('A disposable test database is required')
        from fastapi.testclient import TestClient
        from app.db import connection
        from app.main import app
        from app.workflow_auth import COOKIE
        cls.db = staticmethod(connection)
        cls.base = '/api/erp-refunds/lines'
        cls.anon = TestClient(app)
        account_ids = []
        def remove_test_logins():
            with connection() as conn:
                conn.execute('DELETE FROM workflow_accounts WHERE user_id=ANY(%s)', (account_ids,))
        cls.addClassCleanup(remove_test_logins)
        for role in ('staff','other','admin'):
            token = secrets.token_hex(24)
            with connection() as conn:
                member = conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',
                                      (uuid4().hex+'@example.test',role)).fetchone()['id']
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',
                             (member,'unused',role=='admin'))
                conn.execute("INSERT INTO workflow_sessions VALUES (%s,%s,now()+interval '1 day')",
                             (hashlib.sha256(token.encode()).hexdigest(),member))
            account_ids.append(member)
            client = TestClient(app,headers={'X-Workflow-Request':'1'})
            client.cookies.set(COOKIE,token)
            setattr(cls,role,client)

    def setUp(self):
        self.seller = uuid4().hex
        with self.db() as conn:
            self.product = str(conn.execute("""INSERT INTO products(model,name,marketplace,currency,data_account_key)
                 VALUES(%s,'样品','US','USD',%s) RETURNING id""",(uuid4().hex,self.seller)).fetchone()['id'])
        self.body = {'product_id':self.product,'sku':'SKU-1','account_ref':self.seller,'order_ref':'O-1',
                     'line_ref':'item-1','currency':'USD','refundable_amount':'25.00'}
        self.entry = {'entry_ref':'TX-A/1','statement_ref':'STMT-1','currency':'USD','amount':'10.00',
                      'posted_on':'2025-11-01'}

    def register(self, **overrides):
        response = self.staff.post(self.base,json={**self.body,**overrides})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()


class RefundLedgerTests(OrderLineFixture):
    def test_mark_is_reversible_and_separate_from_two_settlement_rows(self):
        row = self.register()
        path = self.base + '/' + row['id']
        self.assertEqual(row['id'],self.register()['id'])
        marked = {'marked':True,'reason':'客服收到待核对邮件','expected_version':1}
        result = self.staff.put(path+'/mark',json=marked).json()
        self.assertEqual(result['state'],'marked')
        self.assertEqual(self.staff.put(path+'/mark',json=marked).json()['version'],2)
        clear = self.staff.put(path+'/mark',json={'marked':False,'reason':'尚无凭证','expected_version':2})
        self.assertEqual(clear.json()['state'],'unmarked')
        result = self.staff.put(path+'/mark',json={**marked,'expected_version':3}).json()
        self.assertEqual(result['version'],4)
        first = self.admin.post(path+'/entries',json=self.entry)
        self.assertEqual(first.status_code,201,first.text)
        self.assertEqual(first.json()['state'],'partial')
        two = self.admin.post(path+'/entries',json={**self.entry,'entry_ref':'TX-B/1','amount':'15.00'})
        self.assertEqual(two.json()['state'],'full')
        self.assertEqual(two.json()['entry_count'],2)
        self.assertEqual(Decimal(two.json()['recorded_amount']),Decimal('25'))
        self.assertEqual(self.staff.put(path+'/mark',json={'marked':False,'reason':'复核后清除人工标记',
                         'expected_version':two.json()['version']}).json()['state'],'full')
        result = self.staff.get(path,params={'limit':1}).json()
        self.assertEqual(result['source'],'manual_statement_reconciliation')
        self.assertFalse(result['marked'])
        self.assertEqual(result['entry_count'],2)
        self.assertEqual(result['entries']['next_offset'],1)
        page2 = self.staff.get(path,params={'limit':1,'offset':1,'mark_offset':1}).json()
        self.assertNotEqual(result['entries']['items'][0]['id'],page2['entries']['items'][0]['id'])
        self.assertEqual(page2['entries']['next_offset'],None)
        self.assertEqual(page2['marks']['items'][0]['reason'],'客服收到待核对邮件')
        self.assertEqual(self.admin.delete(path+'/entries/'+result['entries']['items'][0]['id']).status_code,404)
        self.assertEqual(self.staff.get(path).json()['state'],'full')

    def test_amount_currency_scope_and_permissions(self):
        self.assertEqual(self.anon.get(self.base).status_code,401)
        for change in ({'currency':'JPY'},{'refundable_amount':'0'},{'refundable_amount':'1.001'},
                       {'account_ref':'another'},{'currency':'US1'},{'line_ref':'  '},{'line_ref':'A\x00B'}):
            response = self.staff.post(self.base,json={**self.body,**change})
            self.assertEqual(response.status_code,422,response.text)
        row = self.register()
        path = self.base + '/' + row['id']
        self.assertEqual(self.staff.post(self.base,json={**self.body,'refundable_amount':'10'}).status_code,409)
        self.assertEqual(self.other.get(path).status_code,404)
        self.assertEqual(self.other.put(path+'/mark',json={'marked':True,'reason':'越权','expected_version':1}).status_code,404)
        self.assertEqual(self.other.get(self.base,params={'product_id':self.product}).json()['items'],[])
        self.assertEqual(self.staff.post(path+'/entries',json=self.entry).status_code,403)
        for change in ({'currency':'EUR'},{'amount':'-1'},{'amount':'0'},{'amount':'1.005'},
                       {'amount':'25.01'},{'statement_ref':''},{'origin':'amazon_verified'}):
            response = self.admin.post(path+'/entries',json={**self.entry,**change})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.staff.get(path).json()['entry_count'],0)
        second = self.register(line_ref='item-2')
        self.admin.post(path+'/entries',json=self.entry).raise_for_status()
        conflict = self.admin.post(self.base+'/'+second['id']+'/entries',json=self.entry)
        self.assertEqual(conflict.status_code,409)
        rows = self.admin.get(self.base,params={'account_ref':self.seller,'limit':1}).json()
        self.assertEqual(len(rows['items']),1)
        self.assertEqual(rows['next_offset'],1)
        rows = self.staff.get(self.base,params={'order_ref':'O-1','product_id':self.product,'state':'partial'}).json()
        self.assertEqual([r['id'] for r in rows['items']],[row['id']])
        self.assertEqual(self.staff.get(path).headers.get('cache-control'),'no-store')

    def test_concurrent_retries_and_overspend(self):
        row = self.register()
        path = self.base+'/'+row['id']+'/entries'
        with ThreadPoolExecutor(max_workers=2) as pool:
            duplicate = [pool.submit(self.admin.post,path,json=self.entry) for _ in range(2)]
            responses = [result.result() for result in duplicate]
            self.assertEqual([r.status_code for r in responses],[201,201])
            self.assertTrue(all(r.json()['entry_count']==1 for r in responses))
            more = [pool.submit(self.admin.post,path,json={**self.entry,'amount':'15.00','entry_ref':ref})
                    for ref in ('line-B','line-C')]
            self.assertEqual(sorted(r.result().status_code for r in more),[201,422])
        self.assertEqual(self.admin.post(path,json={**self.entry,'amount':'5.00'}).status_code,409)
        detail = self.admin.get(self.base+'/'+row['id']).json()
        self.assertEqual(detail['entry_count'],2)
        self.assertEqual(Decimal(detail['remaining_amount']),0)
