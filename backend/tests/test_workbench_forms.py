"""Submit and close each professional form through the real authenticated API."""
import os
import secrets
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

from test_workbench_catalog import invalid_form, valid_form


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1', 'requires a disposable workflow test database')
class WorkbenchFormIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(os.environ['DATABASE_URL']).path.startswith('/homa_workflow_test_'):
            raise RuntimeError('Use an isolated workflow test database')
        from fastapi.testclient import TestClient
        from app.main import app
        from app.db import connection
        from app.workflow_auth import password_hash

        password = secrets.token_urlsafe(24)
        password_digest = password_hash(password)
        prefix = uuid4().hex[:12]
        clients = {}
        identities = {}
        for role in ('handler', 'reviewer'):
            email = f'workbench-{prefix}-{role}@test.example'
            with connection() as conn:
                row = conn.execute('INSERT INTO app_users(email,display_name) VALUES(%s,%s) RETURNING id',
                                   (email, '工作台矩阵验收-' + role)).fetchone()
                identities[role] = str(row['id'])
                # The handler is a workflow admin solely to assign the independent reviewer on creation.
                conn.execute('INSERT INTO workflow_accounts(user_id,password_hash,is_admin) VALUES(%s,%s,%s)',
                             (row['id'], password_digest, role == 'handler'))
            client = TestClient(app, headers={'X-Workflow-Request': '1'})
            cls.addClassCleanup(client.close)
            login = client.post('/api/workflow-auth/login', json={'email': email, 'password': password})
            if login.status_code != 200:
                raise RuntimeError(f'Test account login failed with HTTP {login.status_code}')
            clients[role] = client
        cls.handler, cls.reviewer = clients['handler'], clients['reviewer']
        cls.identities = identities
        with connection() as conn:
            row = conn.execute("INSERT INTO products(model,name,marketplace) VALUES(%s,'工作台矩阵测试商品','US') RETURNING id",
                               ('workbench-' + prefix,)).fetchone()
            cls.product_id = str(row['id'])

    def test_submit_and_verify_every_form(self):
        from app.workbench_definitions import pages

        for page in pages().values():
            for section in page['sections']:
                with self.subTest(page=page['id'], form=section['id']):
                    self.exercise_form(page, section)

    def exercise_form(self, page, section):
        good = valid_form(section)
        required = next(field for field in section['fields'] if field['required'])
        fields = {k: value for k, value in good.items() if k != required['key']}
        source_id = f'test/{page["id"]}/{section["id"]}'
        create_body = {'request_id': str(uuid4()), 'product_id': self.product_id, 'page_id': page['id'],
                       'section_id': section['id'], 'title': section['title'], 'site': 'US', 'object_ref': source_id,
                       'reviewer_id': self.identities['reviewer'], 'values': fields}
        # An array is outside every supported scalar field type.
        malformed = self.handler.post('/api/workbench/items', json={**create_body, 'request_id': str(uuid4()),
                                                                    'values': {**good, required['key']: [1]}})
        self.assertEqual(malformed.status_code, 422, malformed.text)
        response = self.handler.post('/api/workbench/items', json=create_body)
        self.assertEqual(response.status_code, 201, response.text)
        draft = response.json()
        self.assertEqual(draft['status'], 'draft')
        item_path = '/api/workbench/items/' + draft['id']
        self.assertEqual(draft['object_ref'], source_id)
        self.assertEqual(draft['values'], fields)
        self.assertEqual(draft['definition']['id'], section['id'])

        def act(client, action, version, **payload):
            return client.post(item_path + '/actions',
                               json={'expected_version': version, 'action': action, 'request_id': str(uuid4()), **payload})

        def write(values, version):
            result = self.handler.patch(item_path, json={'expected_version': version, 'values': values})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['status'], 'draft')
            return result.json()['version']

        # A failed submission must preserve the original draft, revision and event ledger.
        version = draft['version']
        missing = act(self.handler, 'submit', version)
        self.assertEqual(missing.status_code, 422, missing.text)
        self.assertIn(required['label'], missing.text)
        for check in section['checks']:
            if check['op'] in ('lte', 'gte', 'eq', 'neq', 'required_if', 'block_if'):
                version = write(invalid_form(section, check), version)
                invalid = act(self.handler, 'submit', version)
                self.assertEqual(invalid.status_code, 422, invalid.text)
                current = self.handler.get(item_path).json()
                self.assertEqual((current['version'], current['status']), (version, 'draft'))

        version = write(good, version)
        for actor, action, status, payload in [
            (self.handler, 'submit', 'pending_review', {}),
            (self.reviewer, 'approve', 'approved', {}),
            (self.handler, 'execute', 'executed', {'evidence': '测试回执 ' + source_id}),
            (self.reviewer, 'verify', 'closed', {'note': '测试验收 ' + section['output']}),
        ]:
            result = act(actor, action, version, **payload)
            self.assertEqual(result.status_code, 200, result.text)
            item = result.json()
            self.assertEqual((item['status'], item['version']), (status, version + 1))
            version = item['version']
        self.assertEqual(item['values'], good)
        last = item['history'][-4:]
        self.assertEqual([event['action'] for event in last], ['submit', 'approve', 'execute', 'verify'])
        self.assertEqual(last[-2]['evidence'], '测试回执 ' + source_id)
        self.assertEqual(last[-1]['actor_id'], self.identities['reviewer'])
        self.assertEqual(last[-1]['snapshot']['values'], good)
        scope = {'product_id': self.product_id, 'page_id': page['id'], 'section_id': section['id']}
        listed = self.reviewer.get('/api/workbench/items', params=scope)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([it['id'] for it in listed.json()['items']], [draft['id']])
