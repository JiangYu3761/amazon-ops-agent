"""Exercise the live queue SQL with real multi-warehouse data inside a rolled-back transaction."""
import os
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit
from uuid import uuid4


@skipUnless(os.getenv("WORKFLOW_INTEGRATION") == "1", "requires disposable PostgreSQL")
class WarehouseQueueTest(TestCase):
    def test_access_balance_cancellations_approval_and_paging(self):
        from app.db import connection
        from app.erp_queue import QUEUE
        self.assertTrue(urlsplit(os.environ["DATABASE_URL"]).path.startswith("/homa_workflow_test_"))
        with connection() as conn:
            def row(sql, *params):
                return conn.execute(sql+" RETURNING id", params).fetchone()["id"]
            def user():
                uid = row("INSERT INTO app_users(email,display_name) VALUES (%s,'队列验收')",str(uuid4())+"@test.example")
                conn.execute("INSERT INTO workflow_accounts(user_id,password_hash) VALUES (%s,'no login')",(uid,))
                return uid
            admin, member, peer = user(), user(), user()
            def scope():
                return row("INSERT INTO erp_receiving_scopes(account_ref,site,business_timezone,warehouse) VALUES (%s,'US','UTC','W')",str(uuid4()))
            warehouse, hidden = scope(), scope()
            conn.execute("INSERT INTO erp_receiving_members VALUES (%s,%s)", (warehouse,member))
            def bill(s,ref):
                return row("""INSERT INTO erp_payable_bills(scope_id,supplier,document_ref,currency,amount,created_by)
                           VALUES (%s,'vendor',%s,'USD',100,%s)""",s,ref,member)
            payable=bill(warehouse,"P-1")
            secret=bill(hidden,"HIDDEN")
            def pay(amount):
                conn.execute("""INSERT INTO erp_payable_payments(bill_id,scope_id,external_ref,amount,paid_on,actor_id)
                             VALUES(%s,%s,%s,%s,CURRENT_DATE,%s)""",(payable,warehouse,str(uuid4()),amount,member))
            pay(30)
            def request(owner):
                return row("""INSERT INTO erp_procurement_requests(scope_id,owner_id,supplier,reason,status)
                           VALUES(%s,%s,'v','test','pending')""",warehouse,owner)
            own_request, other_request=request(admin),request(peer)
            fbm=row("""INSERT INTO erp_fbm_orders(scope_id,number,origin,paid,source_ref,created_by)
                    VALUES(%s,'FBM1','manual',true,'fixture',%s)""",warehouse,admin)
            def package(ref,status):
                return row("INSERT INTO erp_fbm_packages(order_id,package_ref,status) VALUES (%s,%s,%s)",fbm,ref,status)
            hold, shipped=package('hold','hold'),package('shipped','after_dispatch')
            failed=package('retry','stop_failed')
            package('completed','cancelled')
            def queue(actor,is_admin=False,limit=500,offset=0):
                found=conn.execute(QUEUE,dict(actor=actor,admin=is_admin,limit=limit,offset=offset)).fetchall()
                return [r for r in found if r["scope_id"] in (warehouse,hidden)]
            personal=queue(member)
            self.assertEqual([r["id"] for r in personal], [shipped,failed,hold,payable])
            self.assertEqual(personal[-1]["balance"],70)
            self.assertFalse(queue(peer))
            visible={r["id"] for r in queue(admin,True)}
            self.assertIn(other_request,visible)
            self.assertNotIn(own_request,visible)
            self.assertIn(secret,visible)
            self.assertEqual(queue(member,limit=1,offset=1)[0]["id"],failed)
            pay(70)
            conn.execute("UPDATE erp_fbm_packages SET status='cancelled' WHERE id=%s",(hold,))
            conn.execute("UPDATE erp_procurement_requests SET status='approved' WHERE id=%s",(other_request,))
            self.assertEqual([r["id"] for r in queue(member)],[shipped,failed])
            self.assertNotIn(other_request,{r["id"] for r in queue(admin,True)})
            conn.rollback()
