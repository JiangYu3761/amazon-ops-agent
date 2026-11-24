from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from test_erp_refunds import OrderLineFixture


class ReturnPackagesTests(OrderLineFixture):
    def setUp(self):
        super().setUp()
        row=self.staff.post(self.base,json=self.body).json()
        self.id=row['id']
        self.path='/api/erp-returns/lines/'+self.id+'/packages'
        self.package={'package_ref':'PACK-A','ordered_units':5,'order_evidence':'ORDER-1/L1',
                      'expected_units':4,'channel':'local'}
        self.event={'kind':'receive','event_ref':'SCAN-A','evidence_ref':'仓库签收 734',
                    'evidence_type':'local_record','occurred_on':'2026-10-01','received':2}

    def test_receipt_and_disposition_in_independent_installments(self):
        created=self.staff.post(self.path,json=self.package)
        self.assertEqual(created.status_code,201,created.text)
        pack=created.json()
        endpoint=self.path+'/'+pack['id']+'/events'
        self.assertEqual(self.staff.post(self.path,json=self.package).json()['id'],pack['id'])
        self.assertEqual(self.staff.post(self.path,json={**self.package,'expected_units':3}).status_code,409)
        self.assertEqual(self.staff.post(self.path,json={**self.package,'package_ref':'PACK-B','expected_units':2}).status_code,422)
        self.assertEqual(self.staff.post(self.path,json={**self.package,'ordered_units':8}).status_code,409)
        disposition={**self.event,'kind':'dispose','event_ref':'QC-A','received':0,'good':1,'defective':1}
        self.assertEqual(self.admin.post(endpoint,json=disposition).status_code,422)
        self.assertEqual(self.staff.post(endpoint,json=self.event).status_code,403)
        self.assertEqual(self.anon.get(self.path).status_code,401)
        self.assertEqual(self.other.get(self.path).status_code,404)
        self.assertEqual(self.other.get(self.path+'/'+pack['id']).status_code,404)

        self.admin.post(endpoint,json=self.event).raise_for_status()
        self.assertEqual(self.admin.post(endpoint,json=self.event).json()['received'],2)
        self.assertEqual(self.admin.post(endpoint,json={**self.event,'received':1}).status_code,409)
        self.admin.post(endpoint,json=disposition).raise_for_status()
        self.admin.post(endpoint,json={**self.event,'event_ref':'SCAN-B'}).raise_for_status()
        last=self.admin.post(endpoint,json={**disposition,'event_ref':'QC-B','good':0,'defective':1,'destroyed':1}).json()
        self.assertEqual([last[k] for k in ('received','good','defective','destroyed','outstanding','unclassified')],[4,1,2,1,0,0])
        self.assertEqual(self.admin.post(endpoint,json={**self.event,'event_ref':'SCAN-C'}).status_code,422)
        self.assertEqual(self.admin.post(endpoint,json={**disposition,'event_ref':'QC-C'}).status_code,422)
        seen=[]
        for offset in (0,2):
            current=self.staff.get(self.path+'/'+pack['id'],params={'offset':offset,'limit':2}).json()
            seen.extend(r['event_ref'] for r in current['events']['items'])
        self.assertEqual(set(seen),{'SCAN-A','SCAN-B','QC-A','QC-B'})
        refund=self.staff.get(self.base+'/'+self.id).json()
        self.assertEqual(refund['state'],'unmarked')
        self.assertEqual(Decimal(refund['recorded_amount']),0)
        self.admin.post(self.base+'/'+self.id+'/entries',json=self.entry).raise_for_status()
        after=self.admin.get(self.path+'/'+pack['id']).json()
        self.assertEqual(after['received'],last['received'])
        self.assertEqual(after['events'],last['events'])

    def test_backdated_disposition_preserves_each_days_available_units(self):
        pack=self.staff.post(self.path,json=self.package).json()
        endpoint=self.path+'/'+pack['id']+'/events'
        self.admin.post(endpoint,json=self.event).raise_for_status()
        self.admin.post(endpoint,json={**self.event,'event_ref':'SCAN-B','occurred_on':'2026-10-04'}).raise_for_status()
        qc={**self.event,'kind':'dispose','received':0,'good':2,'event_ref':'QC-A','occurred_on':'2026-10-03'}
        self.admin.post(endpoint,json=qc).raise_for_status()
        early={**qc,'good':1,'event_ref':'QC-B'}
        for day in ('2026-09-30','2026-10-02'):
            response=self.admin.post(endpoint,json={**early,'occurred_on':day})
            self.assertEqual(response.status_code,422,response.text)
        early['occurred_on']='2026-10-04'
        result=self.admin.post(endpoint,json=early)
        self.assertEqual(result.status_code,201,result.text)
        self.assertEqual(len(result.json()['events']['items']),4)
        self.assertEqual(result.json()['unclassified'],1)

    def test_external_receipts_require_channel_evidence_and_concurrent_caps(self):
        for channel,source in (('third_party','warehouse_report'),('fba','fba_report')):
            pack=self.staff.post(self.path,json={**self.package,'expected_units':2,'channel':channel,
                                                'package_ref':channel}).json()
            endpoint=self.path+'/'+pack['id']+'/events'
            self.assertEqual(self.admin.post(endpoint,json=self.event).status_code,422)
            report={**self.event,'evidence_type':source,'event_ref':'EXTERNAL-'+channel}
            with ThreadPoolExecutor(max_workers=2) as pool:
                retries=[pool.submit(self.admin.post,endpoint,json=report) for _ in range(2)]
                self.assertEqual([r.result().status_code for r in retries],[201,201])
            self.assertEqual(self.admin.get(self.path+'/'+pack['id']).json()['received'],2)
            classify={**report,'kind':'dispose','received':0,'good':2}
            with ThreadPoolExecutor(max_workers=2) as pool:
                competing=[pool.submit(self.admin.post,endpoint,json={**classify,'event_ref':channel+'-'+key}) for key in ('A','B')]
                self.assertEqual(sorted(r.result().status_code for r in competing),[201,422])
            self.assertEqual(self.admin.delete(endpoint+'/'+pack['id']).status_code,404)
        result=self.staff.get(self.path,params={'limit':1}).json()
        self.assertEqual(result['expected_total'],4)
        self.assertEqual(result['next_offset'],1)
        self.assertEqual(self.staff.get(self.path,params={'limit':1,'offset':1}).json()['next_offset'],None)
