"""Identity-preserving advertised-product import, including its actual PostgreSQL/HTTP entry point."""
import csv
import hashlib
import io
import os
import secrets
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

from app.importer import parse_csv


SAMPLE = dict(date='2025-05-01', campaign_name='same campaign', entity_name='same group', impressions='100',
              clicks='10', spend='10.00', sales='50.00', orders='2', currency='USD', report_grain='advertised_product',
              profile_id='1201', ad_type='SP', campaign_id='201', ad_group_id='301', ad_id='401', sku='S-1',
              asin='A-1', attribution_days='7', account_key='store-A', marketplace='US')


def file_rows(*rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode()


class ProductAdRows(unittest.TestCase):
    def test_identity_uses_report_dimensions_and_ignores_labels(self):
        def parse(**delta):
            row = parse_csv('advertising', file_rows({**SAMPLE, **delta}))[0][0]
            return row['business_key']
        self.assertEqual(parse(),parse(campaign_name='renamed',entity_name='new name',spend='17.00'))
        for dim in ('profile_id','campaign_id','ad_group_id','ad_id','sku','attribution_days'):
            self.assertNotEqual(parse(),parse(**{dim:'9'}),dim)
        for key in ('ad_id','ad_group_id','campaign_id','sku','profile_id','attribution_days','currency','report_grain','ad_type'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,'第 3 行'):
                parse_csv('advertising',file_rows(SAMPLE,{**SAMPLE,key:''}))

    def test_in_file_conflicts_stop_import_and_stable_keys_cannot_fall_back_to_names(self):
        with self.assertRaisesRegex(ValueError,'重复'):
            parse_csv('advertising',file_rows(SAMPLE,{**SAMPLE,'spend':'17.00'}))
        legacy = {k:SAMPLE[k] for k in ('date','campaign_name','entity_name','impressions','clicks','spend','sales','orders','currency')}
        with self.assertRaisesRegex(ValueError,'第 3 行.*真实广告 ID'):
            parse_csv('advertising',file_rows(legacy,legacy))
        with self.assertRaisesRegex(ValueError,'冲突'):
            parse_csv('advertising',file_rows(legacy,{**legacy,'spend':'17.00'}))
        with self.assertRaisesRegex(ValueError,'advertised_product'):
            parse_csv('advertising',file_rows({**legacy,'ad_id':'1001'}))


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1','requires disposable PostgreSQL')
class ProductAdUpload(unittest.TestCase):
    def setUp(self):
        self.assertTrue(urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'))
        from app.db import connection
        from app.main import app
        from fastapi.testclient import TestClient
        self.conn = connection
        token = secrets.token_urlsafe(32)
        with self.conn() as db:
            self.admin = db.execute("INSERT INTO app_users(email,display_name) VALUES(%s,'test') RETURNING id",
                                    (uuid4().hex+'@test.example',)).fetchone()['id']
            db.execute("INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,'fixture',true)",(self.admin,))
            db.execute("INSERT INTO workflow_sessions VALUES(%s,%s,now()+interval '1 hour')",
                       (hashlib.sha256(token.encode()).hexdigest(),self.admin))
            self.product = db.execute("""INSERT INTO products(model,name,sku,asin,marketplace,currency,data_account_key,business_timezone)
                VALUES(%s,'test','S-1','A-1','US','USD','store-A','UTC') RETURNING id""",(uuid4().hex,)).fetchone()['id']
        self.client = TestClient(app,headers={'X-Workflow-Request':'1'},cookies={'homa_workflow_session':token})

    def tearDown(self):
        self.client.close()
        with self.conn() as db:
            db.execute('DELETE FROM operating_csv_artifacts WHERE product_id=%s',(self.product,))
            db.execute('DELETE FROM operating_product_mappings WHERE product_id=%s',(self.product,))
            db.execute('DELETE FROM operating_connections WHERE created_by=%s',(self.admin,))
            db.execute('DELETE FROM products WHERE id=%s',(self.product,))
            db.execute('DELETE FROM audit_logs WHERE actor_id=%s',(self.admin,))
            db.execute('DELETE FROM app_users WHERE id=%s',(self.admin,))

    def upload(self, *rows):
        return self.client.post(f'/api/products/{self.product}/imports',data={'import_type':'advertising','required_grain':'advertised_product'},
                                files={'file':('ads.csv',file_rows(*rows),'text/csv')})

    def test_same_label_replay_rename_correction_and_artifact_history(self):
        second = {**SAMPLE,'ad_id':'402'}
        original=self.upload(SAMPLE,second)
        self.assertEqual(original.status_code,201,original.text)
        self.assertEqual(self.upload(SAMPLE,second).json()['id'],original.json()['id'])
        with self.conn() as db:
            total=db.execute('SELECT COUNT(*) n,SUM(spend) cost FROM erp_advertised_product_rows WHERE product_id=%s',(self.product,)).fetchone()
            self.assertEqual((total['n'],total['cost']),(2,20))
        fixed=self.upload({**second,'campaign_name':'renamed','spend':'17'})
        self.assertEqual(fixed.status_code,201,fixed.text)
        self.assertEqual(self.upload(SAMPLE,second).json()['id'],original.json()['id'])
        with self.conn() as db:
            rows=db.execute('SELECT * FROM erp_advertised_product_rows WHERE product_id=%s ORDER BY ad_id',(self.product,)).fetchall()
            self.assertEqual([r['spend'] for r in rows],[10,17])
            self.assertEqual((rows[1]['campaign_name'],rows[1]['source_line']),('renamed',2))
            previous=db.execute('SELECT previous_rows FROM operating_csv_artifacts WHERE import_id=%s',
                                (fixed.json()['id'],)).fetchone()['previous_rows']
            self.assertEqual((previous[0]['ad_id'],previous[0]['source_line']),('402',3))
            self.assertFalse(db.execute('SELECT 1 FROM ad_performance_daily WHERE product_id=%s',(self.product,)).fetchone())
        report=self.client.get(f'/api/products/{self.product}/analytics',params={'start_date':SAMPLE['date'],'end_date':SAMPLE['date']})
        self.assertEqual(report.status_code,200,report.text)
        metric={r['key']:r['value'] for r in report.json()['modules']['ads']['metrics']}
        self.assertEqual(metric['spend'],27)
        self.assertIsNone(metric['mature_acos'])

    def test_legacy_same_day_is_retained_without_combining_overlapping_sources(self):
        legacy={k:SAMPLE[k] for k in ('date','campaign_name','entity_name','impressions','clicks','spend','sales','orders','currency')}
        response=self.client.post(f'/api/products/{self.product}/imports',data={'import_type':'advertising'},
                                  files={'file':('old.csv',file_rows(legacy),'text/csv')})
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(self.upload(SAMPLE).status_code,201)
        report=self.client.get(f'/api/products/{self.product}/analytics',params={'start_date':SAMPLE['date'],'end_date':SAMPLE['date']})
        self.assertEqual(report.status_code,200,report.text)
        metric={r['key']:r['value'] for r in report.json()['modules']['ads']['metrics']}
        self.assertIsNone(metric['spend'])
        self.assertIsNone(metric['mature_acos'])
        self.assertIn('overlapping_ad_sources',[r['code'] for r in report.json()['data_quality']['issues']])
        with self.conn() as db:
            self.assertTrue(db.execute('SELECT 1 FROM ad_performance_daily WHERE product_id=%s',(self.product,)).fetchone())
            self.assertTrue(db.execute('SELECT 1 FROM erp_advertised_product_rows WHERE product_id=%s',(self.product,)).fetchone())

    def test_product_scope_and_profile_mapping_reject_wrong_sku_without_partial_writes(self):
        for changed in ({'account_key':'different'},{'marketplace':'CA'},{'sku':'other'},{'asin':'other'},{'currency':'CAD'}):
            resp=self.upload(SAMPLE,{**SAMPLE,'ad_id':'402',**changed})
            self.assertEqual(resp.status_code,422,resp.text)
        with self.conn() as db:
            self.assertFalse(db.execute('SELECT 1 FROM data_imports WHERE product_id=%s',(self.product,)).fetchone())
            source=db.execute("""INSERT INTO operating_connections(name,provider,account_key,region,marketplace,marketplace_id,
                currency,time_zone,ads_profile_id,created_by) VALUES('test','ads_sp','store-A','NA','US',%s,
                'USD','UTC','1201',%s) RETURNING id""",(uuid4().hex,self.admin)).fetchone()['id']
            db.execute("INSERT INTO operating_product_mappings VALUES(%s,%s,'S-1','A-1')",(source,self.product))
        mapped={**SAMPLE,'account_key':'','marketplace':''}
        resp=self.upload(mapped)
        self.assertEqual(resp.status_code,201,resp.text)
        self.assertEqual(self.upload({**mapped,'profile_id':'1202'}).status_code,422)
