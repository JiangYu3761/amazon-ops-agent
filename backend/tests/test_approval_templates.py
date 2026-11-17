"""Business template coverage and static/API definition consistency."""
import json
import os
from pathlib import Path
import unittest

from app.approval_templates import ALIASES, DETAILS, static_registry, template_for
from app.process_rules import FormField


class ApprovalTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(__file__).resolve().parents[2]
        # Tests copied to an API container use its immutable catalog snapshot.
        path=Path(os.getenv('CATALOG_FILE',str(cls.root/'frontend/catalog.json')))
        cls.catalog=json.loads(path.read_text())

    def test_every_catalog_matter_has_specific_business_fields_and_stages(self):
        self.assertEqual(len(self.catalog['tasks']),79)
        for task in self.catalog['tasks']:
            with self.subTest(task=task['key']):
                template=template_for({**task,'sop_key':task['key']})
                self.assertEqual(template['key'],task['key'])
                self.assertGreaterEqual(len(template['fields']),4)
                self.assertGreaterEqual(len(template['stages']),4)
                self.assertEqual(template['stages'][0]['type'],'application')
                self.assertEqual(template['stages'][-1]['type'],'approval')
                self.assertTrue(any(s['type']=='handling' for s in template['stages']))
                self.assertNotIn('summary',{f['id'] for f in template['fields']})
                self.assertNotIn('result',{f['id'] for f in template['fields']})
                keys=[s['key'] for s in template['stages']]
                self.assertEqual(len(set(keys)),len(keys))
                for stage in template['stages']:
                    if stage.get('return_to'):
                        self.assertIn(stage['return_to'],keys)
                        self.assertLess(keys.index(stage['return_to']),keys.index(stage['key']))
                    for field in stage['fields']:
                        FormField(**field)
                # Do not put assumed real people, dates or status into a template.
                self.assertFalse(any(k in s for s in template['stages'] for k in ('assignee_id','reviewer_ids','actual_end','status')))

    def test_fcc_has_seven_real_handoffs_and_draft_revision_loop(self):
        template=template_for({'sop_key':'fcc-certification'})
        self.assertEqual([s['key'] for s in template['stages']],['application','manager_review','testing','draft_feedback','draft_review','original','archive_review'])
        self.assertEqual(template['stages'][4]['return_to'],'draft_feedback')
        self.assertEqual(template['stages'][6]['return_to'],'original')
        fields={f['id'] for s in template['stages'] for f in s['fields']}
        self.assertTrue({'planned_draft_date','planned_original_date','accepted_on','draft_received_on','issued_on','received_on','external_contact','report_number','archive_location'}.issubset(fields))
        for key in ('application_files','acceptance_receipt','draft_files','original_files'):
            self.assertTrue(next(f for s in template['stages'] for f in s['fields'] if f['id']==key)['required'])

    def test_three_compliance_matters_are_independent_with_real_dependencies(self):
        tasks={t['key']:t for t in self.catalog['tasks']}
        for key in ('fcc-certification','dangerous-goods','label-confirmation'):
            self.assertIn(key,tasks)
        self.assertTrue({'fcc-certification','dangerous-goods'}.issubset(tasks['label-confirmation']['depends_on']))
        self.assertEqual(len({template_for({'sop_key':k})['name'] for k in ('fcc-certification','dangerous-goods','label-confirmation')}),3)
        for old,new in ALIASES.items():
            self.assertEqual(template_for({'sop_key':old})['key'],new)
        self.assertEqual(template_for({'title':'确认FCC、危险品及标签状态'})['key'],'fcc-certification')

    def test_templates_are_returned_as_independent_snapshots(self):
        first=template_for({'sop_key':'fcc-certification'})
        first['stages'][0]['fields'][0]['label']='changed'
        second=template_for({'sop_key':'fcc-certification'})
        self.assertNotEqual(second['stages'][0]['fields'][0]['label'],'changed')

    def test_each_catalog_item_uses_an_explicit_definition(self):
        known=set(DETAILS)|{'fcc-certification','dangerous-goods','label-confirmation'}
        self.assertEqual({t['key'] for t in self.catalog['tasks']},known)
        labels=[tuple(f['label'] for f in template_for({'sop_key':t['key']})['fields']) for t in self.catalog['tasks']]
        self.assertEqual(len(set(labels)),79)

    def test_static_preview_matches_the_server_business_registry(self):
        path=self.root/'frontend/approval-templates.json'
        if not path.exists():
            # Docker images have the API catalog, but no frontend build assets.
            # The host suite verifies the actual generated file; this assertion
            # still verifies complete serialization inside the API image.
            self.assertEqual(len(static_registry(self.catalog)['templates']),82)
            return
        self.assertEqual(json.loads(path.read_text()),static_registry(self.catalog))
