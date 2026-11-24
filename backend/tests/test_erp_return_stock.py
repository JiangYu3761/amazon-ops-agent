"""Only a verified local disposition can add transferable stock in its order line's warehouse scope."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from test_erp_refunds import OrderLineFixture


class ReturnStockTests(OrderLineFixture):
    def setUp(self):
        super().setUp()
        self.line_id=self.register()['id']
        self.packages=f'/api/erp-returns/lines/{self.line_id}/packages'
        self.warehouses=[]
        self.member=self.staff.get('/api/workflow-auth/session').json()['user']['id']
        # Membership has a FK to the temporary login. Release only this test's explicit grants.
        def cleanup_grants():
            with self.db() as conn:
                conn.execute('DELETE FROM erp_receiving_members WHERE scope_id=ANY(%s::uuid[])',(self.warehouses,))
        self.addCleanup(cleanup_grants)

    def scope(self,name,account=None,site='US',grant=True):
        body={'request_id':str(uuid4()),'account_ref':account or self.seller,'site':site,
              'business_timezone':'America/Los_Angeles','warehouse':name,'member_ids':[self.member] if grant else []}
        response=self.admin.post('/api/erp-receiving/scopes',json=body)
        self.assertEqual(response.status_code,201,response.text)
        identifier=response.json()['id']
        self.warehouses.append(identifier)
        return identifier

    def package(self,channel='local'):
        r=self.staff.post(self.packages,json={'package_ref':uuid4().hex,'ordered_units':100,'order_evidence':'ORDER-A',
                                             'expected_units':10,'channel':channel})
        self.assertEqual(r.status_code,201,r.text)
        return r.json()['id']

    def event(self,p,kind,source='local_record',**units):
        ref=uuid4().hex
        result=self.admin.post(self.packages+'/'+p+'/events',json={'kind':kind,'event_ref':ref,'evidence_ref':'e-'+ref,
                               'evidence_type':source,'occurred_on':'2026-11-24',**units})
        self.assertEqual(result.status_code,201,result.text)
        return next(r['id'] for r in result.json()['events']['items'] if r['event_ref']==ref)

    def post(self,p,event,scope,who=None,expected=201,key=None):
        result=(who or self.staff).post('/api/erp-returns/packages/'+p+'/post-stock',
                    json={'request_id':key or str(uuid4()),'event_id':event,'scope_id':scope})
        self.assertEqual(result.status_code,expected,result.text)
        return result.json()

    def balance(self,scope):
        result=self.staff.get('/api/erp-stock/balances',params={'scope_id':scope,'sku':self.body['sku']})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()['items']

    def test_disposition_mixed_stock_idempotency_transfer_and_no_financial_change(self):
        origin,target=self.scope('A'),self.scope('B')
        p=self.package()
        self.event(p,'receive',received=10)
        event=self.event(p,'dispose',good=6,defective=2,destroyed=2)
        before=self.staff.get(self.base+'/'+self.line_id).json()
        self.assertEqual(self.balance(origin),[])
        request_id=str(uuid4())
        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(lambda key:self.post(p,event,origin,key=key),[request_id,request_id,str(uuid4())]))
        for result in results:
            row=next(r for r in result['events']['items'] if r['id']==event)
            self.assertTrue(row['stock_posted'])
            self.assertEqual(row['stock_warehouse'],'A')
        funds=self.staff.get(self.base+'/'+self.line_id).json()
        self.assertEqual((funds['state'],funds['recorded_amount'],funds['remaining_amount'],funds['version']),
                         (before['state'],before['recorded_amount'],before['remaining_amount'],before['version']))
        self.assertEqual([self.balance(origin)[0][k] for k in ('pending','good','bad')],[0,6,2])
        self.post(p,event,target,expected=409)
        transfer={'request_id':str(uuid4()),'from_scope_id':origin,'to_scope_id':target,
                  'sku':self.body['sku'],'qty':7,'reference':'transfer-1','evidence':'交接单'}
        self.assertEqual(self.staff.post('/api/erp-stock/transfers',json=transfer).status_code,409)
        transfer['qty']=6
        self.assertIn(self.staff.post('/api/erp-stock/transfers',json=transfer).status_code,(200,201))
        self.assertEqual(self.balance(origin)[0]['good'],0)
        self.assertEqual(self.balance(origin)[0]['bad'],2)
        self.assertEqual(self.balance(target)[0]['good'],6)

    def test_channel_scope_line_rights_and_destroyed_only(self):
        local=self.scope('A')
        wrong_shop=self.scope('A-other',account='other-'+uuid4().hex)
        wrong_site=self.scope('A-CA',site='CA')
        unauthorized=self.scope('A-limited',grant=False)
        p=self.package()
        receive=self.event(p,'receive',received=6)
        destroyed=self.event(p,'dispose',destroyed=2)
        usable=self.event(p,'dispose',good=3,defective=1)
        injected=self.staff.post('/api/erp-returns/packages/'+p+'/post-stock',json={
            'event_id':usable,'scope_id':local,'request_id':str(uuid4()),'good':100})
        self.assertEqual(injected.status_code,422)
        self.post(p,receive,local,expected=422)
        self.post(p,destroyed,local,expected=422)
        self.post(p,usable,wrong_shop,expected=422)
        self.post(p,usable,wrong_site,expected=422)
        self.post(p,usable,unauthorized,expected=404)
        self.post(p,usable,local,who=self.other,expected=404)
        for channel,source in [('fba','fba_report'),('third_party','warehouse_report')]:
            external=self.package(channel)
            self.event(external,'receive',source,received=2)
            event=self.event(external,'dispose',source,good=1,defective=1)
            self.post(external,event,local,expected=422)
            self.post(p,event,local,expected=422)
        self.assertEqual(self.balance(local),[])
        self.assertEqual(self.anon.post('/api/erp-returns/packages/'+p+'/post-stock',
            headers={'X-Workflow-Request':'1'},json={'event_id':usable,'scope_id':local,'request_id':str(uuid4())}).status_code,401)

    def test_one_package_cannot_post_different_events_to_different_warehouses(self):
        a,b=self.scope('A'),self.scope('B')
        p=self.package()
        self.event(p,'receive',received=6)
        first=self.event(p,'dispose',good=3)
        second=self.event(p,'dispose',defective=3)
        request=str(uuid4())
        self.post(p,first,a,key=request)
        self.post(p,second,a,key=request,expected=409)
        self.post(p,second,b,expected=409)
        complete=self.post(p,second,a)
        self.assertEqual([s['id'] for s in complete['stock_scopes']],[a])
        self.assertEqual(self.balance(b),[])
        self.assertEqual([self.balance(a)[0][k] for k in ('good','bad')],[3,3])
