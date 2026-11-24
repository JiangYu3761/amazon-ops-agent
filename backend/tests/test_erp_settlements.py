"""Statement-level cash conservation and immutable, source-referenced CSV rows."""
import csv
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from test_erp_refunds import OrderLineFixture


class SettlementTests(OrderLineFixture):
    def setUp(self):
        super().setUp()
        self.api='/api/erp-settlements/statements'
        self.header={'account_ref':self.seller,'site':'US','currency':'USD','statement_ref':'NOV-A',
                     'payout_date':'2026-11-07','opening_balance':'10','closing_balance':'5',
                     'actual_payout':'68','payout_ref':'BANK-001'}
        response=self.staff.post(self.api,json=self.header)
        self.assertEqual(response.status_code,201,response.text)
        self.statement=response.json()
        self.path=self.api+'/'+self.statement['id']

    def row(self, ref, kind='sale', amount='100', **extra):
        return {'account_ref':self.seller,'site':'US','currency':'USD','statement_ref':'NOV-A',
                'entry_ref':ref,'kind':kind,'occurred_on':'2026-11-01','original_order_date':'','amount':amount,**extra}

    def upload(self,*entries,ending='\n',who=None,path=None):
        stream=io.StringIO(newline='')
        writer=csv.DictWriter(stream,fieldnames=list(self.row('')),lineterminator=ending)
        writer.writeheader()
        writer.writerows(entries)
        return (who or self.staff).post((path or self.path)+'/imports',files={
            'file':('statement.csv',stream.getvalue().encode('utf-8-sig'),'text/csv')})

    def test_funding_date_conservation_distinct_currency_and_history(self):
        self.assertEqual(self.statement['state'],'pending_entries')
        self.assertIsNone(self.statement['expected_payout'])
        pending=self.staff.get(self.api,params={'account_ref':self.seller}).json()['currency_totals'][0]
        self.assertEqual(pending['pending_actual'],'68.00')
        self.assertIsNone(pending['difference'])
        result=self.upload(self.row('sale'),self.row('F1','fee','-15'),self.row('R1','refund','-20',original_order_date='2026-10-03'))
        self.assertEqual(result.status_code,200,result.text)
        detail=self.staff.get(self.path).json()
        self.assertEqual((detail['expected_payout'],detail['actual_payout'],detail['difference']),('70.00','68.00','-2.00'))
        refund=next(r for r in detail['entries']['items'] if r['kind']=='refund')
        self.assertEqual(refund['original_order_date'],'2026-10-03')
        self.assertEqual(self.anon.get(self.api).status_code,401)
        self.assertEqual(self.other.get(self.path).status_code,404)
        self.assertEqual(self.upload(self.row('hidden'),who=self.other).status_code,404)
        second=self.staff.post(self.api,json={**self.header,'currency':'JPY','payout_date':'2026-11-11',
                                    'opening_balance':'0','closing_balance':'0','actual_payout':'12'}).json()
        self.upload(self.row('J1',currency='JPY',amount='12'),path=self.api+'/'+second['id']).raise_for_status()
        totals=self.admin.get(self.api,params={'account_ref':self.seller,'start':'2026-11-01','end':'2026-11-30','limit':1}).json()
        self.assertEqual([(t['currency'],t['expected']) for t in totals['currency_totals']],[('JPY','12'),('USD','70.00')])
        self.assertEqual(totals['next_offset'],1)
        october=self.staff.get(self.api,params={'account_ref':self.seller,'start':'2026-10-01','end':'2026-10-31'}).json()
        self.assertEqual(october['items'],[])
        revision={k:self.header[k] for k in ('payout_date','opening_balance','closing_balance','actual_payout','payout_ref')}
        revision.update({'expected_version':1,'reason':'复核银行凭证','actual_payout':'70','payout_ref':'BANK-002'})
        invalid=self.staff.patch(self.path,json={**revision,'payout_date':'2026-10-01'})
        self.assertEqual(invalid.status_code,422,invalid.text)
        changed=self.staff.patch(self.path,json=revision)
        self.assertEqual(changed.json()['state'],'balanced')
        self.assertEqual(changed.json()['revisions']['items'][-1]['snapshot']['actual_payout'],'68.00')
        self.assertEqual(self.staff.patch(self.path,json=revision).status_code,409)

    def test_list_and_currency_totals_share_a_snapshot_during_import(self):
        @contextmanager
        def import_after_page():
            with self.db() as db:
                def execute(query, *args):
                    result=db.execute(query,*args)
                    if query.startswith('SELECT s.*,n,net'):
                        self.upload(self.row('during-read')).raise_for_status()
                    return result
                yield SimpleNamespace(execute=execute)

        with patch('app.erp_settlements.connection',import_after_page):
            listing=self.staff.get(self.api,params={'account_ref':self.seller}).json()
        self.assertEqual(listing['items'][0]['state'],'pending_entries')
        self.assertEqual(listing['currency_totals'][0]['pending_count'],1)
        self.assertEqual(self.staff.get(self.path).json()['entry_count'],1)

    def test_import_retry_reversal_and_conflicting_files(self):
        sale=self.row('A-sale')
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=[pool.submit(self.upload,sale) for _ in range(2)]
            self.assertEqual(sorted(x.result().json()['replayed'] for x in replies),[False,True])
        alternate=self.upload(sale,ending='\r\n')
        self.assertEqual((alternate.json()['added'],alternate.json()['skipped']),(0,1))
        bad=self.upload(self.row('0-valid'),self.row('A-sale',amount='19'))
        self.assertEqual(bad.status_code,409,bad.text)
        self.assertIn('第 3 行',bad.text)
        for entry in (self.row('b','fee','10'),self.row('b','refund','0'),self.row('b',currency='EUR'),
                      self.row('b',account_ref='另一个店铺'),self.row('b',amount='1.007'),
                      self.row('b',occurred_on='2026-12-01')):
            self.assertEqual(self.upload(entry).status_code,422)
        original=self.staff.get(self.path).json()
        self.assertEqual(original['entry_count'],1)
        self.assertEqual(len(original['imports']['items']),2)
        reverse=self.path+'/entries/'+original['entries']['items'][0]['id']+'/reversal'
        for _ in range(2):
            result=self.staff.post(reverse,json={'reason':'原报表交易类型错误'})
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['transactions_net'],'0.00')
        self.assertEqual(self.staff.post(reverse,json={'reason':'换原因'}).status_code,409)
        self.assertTrue(self.upload(sale).json()['replayed'])
        self.assertEqual(self.upload(self.row('new-correct',amount='89.25')).json()['added'],1)
        detail=self.staff.get(self.path,params={'limit':1}).json()
        self.assertEqual(detail['entry_count'],3)
        self.assertEqual(detail['transactions_net'],'89.25')
        self.assertEqual(detail['entries']['next_offset'],1)
        self.assertNotIn('profit',detail)
