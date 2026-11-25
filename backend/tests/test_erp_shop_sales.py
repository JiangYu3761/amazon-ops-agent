import os
import unittest
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import uuid4


@unittest.skipUnless(os.environ.get("WORKFLOW_INTEGRATION")=="1","requires disposable PostgreSQL")
class MultiStoreTest(unittest.TestCase):
    def test_permissions_deduplicate_actual_warehouses(self):
        from app.erp_shop_sales import shop_zones
        from app.db import connection
        self.assertTrue(urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'))
        with connection() as conn:
            who=conn.execute("INSERT INTO app_users(email,display_name) VALUES(%s,'经营验收') RETURNING id",(str(uuid4())+'@example.test',)).fetchone()['id']
            conn.execute("INSERT INTO workflow_accounts(user_id,password_hash) VALUES(%s,'none')",(who,))
            prefix=uuid4().hex
            for store,site,warehouse in [('one','US','a'),('one','US','b'),('one','CA','c'),('secret','GB','d')]:
                s=conn.execute("""INSERT INTO erp_receiving_scopes(account_ref,site,warehouse,business_timezone)
                    VALUES(%s,%s,%s,'UTC') RETURNING id""",(prefix+store,site,warehouse)).fetchone()['id']
                if site=='US':conn.execute("INSERT INTO erp_receiving_members VALUES(%s,%s)",(s,who))
            self.assertEqual(shop_zones(conn,dict(is_admin=False,id=who)),{(prefix+'one','US'):['UTC']})
            admins=shop_zones(conn,dict(is_admin=True,id=who))
            self.assertEqual(len([s for s in admins if s[0].startswith(prefix)]),3)
            conn.rollback()

    def test_csv_dates_grain_currency_unknown_numbers_and_ratios(self):
        from fastapi import HTTPException
        from app.erp_shop_sales import HEADER,daily_csv,period_value,comparison
        def parse(*edits):
            base=dict(account_ref='S',site='US',currency='USD',date='2020-02-29',report_grain='shop_day',coverage='complete',
                      ordered_revenue='0',units_ordered='0',orders='')
            return daily_csv(('\n'.join([','.join(HEADER),*[
                ','.join({**base,**e}[k] for k in HEADER) for e in edits]])).encode())
        row=parse({})[0]
        self.assertEqual(row['day'], date(2020,2,29));self.assertEqual(row['ordered_revenue'], Decimal(0))
        self.assertIsNone(row['orders'])
        before=period_value(dict(ordered_revenue=row["ordered_revenue"],ordered_revenue_days=1),"ordered_revenue",1)
        self.assertEqual(comparison(period_value(dict(ordered_revenue=Decimal("2"),ordered_revenue_days=1),"ordered_revenue",1),before)['status'],'zero_base')
        partial=period_value(dict(ordered_revenue=row["ordered_revenue"],ordered_revenue_days=1),"ordered_revenue",2)
        self.assertIsNone(comparison(before,partial)['difference'])
        for bad in ({'date':'2021-02-29'},{'report_grain':'sku_day'},{'currency':'JPY','ordered_revenue':'1.50'},{'orders':'-1'}):
            with self.assertRaises(HTTPException):parse(bad)
        with self.assertRaises(HTTPException):parse({},{})
