"""Local stock balances share the receiving ledger and protect both sides of transfers."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from test_erp_receiving import WarehouseFixture


STOCK = '/api/erp-stock'


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1', 'requires disposable PostgreSQL')
class LocalStockTests(WarehouseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.origin = self.scope['id']
        self.destination_config = {**self.scope_body, 'warehouse':'W2'}
        self.dest = self.post('/scopes', self.destination_config, 201, 'admin')['id']
        self.addCleanup(self.remove_transfers)

    def remove_transfers(self):
        from app.db import connection
        with connection() as conn:
            ids = [row['id'] for row in conn.execute('SELECT id FROM erp_stock_transfers WHERE account_ref=%s',
                                                      (self.scope_body['account_ref'],))]
            conn.execute('DELETE FROM erp_stock_requests WHERE transfer_id=ANY(%s)',(ids,))
            conn.execute('DELETE FROM erp_stock_transfers WHERE id=ANY(%s)',(ids,))

    def balances(self, scope, **query):
        response = self.clients['warehouse'].get(STOCK+'/balances',params={'scope_id':scope,**query})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def goods(self, qty=8):
        notice = self.notice([{'line_id':self.a, 'qty':qty}])
        record = self.post(f'/notices/{notice}/receive',{'mode':'quick','lines':[{'line_id':self.a,'qty':qty,'good':qty,'bad':0}]})
        return record['notices'][-1]['events'][-1]['id']

    def body(self, qty=3, **kwargs):
        return {'from_scope_id':self.origin,'to_scope_id':self.dest,'sku':'A','qty':qty,
                'request_id':str(uuid4()),'reference':'TX-'+uuid4().hex,'evidence':'仓间交接回执 227',**kwargs}

    def send(self, body, status=201, actor='warehouse'):
        r = self.clients[actor].post(STOCK+'/transfers',json=body)
        self.assertEqual(r.status_code,status,r.text)
        return r.json()

    def race(self, requests):
        gate = Barrier(len(requests))
        def post(operation):
            actor, path, payload = operation
            gate.wait(timeout=10)
            return self.clients[actor].post(path,json=payload)
        with ThreadPoolExecutor(max_workers=len(requests)) as pool:
            return list(pool.map(post,requests))

    def test_receiving_quality_buckets_transfer_and_posting_pair(self):
        self.assertEqual(self.balances(self.origin)['items'],[])
        lines=[{'line_id':self.a,'qty':10},{'line_id':self.b,'qty':4}]
        notice=self.notice(lines)
        self.post(f'/notices/{notice}/receive',{'mode':'normal','lines':lines})
        self.post(f'/notices/{notice}/quality',{'mode':'inspect','lines':[{'line_id':self.a,'good':5,'bad':2}]})
        self.send(self.body(qty=6),409)
        p1=self.balances(self.origin,limit=1)
        self.assertEqual((p1['items'][0]['sku'],p1['next_offset']),('A',1))
        self.assertEqual(self.balances(self.origin,limit=1,offset=p1['next_offset'])['items'][0]['sku'],'B')
        item=self.send(self.body(qty=3))
        self.assertEqual((item['from_warehouse'],item['to_warehouse'],item['actor_id'],item['evidence']),
                         ('W1','W2',self.ids['warehouse'],'仓间交接回执 227'))
        def buckets(scope):
            row = self.balances(scope,sku='A')['items'][0]
            return row['pending'],row['good'],row['bad']
        self.assertEqual(buckets(self.origin),(3,2,2))
        self.assertEqual(buckets(self.dest),(0,3,0))
        self.assertEqual(self.balances(self.dest,sku='B')['items'],[])
        from app.db import connection
        with connection() as conn:
            rows=conn.execute('SELECT * FROM erp_stock_postings WHERE transfer_id=%s',(item['id'],)).fetchall()
        self.assertEqual({(str(row['scope_id']),row['good_delta'],row['pending_delta'],row['bad_delta']) for row in rows},
                         {(self.origin,-3,0,0),(self.dest,3,0,0)})

    def test_same_reference_retry_and_mismatched_payload(self):
        self.goods()
        body=self.body()
        first=self.send(body)
        self.assertEqual(self.send(body)['id'],first['id'])
        other_request={**body,'request_id':str(uuid4())}
        self.assertEqual(self.send(other_request,201,actor='admin')['id'],first['id'])
        for change in ({'qty':2},{'evidence':'另一张回执'},{'sku':'B'}):
            self.send({**body,**change,'request_id':str(uuid4())},409)
        self.send({**body,'reference':'另一个编号'},409)
        listed=self.clients['admin'].get(STOCK+'/transfers',params={'scope_id':self.dest}).json()
        self.assertEqual([row['id'] for row in listed['items']],[first['id']])
        self.assertEqual(self.balances(self.dest)['items'][0]['good'],3)

    def test_scope_evidence_and_access_revocation(self):
        self.goods()
        body=self.body()
        for invalid in ({'evidence':'  '},{'qty':0},{'qty':1.5},{'sku':'A\x00B'},{'to_scope_id':self.origin}):
            self.send({**body,**invalid},422)
        for config in ({'account_ref':'other-'+uuid4().hex},{'site':'CA'}):
            other = self.post('/scopes',{**self.destination_config,**config},201,'admin')['id']
            self.send({**body,'to_scope_id':other},422)
        self.send(body,404,'outside')
        self.assertEqual(self.anonymous.get(STOCK+'/balances',params={'scope_id':self.origin}).status_code,401)
        transfer=self.send(body)
        self.post('/scopes',{**self.destination_config,'member_ids':[]},201,'admin')
        self.send(body,404)
        replacement=self.post('/scopes',{**self.destination_config,'warehouse':'W3'},201,'admin')['id']
        for request in (body['request_id'],str(uuid4())):
            self.send({**body,'to_scope_id':replacement,'request_id':request},404)
        self.assertEqual(self.balances(replacement)['items'],[])
        self.assertEqual(self.clients['warehouse'].get(STOCK+'/balances',params={'scope_id':self.dest}).status_code,404)
        self.assertEqual(self.clients['warehouse'].get(STOCK+'/transfers',params={'scope_id':self.origin}).json()['items'],[])
        self.assertEqual(self.clients['admin'].get(STOCK+'/transfers',params={'scope_id':self.origin}).json()['items'][0]['id'],transfer['id'])

    def test_reversal_is_atomic_after_goods_were_transferred(self):
        event=self.goods(8)
        self.send(self.body(qty=6))
        self.post(f'/events/{event}/reverse',{'reason':'原收货凭证纠错'},409,'admin')
        self.assertEqual(self.balances(self.origin)['items'][0]['good'],2)
        detail=self.clients['warehouse'].get('/api/erp-receiving/orders/'+self.order_id).json()
        self.assertEqual(detail['notices'][0]['status'],'received')
        self.assertFalse(detail['notices'][0]['events'][0]['reversed'])
        self.send(self.body(qty=6,from_scope_id=self.dest,to_scope_id=self.origin))
        self.post(f'/events/{event}/reverse',{'reason':'货物已调回并核对'},who='admin')
        self.assertEqual(self.balances(self.origin)['items'][0]['good'],0)
        self.assertEqual(self.balances(self.dest)['items'][0]['good'],0)

    def test_concurrent_transfers_and_receipt_reversal(self):
        event=self.goods(6)
        url=STOCK+'/transfers'
        same=self.body(qty=4)
        pair=self.race([('warehouse',url,same),('warehouse',url,{**same,'request_id':str(uuid4())})])
        self.assertEqual([r.status_code for r in pair],[201,201])
        self.assertEqual(pair[0].json()['id'],pair[1].json()['id'])
        pair=self.race([('warehouse',url,self.body(qty=2)),('warehouse',url,self.body(qty=2))])
        self.assertEqual(sorted(r.status_code for r in pair),[201,409])
        self.send(self.body(qty=6,from_scope_id=self.dest,to_scope_id=self.origin))
        reverse='/api/erp-receiving/events/'+event+'/reverse'
        pair=self.race([('warehouse',url,self.body(qty=6)),('admin',reverse,{'request_id':str(uuid4()),'reason':'收货复核冲销'})])
        self.assertEqual(sum(r.status_code < 300 for r in pair),1)
        self.assertEqual(sum(r.status_code == 409 for r in pair),1)
        self.assertEqual(self.balances(self.origin)['items'][0]['good'],0)
        self.assertEqual(self.balances(self.dest)['items'][0]['good'],6 if pair[0].status_code == 201 else 0)

    def test_opposite_directions_finish_without_deadlock(self):
        self.goods(10)
        self.send(self.body(qty=4))
        outward = self.body(qty=2)
        inward = self.body(qty=2,from_scope_id=self.dest,to_scope_id=self.origin)
        pair = self.race([('warehouse',STOCK+'/transfers',outward),('warehouse',STOCK+'/transfers',inward)])
        self.assertEqual([r.status_code for r in pair],[201,201])
        self.assertEqual(self.balances(self.origin,sku='A')['items'][0]['good'],6)
        self.assertEqual(self.balances(self.dest,sku='A')['items'][0]['good'],4)
