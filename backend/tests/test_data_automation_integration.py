"""Read-only data automation against a disposable database, never Amazon.

Run via scripts/test_process.sh. Every collector is supplied locally and all
credential values are deliberately invalid fixtures. Tests create their own
accounts without consuming the first-administrator setup flow.
"""
import copy
import os
import secrets
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1',
                     'requires a disposable PostgreSQL test database')
class DataAutomationIntegrationTests(unittest.TestCase):
    PREFIX = 'HOMA_TEST_AUTOMATION'

    @classmethod
    def setUpClass(cls):
        from urllib.parse import urlsplit
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('Never run automation integration tests against business data')
        from fastapi.testclient import TestClient
        from app.main import app
        from app.db import connection
        from app.workflow_auth import password_hash
        cls.connection = staticmethod(connection)
        cls.Client = TestClient
        cls.app = app
        cls.ids = {}
        cls.clients = {}
        cls.environment = patch.dict(os.environ, {
            'SYNC_WORKER_ENABLED': 'false',
            **{f'{cls.PREFIX}_{family}_{suffix}': 'invalid-fixture-no-live-amazon'
               for family in ('SP_API', 'ADS')
               for suffix in ('CLIENT_ID', 'CLIENT_SECRET', 'REFRESH_TOKEN')},
        })
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        # An accidental real collector invocation must fail before HTTP I/O.
        cls.network_guard = patch('app.amazon_connectors._Collector.__init__',
                                  side_effect=AssertionError('live Amazon is forbidden in integration tests'))
        cls.network_guard.start()
        cls.addClassCleanup(cls.network_guard.stop)
        password = secrets.token_urlsafe(24)
        hashed = password_hash(password)
        cls.emails = {}
        with connection() as conn:
            for role in ('admin', 'member'):
                email = f'automation-{role}-{uuid4().hex}@test.example'
                cls.emails[role] = email
                row = conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',
                                   (email, '数据自动化测试-' + role)).fetchone()
                cls.ids[role] = row['id']
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',
                             (row['id'], hashed, role == 'admin'))
            conn.commit()
        cls.addClassCleanup(cls.remove_accounts)
        for role in cls.ids:
            client = cls.Client(app, headers={'X-Workflow-Request': '1'})
            result = client.post('/api/workflow-auth/login', json={'email': cls.emails[role], 'password': password})
            assert result.status_code == 200, result.text
            cls.clients[role] = client
        cls.clients['anonymous'] = cls.Client(app, headers={'X-Workflow-Request': '1'})

    @classmethod
    def remove_accounts(cls):
        # In particular leave no administrator account before the existing
        # ProcessIntegrationTests exercise first-admin initialization.
        for client in cls.clients.values():
            client.close()
        with cls.connection() as conn:
            conn.execute('DELETE FROM audit_logs WHERE actor_id=ANY(%s)', (list(cls.ids.values()),))
            conn.execute('DELETE FROM workflow_accounts WHERE user_id=ANY(%s)', (list(cls.ids.values()),))
            # Business rows are removed by each test before actor identities.
            conn.execute('DELETE FROM app_users WHERE id=ANY(%s)', (list(cls.ids.values()),))
            conn.commit()

    def setUp(self):
        self.products = []
        self.connections = []
        self.addCleanup(self.remove_records)
        self.product = self.new_product()
        self.other = self.new_product()
        self.day = datetime.now(timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).date() - timedelta(days=20)

    def remove_records(self):
        with self.connection() as conn:
            if self.connections:
                for table in ('operating_api_facts', 'operating_data_coverage', 'operating_sync_artifacts',
                              'operating_sync_jobs', 'operating_product_mappings'):
                    conn.execute('DELETE FROM ' + table + ' WHERE connection_id=ANY(%s)', (self.connections,))
                conn.execute('DELETE FROM operating_connections WHERE id=ANY(%s)', (self.connections,))
            for table in ('operating_policy_history', 'operating_policies'):
                conn.execute('DELETE FROM ' + table + ' WHERE product_id=ANY(%s)', (self.products,))
            # Immutable CSV snapshots deliberately use restrictive foreign keys.
            # Only these fixtures are removed, in dependency order, in this
            # already-guarded disposable database.
            conn.execute('DELETE FROM operating_csv_artifacts WHERE product_id=ANY(%s)', (self.products,))
            conn.execute('DELETE FROM data_imports WHERE product_id=ANY(%s)', (self.products,))
            conn.execute('DELETE FROM products WHERE id=ANY(%s)', (self.products,))
            conn.commit()

    def new_product(self, currency='USD', marketplace='US', time_zone='America/Los_Angeles'):
        with self.connection() as conn:
            row = conn.execute('''INSERT INTO products(model,name,marketplace,currency,business_timezone,sku)
                VALUES(%s,'数据自动化隔离测试',%s,%s,%s,%s) RETURNING id,sku''',
                ('automation-' + uuid4().hex, marketplace, currency, time_zone, 'SKU-' + uuid4().hex[:12])).fetchone()
            conn.commit()
        self.products.append(row['id'])
        return row

    def connection_body(self, product=None, **changes):
        product = product or self.product
        body = dict(name='无真实凭据测试连接', provider='sp_sales', account_key='test-' + uuid4().hex,
                    region='NA', marketplace='US', marketplace_id='ATVPDKIKX0DER', currency='USD',
                    time_zone='America/Los_Angeles', env_prefix=self.PREFIX, enabled=True,
                    sync_interval_minutes=60, lookback_days=7,
                    mappings=[{'product_id': str(product['id']), 'sku': product['sku'], 'asin': None}])
        body.update(changes)
        if body['provider'] == 'ads_sp':
            body.setdefault('ads_profile_id', '123456789')
        return body

    def create(self, body=None):
        from uuid import UUID
        body = body or self.connection_body()
        result = self.clients['admin'].post('/api/integrations/connections', json=body)
        self.assertEqual(result.status_code, 201, result.text)
        row = result.json()
        self.connections.append(UUID(row['id']))
        return row, body

    def enqueue(self, source, day=None):
        day = day or self.day
        response = self.clients['admin'].post('/api/integrations/connections/' + source['id'] + '/sync',
                                            json={'start_date': day.isoformat(), 'end_date': day.isoformat()})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def claim(self, expected=None):
        from app.sync_worker import claim_job
        job = claim_job()
        self.assertIsNotNone(job)
        if expected:
            self.assertEqual(str(job['id']), expected['id'])
        return job

    def batch(self, product=None, day=None, revenue='120.00', units=6, dataset='sales', **options):
        from app.amazon_connectors import SyncBatch
        product = product or self.product
        day = day or self.day
        payload = dict(sale_date=day.isoformat(), sku=product['sku'], asin=None,
                       units_ordered=units, ordered_revenue=revenue, currency='USD', orders=4, sessions=40)
        if dataset == 'advertising':
            payload = dict(report_date=day.isoformat(), sku=product['sku'], currency='USD',
                           campaign_id='campaign-id-1', campaign_name='同一广告名称',
                           entity_id='ad-id-1', entity_name='同一广告名称',
                           impressions=100, clicks=10, spend='12.00', attributed_sales='60.00',
                           attributed_orders=3, ad_type='SP', report_grain='advertised_product',
                           attribution_days=7, mature=True)
        return SyncBatch(rows=[dict(product_id=str(product['id']), dataset=dataset, day=day.isoformat(),
                                    natural_key=product['sku'] if dataset == 'sales' else 'campaign-id-1|ad-id-1', payload=payload)],
                         coverage=[dict(product_id=str(product['id']), dataset=dataset, day=day.isoformat(), complete=True)],
                         checkpoint={}, **options)

    def run_batch(self, source, batch):
        from app.sync_worker import execute_job
        queued = self.enqueue(source)
        job = self.claim(queued)
        collector = Mock(return_value=batch)
        execute_job(job, collector=collector)
        collector.assert_called_once()
        with self.connection() as conn:
            result = conn.execute('SELECT * FROM operating_sync_jobs WHERE id=%s', (job['id'],)).fetchone()
        return job, result

    def facts(self, source):
        with self.connection() as conn:
            return conn.execute('SELECT * FROM operating_api_facts WHERE connection_id=%s ORDER BY fact_date,natural_key',
                                (source['id'],)).fetchall()

    def analytics(self, product=None, day=None):
        product = product or self.product
        day = day or self.day
        response = self.clients['member'].get('/api/products/' + str(product['id']) + '/analytics',
                                             params={'start_date': day.isoformat(), 'end_date': day.isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def metric(data, module, key):
        return next(row for row in data['modules'][module]['metrics'] if row['key'] == key)

    def test_administrator_configures_connections_policies_and_versioned_history(self):
        source, body = self.create()
        self.assertTrue(source['credentials']['configured'])
        changed = self.clients['admin'].put('/api/integrations/connections/' + source['id'],
                                           json={**body, 'name': '修改后的连接', 'expected_version': 1})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()['version'], 2)
        stale = self.clients['admin'].put('/api/integrations/connections/' + source['id'],
                                         json={**body, 'expected_version': 1})
        self.assertEqual(stale.status_code, 409)
        path = '/api/products/' + str(self.product['id']) + '/analytics/policy'
        policy = {'lead_time_days': 30, 'safety_days': 7, 'moq': 100, 'pack_size': 10, 'target_acos': '25'}
        for version in (0, 1):
            response = self.clients['admin'].put(path, json={'expected_version': version, 'policy': {**policy, 'lead_time_days': 30 + version}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['version'], version + 1)
        self.assertEqual(self.clients['admin'].put(path, json={'expected_version': 1, 'policy': policy}).status_code, 409)
        with self.connection() as conn:
            history = conn.execute('SELECT * FROM operating_policy_history WHERE product_id=%s ORDER BY version', (self.product['id'],)).fetchall()
            audit = conn.execute("SELECT * FROM audit_logs WHERE entity_type='operating_connection' AND entity_id=%s ORDER BY id", (source['id'],)).fetchall()
        self.assertEqual([r['policy']['lead_time_days'] for r in history], [30, 31])
        self.assertTrue(all(r['actor_id'] == self.ids['admin'] for r in history))
        self.assertEqual([r['action'] for r in audit], ['created', 'updated'])
        self.assertEqual(self.enqueue(source)['status'], 'queued')

    def test_anonymous_and_member_cannot_mutate_connections_policy_or_import_csv(self):
        source, body = self.create()
        queued = self.enqueue(source)
        for role, expected in (('anonymous', 401), ('member', 403)):
            client = self.clients[role]
            with self.subTest(role=role):
                requests = [
                    client.post('/api/integrations/connections', json=body),
                    client.put('/api/integrations/connections/' + source['id'], json={**body, 'expected_version': 1}),
                    client.post('/api/integrations/connections/' + source['id'] + '/sync', json={}),
                    client.post('/api/integrations/jobs/' + queued['id'] + '/cancel'),
                    client.post('/api/integrations/jobs/' + queued['id'] + '/retry'),
                    client.put('/api/products/' + str(self.product['id']) + '/analytics/policy', json={'expected_version': 0, 'policy': {}}),
                    client.post('/api/products/' + str(self.product['id']) + '/imports', data={'import_type': 'sales'},
                                files={'file': ('sales.csv', self.sales_csv(self.product, '999.00'))}),
                ]
                self.assertEqual([r.status_code for r in requests], [expected] * len(requests), [r.text for r in requests])
        self.assertEqual(self.clients['anonymous'].get('/api/integrations/connections').status_code, 401)
        self.assertEqual(self.clients['anonymous'].get('/api/products/' + str(self.product['id']) + '/analytics').status_code, 401)
        with self.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM data_imports WHERE product_id=%s', (self.product['id'],)).fetchone()['n'], 0)
            self.assertEqual(conn.execute('SELECT status FROM operating_sync_jobs WHERE id=%s', (queued['id'],)).fetchone()['status'], 'queued')

    def test_missing_credentials_neither_enqueue_nor_attempt_network(self):
        from app.amazon_connectors import ConnectorError, collect_connection
        from app.sync_worker import schedule_due
        missing = {f'{self.PREFIX}_SP_API_{suffix}': '' for suffix in ('CLIENT_ID', 'CLIENT_SECRET', 'REFRESH_TOKEN')}
        with patch.dict(os.environ, missing), patch('app.amazon_connectors.httpx.Client') as http:
            source, body = self.create()
            self.assertFalse(source['credentials']['configured'])
            response = self.clients['admin'].post('/api/integrations/connections/' + source['id'] + '/sync', json={})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['detail']['code'], 'credentials_missing')
            self.assertEqual(schedule_due(), 0)
            with self.assertRaises(ConnectorError) as error:
                collect_connection(body, body['mappings'], self.day, self.day, {})
            self.assertEqual(error.exception.code, 'credentials_missing')
            http.assert_not_called()
        with self.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM operating_sync_jobs WHERE connection_id=%s', (source['id'],)).fetchone()['n'], 0)

    def test_duplicate_concurrent_requests_have_one_active_job(self):
        source, _ = self.create()
        def enqueue(_):
            return self.clients['admin'].post('/api/integrations/connections/' + source['id'] + '/sync',
                                               json={'start_date': self.day.isoformat(), 'end_date': self.day.isoformat()})
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(enqueue, range(2)))
        self.assertEqual([r.status_code for r in results], [202, 202], [r.text for r in results])
        self.assertEqual(results[0].json()['id'], results[1].json()['id'])
        with self.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM operating_sync_jobs WHERE connection_id=%s', (source['id'],)).fetchone()['n'], 1)

    def test_ads_default_window_and_explicit_month_backfill_do_not_replace_each_other(self):
        source, body = self.create(self.connection_body(provider='ads_sp',lookback_days=1))
        api = self.clients['admin']
        path = '/api/integrations/connections/'+source['id']+'/sync'
        jobs = '/api/integrations/jobs/'
        # During the US autumn DST change, UTC has rolled over to Nov 1 but LA has not.
        with patch('app.data_automation.now_utc',return_value=datetime(2026,11,1,6,30,tzinfo=timezone.utc)):
            default = api.post(path,json={}).json()
            self.assertEqual((default['start_date'],default['end_date']),('2026-10-23','2026-10-30'))
            month = {'start_date':'2026-10-01','end_date':'2026-10-31'}
            self.assertEqual(api.post(path,json=month).status_code,409)
            self.assertEqual(api.post(path,json={**month,'start_date':'2026-09-30'}).status_code,422)
            self.assertEqual(api.post(jobs+default['id']+'/cancel').status_code,200)
            selected = api.post(path,json=month).json()
            self.assertEqual((selected['start_date'],selected['end_date']),tuple(month.values()))
            self.assertEqual(api.post(path,json=month).json()['id'],selected['id'])
            self.assertEqual(api.post(jobs+default['id']+'/retry').status_code,409)
            api.post(jobs+selected['id']+'/cancel').raise_for_status()
            retried = api.post(jobs+selected['id']+'/retry').json()
            self.assertNotEqual(retried['id'],selected['id'])
            self.assertEqual((retried['start_date'],retried['end_date']),tuple(month.values()))
            self.assertEqual(api.post(jobs+selected['id']+'/retry').json()['id'],retried['id'])
            api.post(jobs+retried['id']+'/cancel').raise_for_status()
            end_only = api.post(path,json={'end_date':'2026-09-30'}).json()
            self.assertEqual((end_only['start_date'],end_only['end_date']),('2026-09-30','2026-09-30'))
        api.put('/api/integrations/connections/'+source['id'],json={**body,'expected_version':source['version']}).raise_for_status()
        self.assertEqual(api.post(jobs+selected['id']+'/retry').status_code,409)

    def test_ads_scheduler_catches_up_missed_mature_days_and_returns_to_recent_range(self):
        from app.amazon_connectors import SyncBatch
        from app.sync_worker import schedule_due, publish_batch
        source,_ = self.create(self.connection_body(provider='ads_sp',sync_interval_minutes=1440))
        zone = ZoneInfo('America/Los_Angeles')
        today = datetime.now(zone).date()
        first = today-timedelta(days=50)
        with self.connection() as conn:
            # Simulate a pre-fix release that stopped after fetching only immature reports.
            conn.execute("""INSERT INTO operating_sync_jobs(connection_id,connection_version,status,
                start_date,end_date,created_at) VALUES(%s,%s,'succeeded',%s,%s,%s)""",
                (source['id'],source['version'],first,first+timedelta(days=6),
                 datetime.combine(first+timedelta(days=7),datetime.min.time(),tzinfo=zone)))
        recent_end = today-timedelta(days=1)
        # 31-day catch-up followed immediately by the remaining older and recent dates.
        for start,end in ((first,first+timedelta(days=30)),(first+timedelta(days=31),recent_end)):
            self.assertEqual(schedule_due(),1)
            job = self.claim()
            self.assertEqual((job['start_date'],job['end_date']),(start,end))
            self.assertTrue(publish_batch(job,SyncBatch([],[],{'ads_as_of_day':today.isoformat()})))
        self.assertEqual(schedule_due(),0)
        with self.connection() as conn:
            conn.execute('UPDATE operating_connections SET next_sync_at=now() WHERE id=%s',(source['id'],))
        self.assertEqual(schedule_due(),1)
        regular = self.claim()
        self.assertEqual((regular['start_date'], regular['end_date']),(today-timedelta(days=8),recent_end))

    def test_rejected_historical_ads_chunk_leaves_recent_collection_runnable(self):
        from app.sync_worker import schedule_due,fail_job
        source,_ = self.create(self.connection_body(provider='ads_sp',sync_interval_minutes=1440))
        today = datetime.now(ZoneInfo('America/Los_Angeles')).date()
        earliest = today-timedelta(days=35)
        with self.connection() as conn:
            conn.execute("""INSERT INTO operating_sync_jobs(connection_id,connection_version,status,
                start_date,end_date,created_at) VALUES(%s,%s,'failed',%s,%s,now()-interval '34 days')""",
                (source['id'],source['version'],earliest,earliest))
        self.assertEqual(schedule_due(),1)
        backfill=self.claim()
        self.assertEqual((backfill['start_date'],backfill['end_date']),(earliest,today-timedelta(days=9)))
        fail_job(backfill,'report_failed',False)
        self.assertEqual(schedule_due(),1)
        recent=self.claim()
        self.assertEqual((recent['start_date'],recent['end_date']),(today-timedelta(days=8),today-timedelta(days=1)))

    def test_stale_ads_report_is_archived_without_regressing_an_already_refreshed_day(self):
        from app.sync_worker import publish_batch
        source,_ = self.create(self.connection_body(provider='ads_sp'))
        instant = datetime.now(timezone.utc)
        day = instant.astimezone(ZoneInfo('America/Los_Angeles')).date()-timedelta(days=12)
        # A later download can return an older platform revision. A genuinely newer
        # correction still replaces the partition, and all three artifacts remain.
        for age,spend,expected,writes in ((1,'20','20',1),(2,'9','20',0),(0,'17','17',1)):
            job = self.claim(self.enqueue(source,day))
            batch = self.batch(day=day,dataset='advertising')
            batch.rows[0]['payload']['spend']=spend
            stamp = instant-timedelta(days=age)
            batch.checkpoint={'ads_as_of_day':stamp.date().isoformat(),'ads_generated_at':stamp.isoformat()}
            self.assertTrue(publish_batch(job,batch))
            with self.connection() as conn:
                saved = conn.execute("""SELECT payload->>'spend' AS spend FROM operating_api_facts
                    WHERE connection_id=%s AND fact_date=%s""",(source['id'],day)).fetchone()
                count = conn.execute('SELECT row_count FROM operating_sync_jobs WHERE id=%s',(job['id'],)).fetchone()
                self.assertEqual(saved['spend'],expected)
                self.assertEqual(count['row_count'],writes)
        with self.connection() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM operating_sync_artifacts WHERE connection_id=%s',
                                          (source['id'],)).fetchone()['n'],3)

    def test_mapping_rejects_currency_marketplace_timezone_account_and_duplicate_source(self):
        for mismatch in ({'currency': 'EUR'}, {'marketplace': 'CA'}, {'time_zone': 'UTC'}):
            with self.subTest(mismatch=mismatch):
                response = self.clients['admin'].post('/api/integrations/connections', json=self.connection_body(**mismatch))
                self.assertEqual(response.status_code, 422, response.text)
        source, body = self.create()
        # Different provider still cannot relabel one product as another store.
        response = self.clients['admin'].post('/api/integrations/connections', json=self.connection_body(provider='sp_inventory'))
        self.assertEqual(response.status_code, 409, response.text)
        duplicate = self.connection_body(account_key=body['account_key'])
        response = self.clients['admin'].post('/api/integrations/connections', json=duplicate)
        self.assertEqual(response.status_code, 409)
        # A genuinely separate product/store/currency/time zone is independent.
        canadian = self.new_product(currency='CAD', marketplace='CA', time_zone='America/Toronto')
        other, _ = self.create(self.connection_body(canadian, currency='CAD', marketplace='CA',
                                                  marketplace_id='A2EUQ1WTGCTBG2', time_zone='America/Toronto'))
        self.assertNotEqual(source['account_key'], other['account_key'])
        with self.connection() as conn:
            self.assertEqual(conn.execute('SELECT data_account_key FROM products WHERE id=%s', (canadian['id'],)).fetchone()['data_account_key'], other['account_key'])

    def test_aliasing_one_asin_across_products_is_rejected(self):
        body = self.connection_body()
        body['mappings'] = [{'product_id': str(p['id']), 'sku': p['sku'], 'asin': 'B012345678'} for p in (self.product, self.other)]
        response = self.clients['admin'].post('/api/integrations/connections', json=body)
        self.assertEqual(response.status_code, 422, response.text)
        with self.connection() as conn:
            self.assertIsNone(conn.execute('SELECT data_account_key FROM products WHERE id=%s', (self.product['id'],)).fetchone()['data_account_key'])

    def test_collector_publish_is_fenced_idempotent_and_corrected_artifacts_survive(self):
        from app.sync_worker import publish_batch
        source, _ = self.create()
        first, result = self.run_batch(source, self.batch())
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(publish_batch(first, self.batch()))
        second, result = self.run_batch(source, self.batch(revenue='80.00', units=4))
        self.assertEqual(result['status'], 'succeeded')
        facts = self.facts(source)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]['payload']['ordered_revenue'], '80.00')
        self.assertEqual(facts[0]['source_job_id'], second['id'])
        with self.connection() as conn:
            artifacts = conn.execute('SELECT * FROM operating_sync_artifacts WHERE connection_id=%s ORDER BY created_at', (source['id'],)).fetchall()
        self.assertEqual(len(artifacts), 2)
        self.assertEqual([a['normalized_rows'][0]['payload']['ordered_revenue'] for a in artifacts], ['120.00', '80.00'])
        self.assertEqual(self.analytics()['record_counts']['sales'], 1)

    def sales_csv(self, product, revenue, currency=None):
        suffix = ',currency' if currency is not None else ''
        value = ',' + currency if currency is not None else ''
        return (f'date,sku,units_ordered,ordered_revenue{suffix}\n{self.day.isoformat()},{product["sku"]},50,{revenue}{value}\n').encode()

    def test_api_replaces_same_date_csv_partition_without_destroying_original_import(self):
        response = self.clients['admin'].post('/api/products/' + str(self.product['id']) + '/imports',
                                             data={'import_type': 'sales'}, files={'file': ('sales.csv', self.sales_csv(self.product, '999.00'))})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.metric(self.analytics(), 'shop', 'ordered_revenue')['value'], 999.0)
        source, _ = self.create()
        _, result = self.run_batch(source, self.batch())
        self.assertEqual(result['status'], 'succeeded')
        data = self.analytics()
        self.assertEqual(data['record_counts']['sales'], 1)
        self.assertEqual(self.metric(data, 'shop', 'ordered_revenue')['value'], 120.0)
        self.assertEqual(self.metric(data, 'shop', 'units_ordered')['value'], 6.0)
        self.assertTrue(data['data_quality']['coverage']['sales']['is_complete'])
        with self.connection() as conn:
            original = conn.execute('SELECT s.*,i.imported_by FROM sales_daily s JOIN data_imports i ON i.id=s.import_id WHERE s.product_id=%s', (self.product['id'],)).fetchone()
            snapshot = conn.execute('SELECT * FROM operating_csv_artifacts WHERE product_id=%s', (self.product['id'],)).fetchone()
        self.assertEqual(str(original['ordered_revenue']), '999.00')
        self.assertEqual(original['imported_by'], self.ids['admin'])
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['import_id'], original['import_id'])
        self.assertEqual(snapshot['actor_id'], self.ids['admin'])
        self.assertEqual(snapshot['normalized_rows'][0]['ordered_revenue'], '999.00')
        self.assertEqual(snapshot['normalized_rows'][0]['sku'], self.product['sku'])
        self.assertEqual(snapshot['previous_rows'], [])
        import hashlib
        self.assertEqual(snapshot['content_sha256'], hashlib.sha256(self.sales_csv(self.product, '999.00')).hexdigest())

    def test_csv_currency_defaults_to_product_currency(self):
        product = self.new_product(currency='CAD', marketplace='CA', time_zone='America/Toronto')
        response = self.clients['admin'].post('/api/products/' + str(product['id']) + '/imports',
                                             data={'import_type': 'sales'}, files={'file': ('ca.csv', self.sales_csv(product, '55.00'))})
        self.assertEqual(response.status_code, 201, response.text)
        with self.connection() as conn:
            row = conn.execute('SELECT currency,ordered_revenue FROM sales_daily WHERE product_id=%s', (product['id'],)).fetchone()
        self.assertEqual(row['currency'], 'CAD')
        self.assertEqual(str(row['ordered_revenue']), '55.00')

    def test_one_bad_row_rejects_entire_batch_with_no_artifact_or_coverage(self):
        from app.sync_worker import execute_job
        for defect in ('currency', 'product', 'future_day', 'wrong_dataset'):
            with self.subTest(defect=defect):
                source, _ = self.create(self.connection_body(self.new_product()))
                product = {'id': self.products[-1], 'sku': None}
                with self.connection() as conn:
                    product['sku'] = conn.execute('SELECT sku FROM products WHERE id=%s', (product['id'],)).fetchone()['sku']
                batch = self.batch(product)
                bad = copy.deepcopy(batch.rows[0])
                bad['natural_key'] = 'bad-row'
                if defect == 'currency':
                    bad['payload']['currency'] = 'EUR'
                elif defect == 'product':
                    bad['product_id'] = str(self.other['id'])
                elif defect == 'future_day':
                    bad['day'] = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
                else:
                    bad['dataset'] = 'advertising'
                batch.rows.append(bad)
                queued = self.enqueue(source)
                job = self.claim(queued)
                execute_job(job, collector=Mock(return_value=batch))
                with self.connection() as conn:
                    result = conn.execute('SELECT status,error_code FROM operating_sync_jobs WHERE id=%s', (job['id'],)).fetchone()
                    for table in ('operating_api_facts', 'operating_data_coverage', 'operating_sync_artifacts'):
                        self.assertEqual(conn.execute('SELECT COUNT(*) n FROM ' + table + ' WHERE connection_id=%s', (source['id'],)).fetchone()['n'], 0)
                self.assertEqual(result, {'status': 'failed', 'error_code': 'invalid_report'})

    def test_expired_lease_recovers_and_old_worker_cannot_publish(self):
        from app.sync_worker import publish_batch, schedule_due
        source, _ = self.create()
        old = self.claim(self.enqueue(source))
        with self.connection() as conn:
            conn.execute("UPDATE operating_sync_jobs SET lease_expires_at=now()-interval '1 minute' WHERE id=%s", (old['id'],))
            conn.execute("UPDATE operating_connections SET next_sync_at=now()+interval '1 day' WHERE id=%s", (source['id'],))
            conn.commit()
        schedule_due()
        replacement = self.claim()
        self.assertEqual(replacement['id'], old['id'])
        self.assertNotEqual(replacement['lease_token'], old['lease_token'])
        self.assertFalse(publish_batch(old, self.batch()))
        self.assertEqual(self.facts(source), [])
        self.assertTrue(publish_batch(replacement, self.batch()))
        self.assertEqual(len(self.facts(source)), 1)

    def test_expired_lease_cannot_publish_before_scheduler_reclaims_it(self):
        from app.sync_worker import publish_batch
        source, _ = self.create()
        old = self.claim(self.enqueue(source))
        with self.connection() as conn:
            conn.execute("UPDATE operating_sync_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (old['id'],))
            conn.commit()
        self.assertFalse(publish_batch(old, self.batch()))
        self.assertEqual(self.facts(source), [])
        with self.connection() as conn:
            row = conn.execute('SELECT status,lease_token,error_code FROM operating_sync_jobs WHERE id=%s', (old['id'],)).fetchone()
            artifacts = conn.execute('SELECT COUNT(*) n FROM operating_sync_artifacts WHERE job_id=%s', (old['id'],)).fetchone()['n']
        self.assertEqual(row['status'], 'retry_wait')
        self.assertIsNone(row['lease_token'])
        self.assertEqual(row['error_code'], 'worker_interrupted')
        self.assertEqual(artifacts, 0)
        new = self.claim()
        self.assertNotEqual(new['lease_token'], old['lease_token'])
        self.assertTrue(publish_batch(new, self.batch()))

    def test_pending_same_job_replayed_batch_is_idempotent_in_facts_artifacts_and_count(self):
        from app.amazon_connectors import SyncBatch
        from app.sync_worker import publish_batch
        source, _ = self.create()
        job = self.claim(self.enqueue(source))
        batch = self.batch(pending=True, retry_after=5)
        self.assertTrue(publish_batch(job, batch))
        with self.connection() as conn:
            conn.execute('UPDATE operating_sync_jobs SET next_attempt_at=now() WHERE id=%s', (job['id'],))
            conn.commit()
        resumed = self.claim()
        self.assertEqual(resumed['id'], job['id'])
        self.assertNotEqual(resumed['lease_token'], job['lease_token'])
        self.assertTrue(publish_batch(resumed, batch))
        with self.connection() as conn:
            row = conn.execute('SELECT status,row_count FROM operating_sync_jobs WHERE id=%s', (job['id'],)).fetchone()
            artifacts = conn.execute('SELECT COUNT(*) n FROM operating_sync_artifacts WHERE job_id=%s', (job['id'],)).fetchone()['n']
            conn.execute('UPDATE operating_sync_jobs SET next_attempt_at=now() WHERE id=%s', (job['id'],))
            conn.commit()
        self.assertEqual(row, {'status': 'waiting', 'row_count': 1})
        self.assertEqual(artifacts, 1)
        self.assertEqual(len(self.facts(source)), 1)
        finished = self.claim()
        self.assertTrue(publish_batch(finished, SyncBatch([], [], {})))
        with self.connection() as conn:
            row = conn.execute('SELECT status,row_count FROM operating_sync_jobs WHERE id=%s', (job['id'],)).fetchone()
        self.assertEqual(row, {'status': 'succeeded', 'row_count': 1})
        self.assertEqual(len(self.facts(source)), 1)

    def test_configuration_change_fences_inflight_collector_result(self):
        from app.sync_worker import publish_batch
        source, body = self.create()
        job = self.claim(self.enqueue(source))
        response = self.clients['admin'].put('/api/integrations/connections/' + source['id'],
                                           json={**body, 'name': '新版本配置', 'expected_version': 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(publish_batch(job, self.batch()))
        self.assertEqual(self.facts(source), [])
        with self.connection() as conn:
            row = conn.execute('SELECT status,error_code FROM operating_sync_jobs WHERE id=%s', (job['id'],)).fetchone()
        self.assertEqual(row, {'status': 'cancelled', 'error_code': 'configuration_changed'})
        self.assertEqual(self.enqueue(source)['connection_version'], 2)

    def test_cancelled_job_cannot_publish_or_change_prior_facts(self):
        from app.sync_worker import publish_batch
        source, _ = self.create()
        self.run_batch(source, self.batch())
        before = self.facts(source)
        job = self.claim(self.enqueue(source))
        response = self.clients['admin'].post('/api/integrations/jobs/' + str(job['id']) + '/cancel')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'cancelled')
        self.assertFalse(publish_batch(job, self.batch(revenue='1.00')))
        self.assertEqual(self.facts(source), before)

    def test_two_raw_ad_products_same_names_survive_publish_revisions_and_replay(self):
        from app.amazon_connectors import _Collector, SyncBatch
        from types import SimpleNamespace
        config=self.connection_body(provider='ads_sp')
        second_sku=self.product['sku']+'-B'
        config['mappings'].append(dict(product_id=str(self.product['id']),sku=second_sku,asin=None))
        source,_=self.create(config)
        # Exercise the production row parser with only its business inputs and no network client.
        reader=SimpleNamespace(connection=config,start=self.day,end=self.day,by_asin={},
            by_sku={m['sku']:m for m in config['mappings']},ads_as_of_day=self.day+timedelta(days=10))
        def raw(sku,ad,cost,group):
            return dict(date=self.day.isoformat(),advertisedSku=sku,advertisedAsin='B000000001',
                        campaignId='c1',campaignName='同名',adGroupId='g1',adGroupName=group,adId=ad,
                        impressions=100,clicks=10,cost=str(cost),sales7d='50',purchases7d=1)
        for cost,group in ((10,'同组'),(17,'已改名'),(17,'再次重放')):
            rows=_Collector._ads_rows(reader,[raw(self.product['sku'],'ad1',10,group),raw(second_sku,'ad2',cost,group)])
            coverage=[dict(product_id=str(self.product['id']),dataset='advertising',day=self.day.isoformat(),complete=True)]
            _,job=self.run_batch(source,SyncBatch(rows,coverage,{}))
            self.assertEqual(job['status'],'succeeded')
            result=self.analytics()
            self.assertEqual(self.metric(result,'ads','spend')['value'],10+cost)
            self.assertEqual(self.metric(result,'ads','clicks')['value'],20)
            self.assertEqual(len(self.facts(source)),2)
            self.assertNotIn('conflicting_ad_row',[i['code'] for i in result['data_quality']['issues']])
        # Older stored payloads still have the fact PK even when the JSON lacks standardized ad IDs.
        with self.connection() as conn:
            conn.execute("""UPDATE operating_api_facts SET payload=payload-'ad_id'-'ad_group_id'
                            WHERE connection_id=%s""",(source['id'],))
        self.assertEqual(self.metric(self.analytics(),'ads','spend')['value'],27)

    def test_sales_and_sp_ads_remain_distinct_and_other_product_is_excluded(self):
        sales, body = self.create()
        ads, _ = self.create(self.connection_body(provider='ads_sp', account_key=body['account_key']))
        other, _ = self.create(self.connection_body(self.other))
        self.assertEqual(self.run_batch(sales, self.batch())[1]['status'], 'succeeded')
        self.assertEqual(self.run_batch(ads, self.batch(dataset='advertising'))[1]['status'], 'succeeded')
        self.assertEqual(self.run_batch(other, self.batch(self.other, revenue='5000.00', units=100))[1]['status'], 'succeeded')
        data = self.analytics()
        self.assertEqual(self.metric(data, 'shop', 'ordered_revenue')['value'], 120.0)
        self.assertEqual(self.metric(data, 'ads', 'attributed_sales')['value'], 60.0)
        self.assertEqual(self.metric(data, 'ads', 'spend')['value'], 12.0)
        self.assertEqual(self.metric(data, 'ads', 'acos')['value'], 20.0)
        self.assertEqual(self.metric(data, 'ads', 'tacos')['value'], 10.0)
        self.assertEqual(self.metric(data, 'ads', 'attributed_sales')['inputs']['groups'][0]['ad_type'], 'SP')
        self.assertEqual(data['record_counts']['sales'], 1)
        self.assertEqual(data['record_counts']['advertising'], 1)
        self.assertEqual({s['id'] for s in data['sources']}, {sales['id'], ads['id']})
        self.assertEqual(self.metric(self.analytics(self.other), 'shop', 'ordered_revenue')['value'], 5000.0)

    def test_lifespan_scheduler_automatically_collects_due_connection_and_publishes(self):
        from app.amazon_connectors import SyncBatch
        source, _ = self.create(self.connection_body(lookback_days=1))
        started = datetime.now(timezone.utc)

        def fake_collect(configuration, mappings, start_date, end_date, checkpoint):
            self.assertEqual(str(configuration['id']), source['id'])
            self.assertEqual(configuration['provider'], 'sp_sales')
            self.assertEqual([str(m['product_id']) for m in mappings], [str(self.product['id'])])
            self.assertEqual(start_date, end_date)
            # Derive report dates from the scheduler request, not the fixed
            # historical dates used by the manually driven worker regressions.
            report = self.batch(day=start_date)
            return SyncBatch(report.rows, report.coverage, {})

        record = None
        heartbeats = []
        with patch.dict(os.environ, {'SYNC_WORKER_ENABLED': 'true'}), \
                patch('app.amazon_connectors.collect_connection', side_effect=fake_collect) as collector:
            # Entering the real FastAPI lifespan starts both production worker
            # threads. This test never calls claim_job/execute_job/publish_batch.
            with self.Client(self.app):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with self.connection() as conn:
                        record = conn.execute('SELECT * FROM operating_sync_jobs WHERE connection_id=%s ORDER BY created_at DESC LIMIT 1',
                                              (source['id'],)).fetchone()
                        heartbeats = conn.execute('SELECT * FROM operating_worker_heartbeat WHERE updated_at>=%s', (started,)).fetchall()
                    if record and record['status'] in {'succeeded', 'failed', 'cancelled'} and heartbeats:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(record, 'lifespan scheduler never enqueued its due connection')
                self.assertEqual(record['status'], 'succeeded', dict(record))
                self.assertEqual(record['row_count'], 1)
                collector.assert_called_once()
                self.assertTrue(heartbeats, 'running scheduler must publish a fresh worker heartbeat')
                self.assertTrue(all(row['state'] in {'ready', 'running'} for row in heartbeats))
                analytics = self.analytics(day=record['start_date'])
                self.assertEqual(self.metric(analytics, 'shop', 'ordered_revenue')['value'], 120.0)
                self.assertEqual(analytics['record_counts']['sales'], 1)
            with self.connection() as conn:
                remaining = conn.execute('SELECT COUNT(*) n FROM operating_worker_heartbeat WHERE worker_id=ANY(%s)',
                                         ([r['worker_id'] for r in heartbeats],)).fetchone()['n']
            self.assertEqual(remaining, 0, 'lifespan shutdown must stop threads and remove their heartbeats')


if __name__ == '__main__':
    unittest.main()
