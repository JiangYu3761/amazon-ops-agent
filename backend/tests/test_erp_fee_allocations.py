"""Fee attribution over indivisible currency units and versioned source settlement data."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

from test_erp_refunds import OrderLineFixture


class FeeAllocationTests(OrderLineFixture):
    def setUp(self):
        super().setUp()
        self.keys={}
        with self.db() as conn:
            for name,currency in [('A','USD'),('B','USD'),('C','USD'),('D','JPY'),('E','JPY'),('F','JPY')]:
                p=conn.execute('''INSERT INTO products(model,name,marketplace,currency,sku,data_account_key)
                                  VALUES(%s,'试验产品','US',%s,%s,%s) RETURNING id''',
                               (name+uuid4().hex,currency,name+'-SKU',self.seller)).fetchone()
                self.keys[name]=str(p['id'])
        self.base='/api/erp-settlements/statements'

    def source(self,currency='USD',fee='-30'):
        ref=uuid4().hex
        self.header={'account_ref':self.seller,'site':'US','currency':currency,'statement_ref':ref,
                     'payout_date':'2026-11-10','opening_balance':'31','closing_balance':'1',
                     'actual_payout':'0','payout_ref':'BANK-X'}
        created=self.staff.post(self.base,json=self.header)
        self.assertEqual(created.status_code,201,created.text)
        self.statement=self.base+'/'+created.json()['id']
        csv='account_ref,site,currency,statement_ref,entry_ref,kind,occurred_on,original_order_date,amount\n'
        csv+=f'{self.seller},US,{currency},{ref},F-{ref},fee,2026-11-01,,{fee}\n'
        self.staff.post(self.statement+'/imports',files={'file':('fees.csv',csv,'text/csv')}).raise_for_status()
        self.entry=self.staff.get(self.statement).json()['entries']['items'][0]['id']
        self.endpoint=self.statement+'/entries/'+self.entry+'/allocations'

    def targets(self,names,weights,basis='product'):
        return {'basis':basis,'reason':'按核对后的订单件数归集','targets':[
            {'product_id':self.keys[n],'sku':n+'-SKU' if basis=='sku' else None,'weight':w}
            for n,w in zip(names,weights)]}

    def preview(self,selection):
        r=self.staff.post(self.endpoint+'/preview',json=selection)
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def command(self,preview):
        return {'preview_id':preview['preview_id'],'request_id':str(uuid4())}

    def test_two_to_one_stale_duplicate_manual_void_and_source_reversal(self):
        self.source()
        draft=self.preview(self.targets('AB',['2','1']))
        self.assertEqual(draft['expense'],draft['allocated'])
        self.assertEqual({t['product_id']:t['expense'] for t in draft['targets']},
                         {self.keys['A']:'20.00',self.keys['B']:'10.00'})
        stale=self.preview(self.targets('AB',['1','1']))
        cmd=self.command(draft)
        with ThreadPoolExecutor(2) as pool:
            repeats=[pool.submit(self.staff.post,self.endpoint+'/post',json=cmd) for _ in range(2)]
            responses=[r.result() for r in repeats]
        self.assertEqual([r.status_code for r in responses],[200,200])
        for response in responses:self.assertEqual(response.json()['version'],1)
        self.assertEqual(self.staff.post(self.endpoint+'/post',json=self.command(stale)).status_code,409)
        self.assertEqual(self.other.get(self.endpoint).status_code,404)
        self.assertEqual(self.admin.post(self.endpoint+'/post',json=self.command(stale)).status_code,404)
        self.assertEqual(self.staff.get(self.statement).json()['entry_count'],1)
        overview=self.staff.get(self.statement+'/allocation-summary').json()
        self.assertEqual(sum(Decimal(r['expense']) for r in overview['items']),Decimal('30.00'))
        cancel={'expected_version':1,'reason':'按售出数量重新归集','request_id':str(uuid4())}
        self.staff.post(self.endpoint+'/void',json=cancel).raise_for_status()
        history=self.staff.post(self.endpoint+'/void',json=cancel).json()
        self.assertEqual(history['version'],2)
        self.assertIsNone(history['active'])
        self.assertEqual(history['history']['items'][0]['expense'],'-30.00')
        self.assertIsNone(self.staff.post(self.endpoint+'/post',json=cmd).json()['active'])
        next_draft=self.preview(self.targets('AB',['3','1'],basis='sku'))
        self.staff.post(self.endpoint+'/post',json=self.command(next_draft)).raise_for_status()
        self.staff.post(self.statement+'/entries/'+self.entry+'/reversal',json={'reason':'错误的原费用'}).raise_for_status()
        result=self.staff.get(self.endpoint).json()
        self.assertTrue(result['source_reversed'])
        self.assertIsNone(result['active'])
        self.assertEqual(result['version'],4)
        self.assertEqual(self.staff.get(self.statement+'/allocation-summary').json()['items'],[])
        self.assertEqual(self.staff.post(self.endpoint+'/preview',json=self.targets('AB',[2,1])).status_code,409)

    def test_cent_yen_tie_break_invalid_targets_and_payout_change(self):
        for currency,fee,names,one,zero in [('USD','-0.01','ABC','0.01','0.00'),('JPY','-1','DEF','1','0')]:
            self.source(currency,fee)
            a=self.preview(self.targets(names,[1,1,1]))
            b=self.preview(self.targets(names[::-1],[1,1,1]))
            self.assertEqual(a['targets'],b['targets'])
            self.assertEqual([t['expense'] for t in a['targets']],[one,zero,zero])
            self.assertEqual(a['targets'][0]['product_id'],min(self.keys[n] for n in names))
            cmds=[self.targets(names[:2],[1,0]),self.targets(names[:2],[1,-1]),
                  self.targets(names[:2],[1,'0.0000001']),self.targets(names[0]*2,[1,2]),
                  self.targets('D' if currency=='USD' else 'A',[1])]
            for invalid in cmds:
                self.assertEqual(self.staff.post(self.endpoint+'/preview',json=invalid).status_code,422)
            self.staff.post(self.endpoint+'/post',json=self.command(a)).raise_for_status()
            change={k:self.header[k] for k in ('payout_date','opening_balance','closing_balance','actual_payout','payout_ref')}
            change.update({'payout_date':'2026-11-12','reason':'银行回单确认付款日','expected_version':1})
            self.staff.patch(self.statement,json=change).raise_for_status()
            revised=self.staff.get(self.endpoint).json()
            self.assertIsNone(revised['active'])
            self.assertEqual(revised['history']['items'][0]['payout_date'],'2026-11-10')
            self.assertEqual(revised['payout_date'],'2026-11-12')
            self.assertEqual(self.staff.post(self.endpoint+'/post',json=self.command(b)).status_code,409)
            self.assertEqual(self.preview(self.targets(names,[1,1,1]))['payout_date'],'2026-11-12')
