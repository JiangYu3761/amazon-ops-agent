"""The scanner only suggests waiting notices; all quantities still require explicit receipt."""
import os
import unittest
from uuid import uuid4

from test_erp_receiving import WarehouseFixture


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION')=='1','requires disposable PostgreSQL')
class ReceivingScanTests(WarehouseFixture,unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from app.db import connection
        def remove_mappings():
            with connection() as conn:
                actors=list(cls.ids.values())
                conn.execute('DELETE FROM erp_receiving_barcode_history WHERE actor_id=ANY(%s::uuid[])',(actors,))
                conn.execute('DELETE FROM erp_receiving_barcodes WHERE updated_by=ANY(%s::uuid[])',(actors,))
        cls.addClassCleanup(remove_mappings)

    def scan(self,code,scope=None,who='warehouse',expect=200,**page):
        response=self.clients[who].get('/api/erp-receiving/scan',params={'scope_id':scope or self.scope['id'],'code':code,**page})
        self.assertEqual(response.status_code,expect,response.text)
        return response.json()

    def mapped(self,fnsku,sku,status=201,who='admin',**extra):
        result=self.clients[who].post('/api/erp-receiving/barcodes',json={'scope_id':self.scope['id'],'fnsku':fnsku,'sku':sku,**extra})
        self.assertEqual(result.status_code,status,result.text)
        return result.json()

    def test_exact_document_item_pagination_and_stale_choice(self):
        one=self.notice([{'line_id':self.a,'qty':30},{'line_id':self.b,'qty':10}])
        two=self.notice([{'line_id':self.a,'qty':40}])
        from_po=self.scan(self.po_body['number'])
        self.assertEqual({r['notice_id'] for r in from_po['items']},{one,two})
        self.assertTrue(all(r['matched_by']==['order_number'] for r in from_po['items']))
        number=next(r['notice_number'] for r in from_po['items'] if r['notice_id']==one)
        self.assertEqual(self.scan(number)['items'][0]['matched_by'],['notice_number'])
        first_page=self.scan(' A\r\n',limit=1)
        self.assertTrue(first_page['has_more'])
        following=self.scan('A',limit=1,offset=first_page['next_offset'])
        self.assertFalse(following['has_more'])
        self.assertEqual({r['notice_id'] for r in first_page['items']+following['items']},{one,two})
        match=next(r for r in self.scan('A')['items'] if r['notice_id']==one)
        self.assertEqual([(l['sku'],l['qty'],l['matched']) for l in match['lines']],[('A',30,True),('B',10,False)])
        self.assertFalse(self.scan('a')['items'])
        self.assertFalse(self.scan('PO-t')['items'])
        self.assertEqual(self.scan('  ',expect=422)['detail'],'请输入完整的单号、SKU 或已登记的 FNSKU')
        self.assertFalse(self.clients['warehouse'].get('/api/erp-receiving/orders/'+self.order_id).json()['ledger'])
        body={'mode':'normal','scope_id':self.scope['id'],'lines':[{'line_id':self.a,'qty':21}]}
        self.post('/notices/'+one+'/receive',body)
        self.assertEqual([r['notice_id'] for r in self.scan('A')['items']],[two])
        self.assertFalse(self.scan(number)['items'])
        self.post('/notices/'+one+'/receive',body,409)
        self.post('/notices/'+two+'/cancel',{'reason':'供应商取消'})
        self.assertFalse(self.scan(self.po_body['number'])['items'])

    def test_warehouse_authorization_and_scope_at_confirmation(self):
        self.notice([{'line_id':self.a,'qty':1}])
        another=self.post('/scopes',{**self.scope_body,'warehouse':'W2','member_ids':[]},201,'admin')
        second=self.post('/orders',{**self.po_body,'scope_id':another['id']},201,'admin')
        foreign=second['lines'][0]['id']
        waiting=self.post('/orders/'+second['id']+'/notices',{'lines':[{'line_id':foreign,'qty':1}]},201,'admin')['notices'][0]
        self.assertFalse(self.scan(waiting['number'])['items'])
        self.scan('A',scope=another['id'],expect=404)
        self.scan('A',who='outside',expect=404)
        self.assertEqual(self.anonymous.get('/api/erp-receiving/scan',params={'scope_id':self.scope['id'],'code':'A'}).status_code,401)
        self.assertTrue(self.scan('A',scope=another['id'],who='admin')['items'])
        receipt={'scope_id':self.scope['id'],'mode':'normal','lines':[{'line_id':foreign,'qty':1}]}
        self.post('/notices/'+waiting['id']+'/receive',receipt,404,'warehouse')
        self.post('/notices/'+waiting['id']+'/receive',receipt,409,'admin')
        self.assertFalse(self.clients['admin'].get('/api/erp-receiving/orders/'+second['id']).json()['ledger'])

    def test_fnsku_explicit_binding_and_cross_type_collision(self):
        self.notice([{'line_id':self.a,'qty':3}])
        self.notice([{'line_id':self.b,'qty':2}])
        self.assertIsNone(self.scan('F001')['mapped_sku'])
        self.mapped('F001','A',403,'warehouse')
        self.mapped('F001','not-in-warehouse',422)
        self.mapped('F001','A')
        self.mapped('F001','A')
        aliased=self.scan('F001')
        self.assertEqual(aliased['mapped_sku'],'A')
        self.assertEqual(aliased['items'][0]['lines'][0]['matched_by'],['fnsku'])
        self.mapped('F001','B',409)
        self.mapped('F001','B',expected_sku='A')
        self.assertEqual(self.scan('F001')['items'][0]['lines'][0]['sku'],'B')
        url='/api/erp-receiving/barcodes'
        listing=self.clients['warehouse'].get(url,params={'scope_id':self.scope['id'],'fnsku':'F001'})
        self.assertEqual([r['sku'] for r in listing.json()['items']],['B'])
        self.assertEqual(self.clients['outside'].get(url,params={'scope_id':self.scope['id']}).status_code,404)
        self.mapped('A','B')
        third=self.post('/orders',{**self.po_body,'number':'A','lines':[{'sku':'C','qty':4}]},201,'admin')
        self.post('/orders/'+third['id']+'/notices',{'lines':[{'line_id':third['lines'][0]['id'],'qty':1}]},201,'admin')
        candidates=self.scan('A')['items']
        self.assertEqual({r['matched_by'][0] for r in candidates},{'order_number','sku','fnsku'})
        self.assertEqual(len(candidates),3)
        self.assertFalse(self.scan('unknown-'+uuid4().hex)['items'])
        other=self.post('/scopes',{**self.scope_body,'warehouse':'separate'},201,'admin')
        po=self.post('/orders',{**self.po_body,'scope_id':other['id']},201,'admin')
        self.post('/orders/'+po['id']+'/notices',{'lines':[{'line_id':po['lines'][0]['id'],'qty':1}]},201,'admin')
        self.assertIsNone(self.scan('F001',scope=other['id'])['mapped_sku'])
        self.mapped('F001','A',scope_id=other['id'])
        self.assertEqual(self.scan('F001',scope=other['id'])['items'][0]['lines'][0]['sku'],'A')
        self.assertEqual(self.scan('F001')['items'][0]['lines'][0]['sku'],'B')
