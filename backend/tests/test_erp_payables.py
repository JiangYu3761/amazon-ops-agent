"""Manual liabilities backed by posted receipt lines, scoped stock locks and exact cash amounts."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from test_erp_receiving import WarehouseFixture

BASE='/api/erp-payables'


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION')=='1','requires a disposable PostgreSQL database')
class PayablesTests(WarehouseFixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from app.db import connection
        def cleanup_payables():
            with connection() as conn:
                ids=[r['id'] for r in conn.execute('SELECT id FROM erp_payable_bills WHERE created_by=ANY(%s::uuid[])',
                                                    (list(cls.ids.values()),))]
                conn.execute('DELETE FROM erp_payable_requests WHERE bill_id=ANY(%s)',(ids,))
                conn.execute('DELETE FROM erp_payable_payments WHERE bill_id=ANY(%s)',(ids,))
                conn.execute('DELETE FROM erp_payable_sources WHERE bill_id=ANY(%s)',(ids,))
                conn.execute('DELETE FROM erp_payable_bills WHERE id=ANY(%s)',(ids,))
        cls.addClassCleanup(cleanup_payables)

    def setUp(self):
        super().setUp()
        n=self.notice([{'line_id':self.a,'qty':6},{'line_id':self.b,'qty':4}])
        result=self.post(f'/notices/{n}/receive',{'mode':'normal','lines':[
            {'line_id':self.a,'qty':6},{'line_id':self.b,'qty':4}]})
        self.event=result['notices'][0]['events'][0]['id']
        self.source={'event_id':self.event,'line_id':self.a}
        self.bill={'scope_id':self.scope['id'],'document_ref':'INV-7','currency':'USD',
                   'amount':'10.05','sources':[self.source], 'note':'手工核对供应商票据'}
        self.payment={'external_ref':'bank-7','amount':'6.05','paid_on':'2026-11-24'}

    def call(self,path,body,expected=201,who='warehouse'):
        result=self.clients[who].post(BASE+path,json={'request_id':str(uuid4()),**body})
        self.assertEqual(result.status_code,expected,result.text)
        return result.json()

    def test_record_payments_and_external_replay(self):
        initial=self.clients['warehouse'].get(BASE+'/sources',params={'scope_id':self.scope['id']}).json()['items']
        self.assertEqual({r['received_qty'] for r in initial},{4,6})
        original=self.call('/bills',self.bill)
        self.assertEqual(original['amount_source'],'manual')
        self.assertEqual((original['balance'],original['status'],original['sources'][0]['sku']),('10.05','open','A'))
        self.call('/bills',{**self.bill,'document_ref':'second-invoice'},409)
        self.post('/events/'+self.event+'/reverse',{'reason':'仍有关联应付'},409,'admin')
        path='/bills/'+original['id']+'/payments'
        part=self.call(path,self.payment)
        self.assertEqual((part['balance'],part['status']),('4.00','partial'))
        retry=self.call(path,{**self.payment,'amount':'6.050'})
        self.assertEqual(len(retry['payments']),1)
        self.call(path,{**self.payment,'note':'不同内容'},409)
        self.call(path,{**self.payment,'amount':'4.01','external_ref':'other-ref'},409)
        final=self.call(path,{**self.payment,'amount':'4.00','external_ref':'bank-8'})
        self.assertEqual((final['paid'],final['balance'],final['status']),('10.05','0.00','paid'))
        self.call('/bills/'+original['id']+'/void',{'reason':'已付'},409,'admin')
        self.assertEqual(len(self.clients['warehouse'].get(BASE+'/bills/'+original['id']).json()['payments']),2)

    def test_cancel_preserves_evidence_but_releases_receipt(self):
        payload={**self.bill,'request_id':str(uuid4())}
        original=self.call('/bills',payload)
        self.assertEqual(self.call('/bills',payload)['id'],original['id'])
        self.call('/bills',{**payload,'amount':'9.99'},409)
        self.call('/bills/'+original['id']+'/void',{'reason':'权限不足'},403)
        void=self.call('/bills/'+original['id']+'/void',{'reason':'供应商票据作废'},200,'admin')
        self.assertEqual((void['status'],void['balance'],void['void_reason']),('void','0.00','供应商票据作废'))
        self.assertEqual(void['sources'][0]['event_id'],self.event)
        self.post('/events/'+self.event+'/reverse',{'reason':'原收货数录错'},200,'admin')
        self.call('/bills',self.bill,409)
        self.assertEqual(self.clients['warehouse'].get(BASE+'/sources',params={'scope_id':self.scope['id']}).json()['items'],[])

    def test_scope_and_minor_units(self):
        self.assertEqual(self.anonymous.get(BASE+'/bills').status_code,401)
        self.call('/bills',self.bill,404,'outside')
        self.call('/bills',{**self.bill,'currency':'JPY','amount':'20.01'},422)
        self.call('/bills',{**self.bill,'amount':'12.345'},422)
        self.call('/bills',{**self.bill,'sources':[self.source,self.source]},422)
        another=self.post('/scopes',{**self.scope_body,'warehouse':'W2'},201,'admin')
        self.call('/bills',{**self.bill,'scope_id':another['id']},409)
        valid=self.call('/bills',{**self.bill,'currency':'KWD','amount':'1.001'})
        self.assertEqual(valid['amount'],'1.001')
        self.assertEqual(self.clients['outside'].get(BASE+'/bills/'+valid['id']).status_code,404)
        self.call('/bills/'+valid['id']+'/payments',self.payment,404,'outside')
        self.call('/bills/'+valid['id']+'/payments',{**self.payment,'amount':'0.0001'},422)
        self.assertNotIn(valid['id'],[r['id'] for r in self.clients['outside'].get(BASE+'/bills').json()['items']])

    def test_concurrent_payments_and_identical_bank_reference(self):
        first=self.call('/bills',self.bill)
        path='/bills/'+first['id']+'/payments'
        def post_payment(data):
            return self.clients['warehouse'].post(BASE+path,json={'request_id':str(uuid4()),**data}).status_code
        with ThreadPoolExecutor(max_workers=2) as workers:
            codes=list(workers.map(post_payment,[self.payment,{**self.payment,'external_ref':'different'}]))
        self.assertEqual(sorted(codes),[201,409])
        identical={**self.payment,'amount':'4.00','external_ref':'same-transaction'}
        with ThreadPoolExecutor(max_workers=3) as workers:
            self.assertEqual(list(workers.map(post_payment,[identical]*3)),[201]*3)
        saved=self.clients['warehouse'].get(BASE+'/bills/'+first['id']).json()
        self.assertEqual((saved['balance'],len(saved['payments'])),('0.00',2))
        second=self.call('/bills',{**self.bill,'document_ref':'INV-B','sources':[{'event_id':self.event,'line_id':self.b}]})
        self.call('/bills/'+second['id']+'/payments',identical,409)

    def test_concurrent_receipt_reversal_and_bill_never_both_commit(self):
        def bill(_):
            return self.clients['warehouse'].post(BASE+'/bills',json={'request_id':str(uuid4()),**self.bill}).status_code
        def reverse(_):
            return self.clients['admin'].post('/api/erp-receiving/events/'+self.event+'/reverse',
                            json={'request_id':str(uuid4()),'reason':'并发纠错'}).status_code
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures=[workers.submit(bill,None),workers.submit(reverse,None)]
            outcomes=[job.result() for job in futures]
        self.assertIn(outcomes,[[201,409],[409,200]])
        remaining=self.clients['warehouse'].get(BASE+'/bills',params={'scope_id':self.scope['id']}).json()['items']
        self.assertEqual(len(remaining),1 if outcomes[0]==201 else 0)

    def priced_receipt(self, contract_currency='USD', price='5.00', tax='0.30'):
        po=self.post('/orders',{**self.po_body,'number':uuid4().hex, 'lines':[
            {'sku':'PRICE-A','qty':10,'contract_currency':contract_currency,'net_unit_price':price,'unit_tax':tax}]},201,'admin')
        line=po['lines'][0]['id']
        def receive(qty):
            notices=self.post('/orders/'+po['id']+'/notices',{'lines':[{'line_id':line,'qty':qty}]},201)['notices']
            notice=next(n for n in notices if n['status']=='waiting')
            result=self.post('/notices/'+notice['id']+'/receive',{'mode':'normal','lines':[{'line_id':line,'qty':qty}]})
            event=next(n for n in result['notices'] if n['id']==notice['id'])['events'][0]
            return {'event_id':event['id'],'line_id':line}
        return receive

    def preview(self, source, amount='15.90', currency='USD', who='warehouse', code=200):
        response=self.clients[who].post(BASE+'/match-preview',json={'scope_id':self.scope['id'],'currency':currency,
            'amount':amount,'sources':source})
        self.assertEqual(response.status_code,code,response.text)
        return response.json()

    def test_priced_partial_receipts_match_independently_and_freeze_invoice(self):
        receive=self.priced_receipt()
        first=receive(3)
        second=receive(4)
        invoice={'qty':3,'unit':'件','net_unit_price':'5.00','tax_amount':'0.90','amount':'15.90'}
        sources=[{**first,'invoice':invoice}]
        rows=self.clients['warehouse'].get(BASE+'/sources',params={'scope_id':self.scope['id']}).json()['items']
        a,b=[next(x for x in rows if x['event_id']==ref['event_id']) for ref in (first,second)]
        self.assertEqual((a['pricing_status'],a['net_unit_price'],a['expected_net'],a['expected_tax'],a['expected_amount']),
                         ('priced','5.00','15.00','0.90','15.90'))
        self.assertEqual((b['received_qty'],b['expected_amount']),(4,'21.20'))
        self.preview(sources,who='outside',code=404)
        ready=self.preview(sources)
        self.assertEqual((ready['status'],ready['expected_amount'],ready['differences']),('matched','15.90',[]))
        body={**self.bill,'document_ref':'INV-PRICED','amount':'15.90','sources':sources,'request_id':str(uuid4())}
        created=self.call('/bills',body)
        self.assertEqual(self.call('/bills',body)['id'],created['id'])
        self.assertEqual((created['amount_source'],created['sources'][0]['invoice']),('po_receipt_invoice',invoice))
        restored=self.clients['warehouse'].get(BASE+'/bills/'+created['id']).json()
        self.assertEqual(restored['sources'][0]['expected_amount'],'15.90')
        self.call('/bills',{**body,'request_id':str(uuid4())},409)
        self.call('/bills/'+created['id']+'/payments',{'amount':'15.90','external_ref':'voucher','paid_on':'2026-11-24'})
        self.call('/bills/'+created['id']+'/void',{'reason':'已有付款'},409,'admin')
        self.post('/events/'+first['event_id']+'/reverse',{'reason':'已结算'},409,'admin')
        inv2={**invoice,'qty':4,'amount':'21.20','tax_amount':'1.20'}
        self.assertEqual(self.preview([{**second,'invoice':inv2}],amount='21.20')['status'],'matched')

    def test_discrepancies_block_posting_without_consuming_receipt(self):
        first=self.priced_receipt()(3)
        correct={'qty':3,'unit':'件','net_unit_price':'5.00','tax_amount':'0.90','amount':'15.90'}
        for change,code in [({'qty':4},'实收数量'),({'unit':'箱'},'计量单位'),({'net_unit_price':'5.01'},'每件净价'),
                            ({'tax_amount':'0.80'},'本行税额'),({'amount':'16.00'},'本行含税金额')]:
            with self.subTest(change=change):
                src=[{**first,'invoice':{**correct,**change}}]
                issues=self.preview(src)
                self.assertEqual(issues['status'],'difference')
                self.assertTrue(any(code in x for x in issues['lines'][0]['differences']))
                self.call('/bills',{**self.bill,'amount':'15.90','sources':src},422)
        src=[{**first,'invoice':correct}]
        for selections,currency,amt,expected in [(src,'USD','16','发票总额'),(src,'EUR','15.90','合同币种'),
                ([first],'USD','15.90','请录入对应的发票行'),(src+[self.source],'USD','15.90','分别登记')]:
            result=self.preview(selections,currency=currency,amount=amt)
            self.assertTrue(any(expected in msg for msg in result['differences']))
            self.call('/bills',{**self.bill,'sources':selections,'amount':amt,'currency':currency},422)
        unpriced=self.preview([self.source])
        self.assertEqual((unpriced['status'],unpriced['expected_amount']),('manual',None))
        self.assertEqual(self.preview([{**self.source,'invoice':correct}])['status'],'difference')
        self.assertEqual(self.call('/bills',{**self.bill,'amount':'15.90','sources':src})['balance'],'15.90')

    def test_three_decimal_matching_and_multiple_line_differences_cannot_offset(self):
        receive=self.priced_receipt('KWD','2.123','0.001')
        a,b=receive(3),receive(2)
        invoice={'qty':3,'unit':'件','net_unit_price':'2.123','tax_amount':'0.003','amount':'6.372'}
        other={**invoice,'qty':2,'tax_amount':'0.002','amount':'4.248'}
        sources=[{**a,'invoice':invoice},{**b,'invoice':other}]
        self.assertEqual(self.preview(sources,'10.620','KWD')['expected_amount'],'10.620')
        sources[0]['invoice']={**invoice,'amount':'6.371'}
        sources[1]['invoice']={**other,'amount':'4.249'}
        result=self.preview(sources,'10.620','KWD')
        self.assertEqual(result['status'],'difference')
        self.assertTrue(all(line['differences'] for line in result['lines']))
        self.call('/bills',{**self.bill,'currency':'KWD','amount':'10.620','sources':sources},422)
