import unittest
from app.context import build_context, resolve_scope

class Rows:
    def fetchone(self): return {}
    def fetchall(self): return []

class RecordingConnection:
    def __init__(self): self.calls=[]
    def execute(self,sql,params=None):
        self.calls.append((sql,params)); return Rows()

class PageBoundaryTests(unittest.TestCase):
    def test_ads_page_does_not_read_inventory_sales_or_costs(self):
        conn=RecordingConnection()
        context=build_context(conn,'product-A','ads',{'status':'blocked'})
        self.assertIn('advertising',context)
        self.assertNotIn('inventory',context)
        self.assertNotIn('costs',context)
        self.assertNotIn('sales',context)
        self.assertTrue(all(params[0]=='product-A' for _,params in conn.calls))
        task_sql,params=next((sql,p) for sql,p in conn.calls if 'FROM tasks' in sql)
        self.assertIn('t.module_code=%s',task_sql)
        self.assertIn('ads',params)
        self.assertIn('blocked',params)

    def test_import_page_never_falls_back_to_dashboard(self):
        conn=RecordingConnection()
        context=build_context(conn,'product-B','imports')
        self.assertIn('imports',context)
        self.assertNotIn('dashboard',context)
        self.assertNotIn('tasks',context)

    def test_only_explicit_global_prefix_expands_scope(self):
        self.assertEqual(resolve_scope('帮我看库存','ads')[0],'ads')
        self.assertEqual(resolve_scope('@库存 帮我看库存','ads')[0],'inventory')
        self.assertEqual(resolve_scope('@全局 综合判断','ads')[0],'global')
