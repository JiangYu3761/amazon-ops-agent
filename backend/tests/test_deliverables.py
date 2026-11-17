import unittest
from app.deliverable_rules import build_delivery_rows,delivery_catalog,item_meta,preparation_state

class DeliveryRulesTests(unittest.TestCase):
    def setUp(self):
        self.catalog=delivery_catalog()
        self.task={'id':'t1','sop_key':'capability','title':'产品能力确认','status':'completed','owner':'运营','phase':'上线前'}
        self.meta=item_meta(self.task,self.catalog)

    def test_four_categories_cover_all_existing_work(self):
        self.assertEqual(len(self.catalog['delivery_categories']),4)
        categories={x['id'] for x in self.catalog['delivery_categories']}
        self.assertTrue(all(t['delivery']['category'] in categories for t in self.catalog['tasks']))
        self.assertTrue(all(t['delivery']['expected_files'] for t in self.catalog['tasks']))

    def test_completed_work_does_not_imply_file_readiness(self):
        self.assertEqual(preparation_state(self.task,[],self.meta),'missing')
        refs=[{'preparation_status':'prepared'}]
        self.assertEqual(preparation_state(self.task,refs,self.meta),'ready')
        self.task['status']='in_progress'
        self.assertEqual(preparation_state(self.task,refs,self.meta),'file_ready')

    def test_historical_documents_are_excluded_and_signed_queries_are_not_sent_to_ai(self):
        refs=[{'task_id':'t1','title':'能力表','url':'https://example.org/file?token=secret','version':'v1','preparation_status':'prepared','is_current':False}]
        data=build_delivery_rows([self.task],refs,[],self.catalog,{})
        self.assertEqual(data[0]['preparation_state'],'missing')
        refs[0]['is_current']=True
        data=build_delivery_rows([self.task],refs,[],self.catalog,{})
        self.assertEqual(data[0]['documents'][0]['link_host'],'example.org')
        self.assertNotIn('secret',repr(data))
        self.assertNotIn('url',data[0]['documents'][0])

    def test_category_and_filename_filters_respect_current_page(self):
        rows=build_delivery_rows([self.task],[],[],self.catalog,{'delivery_category':'advertising'})
        self.assertEqual(rows,[])
        rows=build_delivery_rows([self.task],[],[],self.catalog,{'search':'产品能力与参数确认表'})
        self.assertEqual(len(rows),1)
