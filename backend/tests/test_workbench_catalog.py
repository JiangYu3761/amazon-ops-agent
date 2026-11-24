"""Check the work forms used by both clients and the API, without a database."""
import re
import unittest
from datetime import date

from fastapi import HTTPException

from app.workbench_definitions import pages, validate_values
from app.workbench_rules import ARITY, evaluate_checks


PAGE_IDS = {
    'overview', 'deliverables', 'process', 'workflow', 'team', 'shop', 'listing', 'ads',
    'inventory', 'calendar', 'finance', 'service', 'compliance', 'ranking', 'agent',
    'imports', 'settings',
}
TODAY = date(2026, 11, 24)
FIELD_ID = re.compile(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*')


def valid_form(section):
    """Use typed, distinct examples and satisfy each declared cross-field condition."""
    values = {}
    for field in section['fields']:
        key, kind = field['key'], field['type']
        if kind == 'number':
            values[key] = max(field.get('min', 0), min(field.get('max', 100), 10))
        elif kind == 'date':
            values[key] = TODAY.isoformat()
        elif kind == 'select':
            excluded = {c['when'] for c in section['checks'] if c['op'] == 'block_if' and c['keys'][0] == key}
            values[key] = next(option for option in field['options'] if option not in excluded)
        elif kind == 'url':
            values[key] = 'https://example.com/evidence/' + key
        else:
            values[key] = '测试记录：' + field['label']
    for check in section['checks']:
        if check['op'] == 'eq':
            left, right = check['keys']
            values[right] = values[left]
    return values


def invalid_form(section, check):
    """Return a validly typed form that should be refused at submission."""
    values = valid_form(section)
    op = check['op']
    left, *rest = check['keys']
    if op == 'block_if':
        values[left] = check['when']
    elif op == 'required_if':
        values[left] = check['when']
        values.pop(rest[0])
    elif op == 'neq':
        values[rest[0]] = values[left]
    elif op in {'lte', 'gte', 'eq'}:
        field_type = next(f['type'] for f in section['fields'] if f['key'] == left)
        small, large = {
            'number': (9, 10), 'date': ('2026-11-24', '2026-11-25'),
            'text': ('v1', 'v2'), 'textarea': ('v1', 'v2'),
        }[field_type]
        values[left], values[rest[0]] = (large, small) if op == 'lte' else (small, large)
    else:
        raise ValueError(op)
    return values


class WorkbenchCatalogTests(unittest.TestCase):
    def test_every_page_and_form_has_runnable_business_definitions(self):
        self.assertEqual(set(pages()), PAGE_IDS)
        for page_id, page in pages().items():
            with self.subTest(page=page_id):
                self.assertTrue(all(page.get(k) for k in ('title', 'purpose', 'roles', 'scope', 'flow')))
                self.assertTrue(all(page['ai'].get(k) for k in ('inputs', 'draft', 'human', 'example')))
                self.assertGreaterEqual(len(page['rules']), 3)
                self.assertTrue(all(all(rule.get(k) for k in ('name', 'inputs', 'logic', 'missing', 'output')) for rule in page['rules']))
                self.assertGreaterEqual(len(page['tests']), 3)
                self.assertTrue(all(all(test.get(k) for k in ('input', 'action', 'expect')) for test in page['tests']))
                self.assertGreaterEqual(len(page['sections']), 3)
                self.assertEqual(len({s['id'] for s in page['sections']}), len(page['sections']))
            for section in page['sections']:
                with self.subTest(page=page_id, section=section['id']):
                    self.assertRegex(section['id'], r'^[a-z][a-z0-9_]*$')
                    self.assertTrue(all(section.get(k) for k in ('purpose', 'source', 'action', 'output', 'checks')))
                    fields = {field['key']: field for field in section['fields']}
                    self.assertGreaterEqual(len(fields), 3)
                    self.assertEqual(len(fields), len(section['fields']))
                    for key, field in fields.items():
                        self.assertIsNotNone(FIELD_ID.fullmatch(key))
                        self.assertIsInstance(field['required'], bool)
                        self.assertTrue(field['label'] and field['help'])
                        self.assertIn(field['type'], {'text', 'textarea', 'number', 'date', 'select', 'url'})
                        if field['type'] == 'select':
                            self.assertTrue(field['options'])
                            self.assertEqual(len(field['options']), len(set(field['options'])))
                    for check in section['checks']:
                        op = check['op']
                        self.assertEqual(len(check['keys']), ARITY[op])
                        self.assertTrue(set(check['keys']) <= set(fields))
                        kinds = [fields[k]['type'] for k in check['keys']]
                        if op == 'deadline':
                            self.assertEqual(kinds, ['date'])
                        if op in {'ratio', 'difference'}:
                            self.assertEqual(kinds, ['number', 'number'])
                        if op in {'block_if', 'required_if'}:
                            self.assertIn(check['when'], fields[check['keys'][0]]['options'])
                        if op in {'lte', 'gte', 'eq', 'neq'}:
                            self.assertEqual(kinds[0], kinds[1])
                    sample = valid_form(section)
                    validate_values(section['fields'], sample, submitting=True)
                    issues = evaluate_checks(section, sample, TODAY)
                    self.assertFalse(any(f['level'] == 'block' for f in issues), issues)

    def test_input_types_required_fields_and_conditional_guards(self):
        invalid = {'number': True, 'date': '2026-02-30', 'select': '未定义选项',
                   'url': 'relative/file', 'text': {}, 'textarea': []}
        for page_id, page in pages().items():
            for section in page['sections']:
                good = valid_form(section)
                for field in section['fields']:
                    key = field['key']
                    with self.subTest(page=page_id, section=section['id'], field=key):
                        with self.assertRaises(HTTPException):
                            validate_values(section['fields'], {**good, key: invalid[field['type']]}, submitting=True)
                        if field['required']:
                            missing = {k: v for k, v in good.items() if k != key}
                            validate_values(section['fields'], missing, submitting=False)
                            with self.assertRaises(HTTPException):
                                validate_values(section['fields'], missing, submitting=True)
                for check in section['checks']:
                    if check['op'] in {'lte', 'gte', 'eq', 'neq', 'required_if', 'block_if'}:
                        with self.subTest(page=page_id, section=section['id'], check=check['label']):
                            bad = invalid_form(section, check)
                            result = evaluate_checks(section, bad, TODAY)
                            self.assertTrue(any(r['level'] == 'block' and r['fields'] == check['keys'] for r in result), result)

    def test_representative_operator_scenario_for_each_page(self):
        # Assert outputs against independently specified examples. Reversing a ratio,
        # changing a condition option, or using an internal deadline in place of a
        # platform cutoff can still pass a structural definition check.
        cases = [
            ('overview', 'quarter_portfolio', {'planned_spend': 110, 'approved_budget': 100}, 'planned_spend', 'block', None),
            ('deliverables', 'revision', {'previous_version': 'v2', 'new_version': 'v2'}, 'previous_version', 'block', None),
            ('process', 'dependency', {'upstream_task': 'SOP-1', 'downstream_task': 'SOP-1'}, 'upstream_task', 'block', None),
            ('workflow', 'applicability', {'applicable': '需补证'}, 'applicable', 'block', None),
            ('team', 'review_packet', {'has_return': '有退回'}, 'has_return', 'block', None),
            ('settings', 'upgrade_check', {'recovery_status': '演练未通过'}, 'recovery_status', 'block', None),
            ('shop', 'notice', {'next_follow_up': '2026-11-28', 'platform_deadline': '2026-11-23'}, 'platform_deadline', 'overdue', -1),
            ('listing', 'catalog_incident', {'issue_kind': '变体错挂'}, 'issue_kind', 'block', None),
            ('ads', 'investigation', {'spend': 100, 'attributed_sales': 400}, 'spend', 'ok', '25'),
            ('calendar', 'readiness', {'available_units': 70, 'forecast_units': 150}, 'available_units', 'ok', '-80'),
            ('inventory', 'receiving', {'shipped_qty': 144, 'received_qty': 132}, 'received_qty', 'ok', '-12'),
            ('finance', 'settlement', {'actual_net': 88, 'expected_net': 100}, 'actual_net', 'ok', '-12'),
            ('service', 'returns', {'return_qty': 2, 'ordered_qty': 40}, 'return_qty', 'ok', '5'),
            ('compliance', 'certificate', {'phase': '已签发'}, 'phase', 'block', None),
            ('ranking', 'observation', {'outcome': '深度内未见'}, 'outcome', 'block', None),
            ('agent', 'effect_review', {'actual_value': 12.25, 'baseline_value': 10}, 'actual_value', 'ok', '2.25'),
            ('imports', 'validate_file', {'total_rows': 12, 'valid_rows': 9}, 'total_rows', 'ok', '3'),
        ]
        self.assertEqual({c[0] for c in cases}, PAGE_IDS)
        for page_id, section_id, fields, first_key, level, expected in cases:
            with self.subTest(page=page_id, section=section_id):
                section = next(s for s in pages()[page_id]['sections'] if s['id'] == section_id)
                found = [r for r in evaluate_checks(section, fields, TODAY) if r['fields'][0] == first_key]
                self.assertIn((level, expected), [(r['level'], r['value']) for r in found])

    def test_all_rule_operators_and_boundary_results(self):
        def evaluate(op, values, kind='number', when=None, today=TODAY):
            keys = ['a', 'b'][:ARITY[op]]
            check = {'op': op, 'keys': keys, 'label': op, 'message': '检查字段'}
            if when is not None:
                check['when'] = when
            return evaluate_checks({'fields': [{'key': k, 'type': kind, 'label': k} for k in keys],
                                    'checks': [check]}, dict(zip(keys, values)), today)

        for op, passing, failing in [('lte', (9, 10), (11, 10)), ('gte', (11, 10), (9, 10)),
                                     ('eq', (10, 10), (9, 10)), ('neq', (9, 10), (10, 10))]:
            self.assertEqual(evaluate(op, passing)[0]['level'], 'ok', op)
            self.assertEqual(evaluate(op, failing)[0]['level'], 'block', op)
            self.assertEqual(evaluate(op, (0, None))[0]['level'], 'missing', op)
        for when, values, level in [('是', ('是', ''), 'block'), ('是', ('是', '凭证'), None),
                                    ('是', ('否', ''), None)]:
            findings = evaluate('required_if', values, 'text', when)
            self.assertEqual(findings[0]['level'] if findings else None, level)
        self.assertEqual(evaluate('block_if', ['是'], 'text', '是')[0]['level'], 'block')
        self.assertEqual(evaluate('block_if', ['否'], 'text', '是'), [])
        for day, level in [('2026-11-23', 'overdue'), ('2026-11-24', 'due'), ('2026-11-25', 'info')]:
            self.assertEqual(evaluate('deadline', [day], 'date')[0]['level'], level)
        self.assertEqual(evaluate('deadline', ['2026-02-30'], 'date')[0]['level'], 'block')
        self.assertEqual(evaluate('ratio', [100, 400])[0]['value'], '25')
        self.assertEqual(evaluate('ratio', [0, 400])[0]['value'], '0')
        self.assertEqual(evaluate('ratio', [100, 0])[0]['level'], 'missing')
        self.assertEqual(evaluate('difference', [70, 150])[0]['value'], '-80')
        for op in ('difference', 'ratio'):
            self.assertEqual(evaluate(op, [1, None])[0]['level'], 'missing')
            self.assertEqual(evaluate(op, [1, 'bad'])[0]['level'], 'block')
