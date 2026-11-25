"""Mock-only Amazon connector contract tests; no credential or live API calls."""
import gzip
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from app import amazon_connectors as ac


class AmazonConnectorTests(unittest.TestCase):
    def setUp(self):
        ac._TOKEN_CACHE.clear();ac._PROFILE_CACHE.clear()
        self.env=patch.dict(os.environ,{f'TEST_{kind}_{part}':value for kind in ('SP_API','ADS') for part,value in [('CLIENT_ID','client-fixture'),('CLIENT_SECRET','secret-fixture'),('REFRESH_TOKEN','refresh-fixture')]})
        self.env.start();self.addCleanup(self.env.stop)
        self.now=datetime(2026,11,22,12,tzinfo=timezone.utc)
        self.mapping=[{'product_id':'p1','sku':'SKU-1','asin':'ASIN-1'}]
        self.calls=[]
        self.connection=dict(provider='sp_sales',region='NA',account_key='account-1',marketplace_id='ATVPDKIKX0DER',marketplace='US',currency='USD',time_zone='America/Los_Angeles',env_prefix='TEST',ads_profile_id='profile-1')

    def client(self,handler):
        def route(request):
            self.calls.append(request)
            if str(request.url)==ac.TOKEN_URL:
                data=parse_qs(request.content.decode())
                self.assertEqual(data['grant_type'],['refresh_token'])
                return httpx.Response(200,json={'access_token':'opaque-token-fixture','expires_in':3600})
            return handler(request)
        client=httpx.Client(transport=httpx.MockTransport(route))
        self.addCleanup(client.close)
        return client

    def collect(self,client,checkpoint=None,provider='sp_sales',start='2026-11-20',end='2026-11-20',mapping=None,connection=None):
        return ac.collect_connection(connection or {**self.connection,'provider':provider},mapping or self.mapping,start,end,checkpoint or {},client=client,now=self.now)

    def report(self,day='2026-11-20',items=None):
        return {'reportSpecification':{'reportType':'GET_SALES_AND_TRAFFIC_REPORT','reportOptions':{'dateGranularity':'DAY','asinGranularity':'SKU'},'marketplaceIds':['ATVPDKIKX0DER'],'dataStartTime':day,'dataEndTime':day},
                'salesAndTrafficByAsin':items if items is not None else [self.sales_item()]}

    def sales_item(self,sku='SKU-1',asin='ASIN-1'):
        return {'sku':sku,'childAsin':asin,'salesByAsin':{'unitsOrdered':2,'orderedProductSales':{'amount':'19.98','currencyCode':'USD'}},'trafficByAsin':{'sessions':20,'pageViews':28,'unitSessionPercentage':10,'buyBoxPercentage':88}}

    def sales_client(self,report=None,status='DONE'):
        def handler(request):
            path=request.url.path
            self.assertEqual(request.url.scheme,'https')
            if path.endswith('/reports') and request.method=='POST':
                body=json.loads(request.content)
                self.assertEqual(body['reportOptions'],{'dateGranularity':'DAY','asinGranularity':'SKU'})
                return httpx.Response(202,json={'reportId':'report-1'})
            if path.endswith('/reports/report-1'):return httpx.Response(200,json={'processingStatus':status,'reportDocumentId':'document-1'})
            if path.endswith('/documents/document-1'):return httpx.Response(200,json={'url':'https://reports.s3.amazonaws.com/report.gz?signature=fixture','compressionAlgorithm':'GZIP'})
            if request.url.host=='reports.s3.amazonaws.com':
                self.assertNotIn('authorization',request.headers)
                self.assertNotIn('x-amz-access-token',request.headers)
                return httpx.Response(200,content=gzip.compress(json.dumps(report or self.report()).encode()))
            raise AssertionError(str(request.url))
        return self.client(handler)

    def test_missing_credentials_prevents_all_network_and_reports_names_only(self):
        connection={**self.connection,'env_prefix':'ABSENT_FIXTURE'}
        status=ac.credentials_status(connection)
        self.assertFalse(status['configured'])
        self.assertEqual(len(status['missing_env']),3)
        with self.assertRaises(ac.ConnectorError) as error:self.collect(self.client(lambda req: self.fail('network used')),connection=connection)
        self.assertEqual(error.exception.code,'credentials_missing')
        self.assertEqual(self.calls,[])
        self.assertNotIn('secret-fixture',repr(status))

    def test_sales_resumes_without_creating_duplicate_reports(self):
        client=self.sales_client()
        first=self.collect(client)
        self.assertTrue(first.pending);self.assertEqual(first.rows,[])
        self.assertNotIn('opaque-token',json.dumps(first.checkpoint));self.assertNotIn('signature',json.dumps(first.checkpoint))
        done=self.collect(client,first.checkpoint)
        self.assertFalse(done.pending)
        self.assertEqual(done.rows[0]['payload']['ordered_revenue'],'19.98')
        self.assertEqual(done.rows[0]['payload']['sessions'],20)
        self.assertTrue(done.coverage[0]['complete'])
        self.assertEqual(sum(r.method=='POST' and r.url.path.endswith('/reports') for r in self.calls),1)
        self.assertEqual(sum(str(r.url)==ac.TOKEN_URL for r in self.calls),1)

    def test_report_not_ready_and_cancelled_are_not_empty_success(self):
        client=self.sales_client(status='IN_PROGRESS')
        first=self.collect(client);pending=self.collect(client,first.checkpoint)
        self.assertTrue(pending.pending);self.assertGreater(pending.retry_after,0)
        client=self.sales_client(status='CANCELLED')
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,first.checkpoint)
        self.assertEqual(error.exception.code,'report_cancelled')

    def test_multiday_sales_use_distinct_daily_reports_without_fabricated_split(self):
        def handler(request):
            path=request.url.path
            if request.method=='POST':
                day=json.loads(request.content)['dataStartTime'][:10]
                return httpx.Response(202,json={'reportId':day})
            day=path.rsplit('/',1)[-1]
            if '/documents/' in path:return httpx.Response(200,json={'url':'https://reports.s3.amazonaws.com/'+day,'compressionAlgorithm':'GZIP'})
            if request.url.host=='reports.s3.amazonaws.com':return httpx.Response(200,content=gzip.compress(json.dumps(self.report(day)).encode()))
            return httpx.Response(200,json={'processingStatus':'DONE','reportDocumentId':day})
        client=self.client(handler)
        first=self.collect(client,end='2026-11-21');second=self.collect(client,first.checkpoint,end='2026-11-21')
        self.assertTrue(second.pending);self.assertIn('2026-11-20',second.checkpoint['completed'])
        done=self.collect(client,second.checkpoint,end='2026-11-21')
        self.assertEqual([r['day'] for r in done.rows],['2026-11-20','2026-11-21'])
        self.assertEqual(len([r for r in self.calls if r.method=='POST' and '/reports' in r.url.path]),2)

    def test_sales_multiday_aggregate_or_wrong_currency_is_rejected(self):
        report=self.report();report['reportSpecification']['dataEndTime']='2026-11-21'
        client=self.sales_client(report);pending=self.collect(client)
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint)
        self.assertEqual(error.exception.code,'report_scope_mismatch')
        report=self.report();report['salesAndTrafficByAsin'][0]['salesByAsin']['orderedProductSales']['currencyCode']='EUR'
        client=self.sales_client(report);pending=self.collect(client)
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint)
        self.assertEqual(error.exception.code,'currency_mismatch')

    def test_missing_sku_is_incomplete_without_zero_row(self):
        client=self.sales_client(self.report(items=[]));pending=self.collect(client);done=self.collect(client,pending.checkpoint)
        self.assertEqual(done.rows,[]);self.assertFalse(done.coverage[0]['complete'])

    def test_ambiguous_sku_binding_and_changed_scope_checkpoint_fail(self):
        client=self.sales_client();pending=self.collect(client)
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint,connection={**self.connection,'account_key':'another'})
        self.assertEqual(error.exception.code,'stale_checkpoint')
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,mapping=self.mapping*2)
        self.assertEqual(error.exception.code,'ambiguous_mapping')

    def inventory_item(self,sku='SKU-1',asin='ASIN-1'):
        return {'sellerSku':sku,'asin':asin,'lastUpdatedTime':'2026-11-21T01:00:00Z','inventoryDetails':{'fulfillableQuantity':10,'inboundWorkingQuantity':2,'inboundShippedQuantity':3,'inboundReceivingQuantity':4,'reservedQuantity':{'totalReservedQuantity':5},'unfulfillableQuantity':{'totalUnfulfillableQuantity':1}}}

    def test_inventory_full_pagination_and_atomic_coverage(self):
        mapping=self.mapping+[{'product_id':'p1','sku':'SKU-2','asin':'ASIN-2'}]
        def handler(request):
            self.assertEqual(request.url.params['details'],'true');self.assertNotIn('startDateTime',request.url.params)
            second='nextToken' in request.url.params
            return httpx.Response(200,json={'payload':{'inventorySummaries':[self.inventory_item('SKU-2','ASIN-2') if second else self.inventory_item()]},'pagination':{} if second else {'nextToken':'page-2'}})
        done=self.collect(self.client(handler),provider='sp_inventory',mapping=mapping,start='2026-11-22',end='2026-11-22')
        self.assertEqual(len(done.rows),2);self.assertEqual(done.rows[0]['payload']['inbound'],9)
        self.assertTrue(done.coverage[0]['complete'])
        self.assertEqual(done.rows[0]['payload']['source_updated_at'],'2026-11-21T01:00:00Z')

    def test_inventory_missing_details_are_unknown_not_zero_and_history_is_not_faked(self):
        item=self.inventory_item();del item['inventoryDetails']['inboundReceivingQuantity']
        client=self.client(lambda r:httpx.Response(200,json={'payload':{'inventorySummaries':[item]}}))
        done=self.collect(client,provider='sp_inventory',start='2026-11-22',end='2026-11-22')
        self.assertIsNone(done.rows[0]['payload']['inbound']);self.assertFalse(done.coverage[0]['complete'])
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,provider='sp_inventory')
        self.assertEqual(error.exception.code,'inventory_history_unavailable')

    def test_inventory_repeated_token_is_rejected(self):
        client=self.client(lambda r:httpx.Response(200,json={'payload':{'inventorySummaries':[]},'pagination':{'nextToken':'same'}}))
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,provider='sp_inventory',start='2026-11-22',end='2026-11-22')
        self.assertEqual(error.exception.code,'invalid_pagination')

    def ads_row(self,sku='SKU-1',asin='ASIN-1',cost='3.25',ad='a1'):
        return dict(date='2026-11-20',advertisedSku=sku,advertisedAsin=asin,campaignId='campaign-shared',adGroupId='group-1',adId=ad,impressions=100,clicks=8,cost=cost,purchases7d=1,sales7d='29.99')

    def ads_client(self,rows=None,report_type='spAdvertisedProduct',profiles=None,metadata=None):
        def handler(request):
            if request.url.path=='/v2/profiles':
                self.assertNotIn('amazon-advertising-api-scope',request.headers)
                return httpx.Response(200,json=profiles if profiles is not None else [dict(profileId='profile-1',countryCode='US',currencyCode='USD',timezone='America/Los_Angeles',accountInfo={'marketplaceStringId':'ATVPDKIKX0DER'})])
            if request.method=='POST':
                body=json.loads(request.content)
                self.assertEqual(body['configuration']['reportTypeId'],'spAdvertisedProduct')
                self.assertEqual(request.headers['Amazon-Advertising-API-Scope'],'profile-1')
                self.assertNotIn('spCampaigns',request.content.decode())
                return httpx.Response(202,json={'reportId':'ads-report'})
            if request.url.host=='reports.s3.amazonaws.com':return httpx.Response(200,content=gzip.compress(json.dumps(rows if rows is not None else [self.ads_row()]).encode()))
            return httpx.Response(200,json={'status':'COMPLETED','configuration':{'adProduct':'SPONSORED_PRODUCTS','reportTypeId':report_type,'groupBy':['advertiser'],'timeUnit':'DAILY','format':'GZIP_JSON'},'startDate':'2026-11-20','endDate':'2026-11-20','url':'https://reports.s3.amazonaws.com/ads.gz?signature=fixture',**(metadata or {})})
        return self.client(handler)

    def test_ads_product_rows_do_not_duplicate_campaign_spend_across_products(self):
        mapping=self.mapping+[dict(product_id='p2',sku='SKU-2',asin='ASIN-2')]
        client=self.ads_client([self.ads_row(),self.ads_row('SKU-2','ASIN-2','5.10','a2')])
        pending=self.collect(client,provider='ads_sp',mapping=mapping)
        done=self.collect(client,pending.checkpoint,provider='ads_sp',mapping=mapping)
        self.assertEqual([r['payload']['spend'] for r in done.rows],['3.25','5.10'])
        self.assertEqual({r['product_id'] for r in done.rows},{'p1','p2'})
        self.assertTrue(all(c['complete'] for c in done.coverage));self.assertFalse(done.rows[0]['payload']['mature'])
        self.assertEqual(done.rows[0]['payload']['attribution_days'],7)
        self.assertTrue(done.warnings)

    def test_ads_maturity_uses_platform_generation_or_request_lower_bound_across_midnight(self):
        # Nov 20 + 7 complete days first matures at Nov 28 00:00 Los Angeles.
        before = datetime(2026,11,28,7,59,tzinfo=timezone.utc)
        after = datetime(2026,11,28,8,1,tzinfo=timezone.utc)
        for generated,expected in ((before.isoformat(),False),(after.isoformat(),True),(None,False)):
            with self.subTest(generated=generated):
                self.now=before
                client=self.ads_client(metadata={'generatedAt':generated})
                pending=self.collect(client,provider='ads_sp')
                self.now=after
                done=self.collect(client,pending.checkpoint,provider='ads_sp')
                self.assertEqual(done.rows[0]['payload']['mature'],expected)
                self.assertEqual(done.checkpoint['ads_as_of_day'],'2026-11-28' if expected else '2026-11-27')
                self.assertEqual(done.checkpoint['ads_generated_at'],generated)
                self.assertEqual(done.checkpoint['ads_requested_at'],before.isoformat())
        legacy=dict(pending.checkpoint)
        legacy.pop('ads_requested_at')
        old=self.collect(client,legacy,provider='ads_sp')
        self.assertFalse(old.rows[0]['payload']['mature'])
        self.assertIsNone(old.checkpoint['ads_as_of_day'])
        fresh=self.collect(client,provider='ads_sp')
        refreshed=self.collect(client,fresh.checkpoint,provider='ads_sp')
        self.assertTrue(refreshed.rows[0]['payload']['mature'])

    def test_invalid_platform_report_timestamp_rejects_the_unpublished_report(self):
        for stamp in ('bad','2026-11-20T02:00:00','2040-01-01T00:00:00Z'):
            with self.subTest(stamp=stamp):
                client=self.ads_client(metadata={'generatedAt':stamp})
                pending=self.collect(client,provider='ads_sp')
                with self.assertRaises(ac.ConnectorError) as caught:
                    self.collect(client,pending.checkpoint,provider='ads_sp')
                self.assertEqual(caught.exception.code,'invalid_report')

    def test_campaign_report_is_never_accepted_as_product_report(self):
        client=self.ads_client(report_type='spCampaigns');pending=self.collect(client,provider='ads_sp')
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint,provider='ads_sp')
        self.assertEqual(error.exception.code,'report_scope_mismatch')

    def test_ads_asin_only_mapping_must_not_be_ambiguous(self):
        mapping=self.mapping+[dict(product_id='p2',sku='SKU-2',asin='ASIN-1')]
        row=self.ads_row();del row['advertisedSku']
        client=self.ads_client([row]);pending=self.collect(client,provider='ads_sp',mapping=mapping)
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint,provider='ads_sp',mapping=mapping)
        self.assertEqual(error.exception.code,'ambiguous_mapping')

    def test_ads_missing_attribution_does_not_become_zero(self):
        row=self.ads_row();del row['sales7d'];client=self.ads_client([row]);pending=self.collect(client,provider='ads_sp')
        done=self.collect(client,pending.checkpoint,provider='ads_sp')
        self.assertIsNone(done.rows[0]['payload']['attributed_sales']);self.assertFalse(done.coverage[0]['complete'])

    def test_retryable_errors_are_safe_and_preserve_retry_after(self):
        for code in (429,503,403):
            client=self.client(lambda r:httpx.Response(code,json={'secret':'must-not-appear'},headers={'Retry-After':'17'}))
            with self.assertRaises(ac.ConnectorError) as error:self.collect(client)
            self.assertEqual(error.exception.retryable,code in (429,503))
            self.assertNotIn('must-not-appear',str(error.exception))
            self.assertNotIn('opaque-token',str(error.exception))
            if code==429:self.assertEqual(error.exception.retry_after,17)

    def test_download_rejects_foreign_hosts_plaintext_and_embedded_credentials(self):
        for url in ['http://reports.s3.amazonaws.com/x','https://evil.example/x','https://reports.s3.amazonaws.com.evil.example/x','https://user:pass@reports.s3.amazonaws.com/x','https://127.0.0.1/x']:
            with self.subTest(url=url),self.assertRaises(ac.ConnectorError) as error:ac._safe_download_url(url)
            self.assertEqual(error.exception.code,'unsafe_download_url')
        self.assertEqual(ac._safe_download_url('https://bucket.s3.us-west-2.amazonaws.com/report.gz'),'https://bucket.s3.us-west-2.amazonaws.com/report.gz')

    def test_download_size_and_compression_bomb_are_bounded(self):
        client=self.sales_client();pending=self.collect(client)
        with patch.object(ac,'MAX_COMPRESSED',10):
            with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint)
        self.assertEqual(error.exception.code,'report_too_large')
        with patch.object(ac,'MAX_UNCOMPRESSED',10):
            with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint)
        self.assertEqual(error.exception.code,'report_too_large')

    def test_access_token_refreshes_once_after_unauthorized_api_response(self):
        count={'reports':0,'tokens':0}
        def handler(request):
            if request.url.path.endswith('/reports'):
                count['reports']+=1
                return httpx.Response(401 if count['reports']==1 else 202,json={'reportId':'r'})
            raise AssertionError()
        client=self.client(handler);result=self.collect(client)
        self.assertTrue(result.pending)
        self.assertEqual(sum(str(r.url)==ac.TOKEN_URL for r in self.calls),2)
        self.assertEqual(count['reports'],2)

    def test_duplicate_sales_rows_and_identity_mismatch_fail_whole_batch(self):
        for report,code in [(self.report(items=[self.sales_item(),self.sales_item()]),'duplicate_report_row'),(self.report(items=[self.sales_item(asin='OTHER')]),'product_identity_mismatch')]:
            client=self.sales_client(report);pending=self.collect(client)
            with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint)
            self.assertEqual(error.exception.code,code)

    def test_malformed_report_objects_produce_safe_errors(self):
        malformed=self.report();malformed['salesAndTrafficByAsin'][0]['salesByAsin']=[]
        client=self.sales_client(malformed);pending=self.collect(client)
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint)
        self.assertEqual(error.exception.code,'invalid_report')
        item=self.inventory_item();item['inventoryDetails']['reservedQuantity']=['bad']
        client=self.client(lambda r:httpx.Response(200,json={'payload':{'inventorySummaries':[item]}}))
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,provider='sp_inventory',start='2026-11-22',end='2026-11-22')
        self.assertEqual(error.exception.code,'invalid_report')

    def test_report_ids_are_not_arbitrary_request_paths(self):
        client=self.client(lambda r:httpx.Response(202,json={'reportId':'../../another-api?token=value'}))
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client)
        self.assertEqual(error.exception.code,'invalid_response')
        self.assertNotIn('token=value',str(error.exception))

    def test_expired_download_is_retryable_and_does_not_revoke_account(self):
        def handler(request):
            if request.method=='POST':return httpx.Response(202,json={'reportId':'r'})
            if '/documents/' in request.url.path:return httpx.Response(200,json={'url':'https://reports.s3.amazonaws.com/expired?signature=PRIVATE'})
            if request.url.host=='reports.s3.amazonaws.com':return httpx.Response(403,content=b'secret upstream body')
            return httpx.Response(200,json={'processingStatus':'DONE','reportDocumentId':'d'})
        client=self.client(handler);pending=self.collect(client)
        with self.assertRaises(ac.ConnectorError) as error:self.collect(client,pending.checkpoint)
        self.assertEqual(error.exception.code,'download_unavailable');self.assertTrue(error.exception.retryable)
        self.assertNotIn('PRIVATE',str(error.exception));self.assertNotIn('secret upstream body',str(error.exception))

    def test_download_strips_client_default_authorization_and_cookie(self):
        client=self.sales_client();client.headers.update({'Authorization':'Bearer client-default-private','Cookie':'private-session=fixture','Amazon-Advertising-API-Scope':'foreign-scope'})
        pending=self.collect(client);done=self.collect(client,pending.checkpoint)
        download=next(r for r in self.calls if r.url.host=='reports.s3.amazonaws.com')
        self.assertNotIn('authorization',download.headers);self.assertNotIn('cookie',download.headers)
        self.assertNotIn('amazon-advertising-api-scope',download.headers)
        self.assertEqual(len(done.rows),1)

    def test_today_sales_report_is_not_claimed_as_a_complete_day(self):
        report=self.report('2026-11-22');client=self.sales_client(report)
        pending=self.collect(client,start='2026-11-22',end='2026-11-22')
        done=self.collect(client,pending.checkpoint,start='2026-11-22',end='2026-11-22')
        self.assertEqual(len(done.rows),1);self.assertFalse(done.coverage[0]['complete'])

    def test_api_uses_requested_region_and_preserves_exact_money(self):
        report=self.report();report['salesAndTrafficByAsin'][0]['salesByAsin']['orderedProductSales']['amount']='123456789.1234'
        client=self.sales_client(report);connection={**self.connection,'region':'EU'}
        pending=self.collect(client,connection=connection);done=self.collect(client,pending.checkpoint,connection=connection)
        self.assertEqual(done.rows[0]['payload']['ordered_revenue'],'123456789.1234')
        self.assertTrue(all(r.url.host in {'sellingpartnerapi-eu.amazon.com','api.amazon.com','reports.s3.amazonaws.com'} for r in self.calls))

    def test_ads_profile_identity_mismatches_are_rejected_before_report_creation(self):
        normal=dict(profileId='profile-1',countryCode='US',currencyCode='USD',timezone='America/Los_Angeles',accountInfo={'marketplaceStringId':'ATVPDKIKX0DER'})
        for changed,code in [({'countryCode':'DE'},'profile_marketplace_mismatch'),({'currencyCode':'EUR'},'profile_currency_mismatch'),({'timezone':'UTC'},'profile_timezone_mismatch'),({'accountInfo':{'marketplaceStringId':'OTHER'}},'profile_marketplace_mismatch')]:
            with self.subTest(changed=changed):
                ac._PROFILE_CACHE.clear();self.calls=[]
                client=self.ads_client(profiles=[{**normal,**changed}])
                with self.assertRaises(ac.ConnectorError) as error:self.collect(client,provider='ads_sp')
                self.assertEqual(error.exception.code,code)
                self.assertFalse(any(r.method=='POST' and '/reporting/reports' in r.url.path for r in self.calls))

    def test_ads_unauthorized_or_duplicate_profile_is_not_used(self):
        for profiles in ([],[dict(profileId='other')],[dict(profileId='profile-1'),dict(profileId='profile-1')]):
            ac._PROFILE_CACHE.clear();client=self.ads_client(profiles=profiles)
            with self.assertRaises(ac.ConnectorError) as error:self.collect(client,provider='ads_sp')
            self.assertEqual(error.exception.code,'profile_not_authorized')

    def test_cached_profile_does_not_allow_relabeling_currency_or_timezone(self):
        client=self.ads_client();self.collect(client,provider='ads_sp')
        for changes,code in [({'currency':'EUR'},'profile_currency_mismatch'),({'time_zone':'UTC'},'profile_timezone_mismatch')]:
            with self.assertRaises(ac.ConnectorError) as error:self.collect(client,provider='ads_sp',connection={**self.connection,'provider':'ads_sp',**changes})
            self.assertEqual(error.exception.code,code)
        self.assertEqual(sum(r.url.path=='/v2/profiles' for r in self.calls),1)

    def test_profile_cache_is_refreshed_after_expiry(self):
        client=self.ads_client();pending=self.collect(client,provider='ads_sp')
        self.now=self.now.replace(minute=6)
        self.collect(client,pending.checkpoint,provider='ads_sp')
        self.assertEqual(sum(r.url.path=='/v2/profiles' for r in self.calls),2)

    def test_uk_country_alias_is_accepted_but_marketplace_id_still_checked(self):
        profiles=[dict(profileId='profile-1',countryCode='GB',currencyCode='GBP',timezone='Europe/London',accountInfo={'marketplaceStringId':'A1F83G8C2ARO7P'})]
        connection={**self.connection,'provider':'ads_sp','region':'EU','marketplace':'UK','marketplace_id':'A1F83G8C2ARO7P','currency':'GBP','time_zone':'Europe/London'}
        self.assertTrue(self.collect(self.ads_client(profiles=profiles),provider='ads_sp',connection=connection).pending)
