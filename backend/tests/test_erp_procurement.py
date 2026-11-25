"""Scoped purchase requests, independent administrator review, and transactional order issuance."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from test_erp_receiving import WarehouseFixture

BASE='/api/erp-procurement/requests'


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION')=='1','requires a disposable PostgreSQL database')
class ProcurementTests(WarehouseFixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from app.db import connection
        def cleanup_requests():
            with connection() as conn:
                ids=[r['id'] for r in conn.execute('SELECT id FROM erp_procurement_requests WHERE owner_id=ANY(%s::uuid[])',
                                                    (list(cls.ids.values()),))]
                for table in ('erp_procurement_retries','erp_procurement_events','erp_procurement_lines'):
                    conn.execute('DELETE FROM '+table+' WHERE request_id=ANY(%s)',(ids,))
                conn.execute('DELETE FROM erp_procurement_requests WHERE id=ANY(%s)',(ids,))
        cls.addClassCleanup(cleanup_requests)

    def setUp(self):
        super().setUp()
        self.payload={'scope_id':self.scope['id'],'supplier':'签发供应商','reason':'预计四周后缺货',
                      'lines':[{'sku':'FILTER','qty':29},{'sku':'POWER','qty':7}]}

    def create(self, who='warehouse', expected=201, **other):
        response=self.clients[who].post(BASE,json={'request_id':str(uuid4()),**self.payload,**other})
        self.assertEqual(response.status_code,expected,response.text)
        return response.json()

    def action(self, item, action, who='warehouse', expected=200, **values):
        response=self.clients[who].post(BASE+'/'+item['id']+'/actions',json={
            'action':action,'request_id':str(uuid4()),'expected_version':item['version'],**values})
        self.assertEqual(response.status_code,expected,response.text)
        return response.json()

    def test_reject_amend_approve_and_issue_one_real_order(self):
        request=self.create()
        self.assertEqual((request['status'],request['version']),('draft',1))
        pending=self.action(request,'submit')
        self.action(pending,'reject','admin',422,note=' ')
        rejected=self.action(pending,'reject','admin',note='第二行需十件')
        self.assertEqual(rejected['events'][-1]['note'],'第二行需十件')
        path=BASE+'/'+request['id']
        content={'expected_version':rejected['version'],'request_id':str(uuid4()),
                 'lines':[{'sku':'FILTER','qty':29},{'sku':'POWER','qty':10}]}
        changed=self.clients['warehouse'].patch(path,json=content)
        self.assertEqual(changed.status_code,200,changed.text)
        self.assertEqual(self.clients['warehouse'].patch(path,json=content).json()['version'],changed.json()['version'])
        self.assertEqual(self.clients['warehouse'].patch(path,json={**content,'request_id':str(uuid4())}).status_code,409)
        submitted=self.action(changed.json(),'submit')
        self.action(submitted,'approve','warehouse',403)
        approved=self.action(submitted,'approve','admin')
        self.action(approved,'issue','warehouse',403)
        issued=self.action(approved,'issue','admin',order_number='APPROVED-PO')
        self.assertEqual(self.action(approved,'issue','admin',order_number='APPROVED-PO')['order_id'],issued['order_id'])
        self.action(approved,'issue','admin',409,order_number='wrong-new-number')
        self.assertEqual([e['action'] for e in issued['events']],['create','submit','reject','edit','submit','approve','issue'])
        self.assertEqual([e['version'] for e in issued['events']],list(range(1,8)))
        self.assertEqual(issued['events'][0]['snapshot']['lines'][1]['qty'],7)
        self.assertEqual(issued['events'][-1]['snapshot']['lines'][1]['qty'],10)
        actual=self.clients['warehouse'].get('/api/erp-receiving/orders/'+issued['order_id']).json()
        self.assertEqual(actual['number'],'APPROVED-PO')
        self.assertEqual((actual['procurement_request_id'],actual['procurement_request_number']),(issued['id'],issued['number']))
        self.assertEqual([(l['sku'],l['qty']) for l in actual['lines']],[('FILTER',29),('POWER',10)])
        received=self.post('/orders/'+issued['order_id']+'/notices',{'lines':[{'line_id':actual['lines'][1]['id'],'qty':6}]},201)
        self.assertEqual(received['lines'][1]['unnotified'],4)

    def test_owner_cannot_self_review_even_as_administrator(self):
        request=self.create('admin')
        pending=self.action(request,'submit','admin')
        self.action(pending,'approve','admin',403)
        self.action(pending,'reject','admin',403,note='自审')
        self.assertEqual(self.clients['admin'].get(BASE+'/'+pending['id']).json()['status'],'pending')

    def test_scope_permissions_and_validation(self):
        self.assertEqual(self.anonymous.get(BASE).status_code,401)
        self.create('outside',404)
        self.create(expected=422,lines=[{'sku':'z','qty':1},{'sku':'z','qty':2}])
        request=self.create()
        self.assertEqual(self.clients['outside'].get(BASE+'/'+request['id']).status_code,404)
        self.assertEqual(self.clients['outside'].get(BASE,params={'scope_id':self.scope['id']}).status_code,404)
        self.action(request,'submit','admin',403)
        self.action(request,'issue','admin',409)
        self.action(request,'submit',expected=422,order_number='premature')
        self.assertEqual(self.clients['admin'].patch(BASE+'/'+request['id'],json={
            'expected_version':request['version'],'request_id':str(uuid4()),'supplier':'其他'}).status_code,403)
        self.assertEqual(self.clients['warehouse'].patch(BASE+'/'+request['id'],json={
            'expected_version':request['version'],'request_id':str(uuid4()),'reason':None}).status_code,422)
        self.assertEqual(self.clients['warehouse'].get(BASE,params={'scope_id':self.scope['id'],'status':'approved'}).json()['items'],[])

    def test_idempotency_concurrent_issue_and_occupied_order_number(self):
        key=str(uuid4())
        original=self.create(request_id=key)
        self.assertEqual(self.create(request_id=key)['id'],original['id'])
        self.create(request_id=key,supplier='另一个供应商',expected=409)
        approved=self.action(self.action(original,'submit'),'approve','admin')
        self.action(approved,'issue','admin',409,order_number=self.po_body['number'])
        self.assertEqual(self.clients['warehouse'].get(BASE+'/'+original['id']).json()['status'],'approved')
        def issue(_):
            return self.action(approved,'issue','admin')
        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes=list(pool.map(issue,range(3)))
        self.assertEqual(len({r['order_id'] for r in outcomes}),1)
        self.assertEqual(sum(e['action']=='issue' for e in outcomes[-1]['events']),1)
        orders=self.clients['warehouse'].get('/api/erp-receiving/orders',params={'scope_id':self.scope['id']}).json()['items']
        self.assertEqual(sum(order['number']=='PO-'+original['number'] for order in orders),1)

    def test_signed_price_basis_survives_issue_and_unpriced_history(self):
        line = {'sku': 'A', 'qty': 8, 'contract_currency': 'USD', 'net_unit_price': '12.10', 'unit_tax': '0.25'}
        for fields in ({'net_unit_price': '1.00'}, {'contract_currency': 'EUR'},
                       {'contract_currency':'JPY','net_unit_price':'12.01'},
                       {'contract_currency':'USD','net_unit_price':'12','unit_tax':'-0.10'}):
            self.create(expected=422, lines=[{'sku':'A','qty':8, **fields}])
        req = self.create(lines=[line, {'sku':'B','qty':3}])
        self.assertEqual(req['events'][0]['snapshot']['lines'][0], line)
        edit = self.clients['warehouse'].patch(BASE+'/'+req['id'], json={
            'request_id':str(uuid4()), 'expected_version':req['version'], 'lines':[{**line,'net_unit_price':'13.30'},
                {'sku':'B','qty':3,'contract_currency':'USD','net_unit_price':'1.20'}]})
        self.assertEqual(edit.status_code, 200, edit.text)
        issued = self.action(self.action(self.action(edit.json(), 'submit'), 'approve', 'admin'), 'issue', 'admin')
        original, latest = issued['events'][0]['snapshot']['lines'], issued['events'][-1]['snapshot']['lines']
        self.assertEqual(original[0]['net_unit_price'], '12.10')
        self.assertIsNone(original[1]['net_unit_price'])
        po = self.clients['warehouse'].get('/api/erp-receiving/orders/'+issued['order_id']).json()
        self.assertEqual([(x['contract_currency'], x['net_unit_price'], x['unit_tax']) for x in po['lines']],
                         [('USD','13.30','0.25'), ('USD','1.20','0.00')])
        self.assertEqual(latest[1]['unit_tax'], '0.00')
