"""Exercise FBM order quantities, package states and replay permissions in PostgreSQL."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from test_erp_receiving import WarehouseFixture


BASE = '/api/erp-fulfillment'


@unittest.skipUnless(os.getenv('WORKFLOW_INTEGRATION') == '1', 'requires disposable PostgreSQL')
class FulfillmentTests(WarehouseFixture, unittest.TestCase):
    def setUp(self):
        self.scope_config = {
            'account_ref': 'fbm-' + uuid4().hex, 'site': 'US', 'warehouse': 'West',
            'business_timezone': 'America/Los_Angeles', 'member_ids': [self.ids['warehouse']],
        }
        self.scope = self.post('/scopes', self.scope_config, 201, 'admin')['id']
        self.addCleanup(self.remove_orders)

    def remove_orders(self):
        from app.db import connection

        with connection() as conn:
            for table in ('erp_fbm_requests', 'erp_fbm_events', 'erp_fbm_package_lines',
                          'erp_fbm_packages', 'erp_fbm_lines'):
                conn.execute('DELETE FROM ' + table +
                             ' WHERE order_id IN (SELECT id FROM erp_fbm_orders WHERE scope_id=%s)', (self.scope,))
            conn.execute('DELETE FROM erp_fbm_orders WHERE scope_id=%s', (self.scope,))

    def command(self, path, data, status=200, who='warehouse'):
        result = self.clients[who].post(BASE + path, json={'request_id': str(uuid4()), **data})
        self.assertEqual(result.status_code, status, result.text)
        return result.json()

    def order_body(self, paid=True):
        return {'scope_id': self.scope, 'number': 'FBM-' + uuid4().hex[:12], 'source_ref': '订单回执-248',
                'origin': 'platform', 'paid': paid,
                'lines': [{'line_ref': 'A', 'sku': 'red', 'qty': 4}, {'line_ref': 'B', 'sku': 'blue', 'qty': 3}]}

    def package(self, order, name, allocations, status=201):
        return self.command('/orders/' + order['id'] + '/packages',
                            {'package_ref': name, 'lines': [{'line_id': order['lines'][i]['id'], 'qty': amount}
                                                           for i, amount in allocations]}, status)

    def event(self, package, action, evidence, status=200, **extra):
        return self.command('/packages/' + package['id'] + '/events',
                            {'action': action, 'expected_version': package['version'], 'evidence': evidence, **extra},
                            status)

    def test_payment_gate_replay_and_recorded_evidence(self):
        create = {**self.order_body(paid=False), 'request_id': str(uuid4())}
        order = self.command('/orders', create, 201)
        self.assertEqual(self.command('/orders', create, 201)['id'], order['id'])
        self.command('/orders', {**create, 'source_ref': '另一个来源'}, 409)
        self.command('/orders', {**create, 'request_id': str(uuid4())}, 409)
        order = self.package(order, 'BOX-A', [(0, 1)])
        package = order['packages'][0]
        self.assertNotIn('release', package['actions'])
        self.event(package, 'release', '仓库放行单-1', 409)
        self.event(package, 'dispatch', '出仓扫码-88', 409)
        path = '/orders/' + order['id'] + '/payment'
        self.command(path, {'evidence': '   '}, 422)
        receipt = {'request_id': str(uuid4()), 'evidence': '付款回执-248'}
        paid = self.command(path, receipt)
        self.assertTrue(paid['paid'])
        self.assertIn('release', paid['packages'][0]['actions'])
        self.assertEqual(self.command(path, receipt)['id'], order['id'])
        self.command(path, {'evidence': '另一条收款记录'}, 409)
        self.event(package, 'release', '   ', 422)
        released = self.event(package, 'release', '仓库放行单-1')
        self.assertEqual(released['packages'][0]['status'], 'released')
        self.event(released['packages'][0], 'dispatch', '', 422)
        shipped = self.event(released['packages'][0], 'dispatch', '承运交接回执-288')
        self.assertEqual(shipped['packages'][0]['status'], 'shipped')
        self.assertEqual([(e['event'], e['evidence'], e['actor_id']) for e in reversed(shipped['events'])], [
            (e, ref, self.ids['warehouse']) for e, ref in [
                ('create', create['source_ref']), ('allocate', 'BOX-A'), ('payment', receipt['evidence']),
                ('release', '仓库放行单-1'), ('dispatch', '承运交接回执-288'),
            ]])

    def test_two_skus_split_and_atomic_rejection(self):
        order = self.command('/orders', self.order_body(), 201)
        first = self.package(order, 'LEFT', [(0, 3), (1, 1)])
        self.assertEqual([l['remaining'] for l in first['lines']], [1, 2])
        self.package(first, 'LEFT', [(0, 1)], 409)
        self.package(first, 'BAD', [(0, 1), (1, 3)], 409)
        self.package(first, 'DUP', [(0, 1), (0, 1)], 422)
        foreign_order = self.command('/orders', self.order_body(), 201)
        self.command('/orders/' + order['id'] + '/packages', {
            'package_ref': 'WRONG-ORDER', 'lines': [{'line_id': foreign_order['lines'][0]['id'], 'qty': 1}]}, 409)
        complete = self.package(order, 'RIGHT', [(0, 1), (1, 2)])
        self.assertEqual([l['remaining'] for l in complete['lines']], [0, 0])
        self.assertEqual([[ (l['line_ref'], l['qty']) for l in p['lines']] for p in complete['packages']],
                         [[('A', 3), ('B', 1)], [('A', 1), ('B', 2)]])
        self.assertEqual(len([e for e in complete['events'] if e['event'] == 'allocate']), 2)
        self.package(order, 'OVERFLOW', [(0, 1)], 409)
        found = self.clients['warehouse'].get(BASE + '/orders', params={'scope_id':self.scope,'search':'rIgHt'})
        self.assertEqual([row['id'] for row in found.json()['items']], [order['id']])

    def test_each_package_responds_to_its_own_cancellation_and_evidence(self):
        order = self.command('/orders', self.order_body(), 201)
        order = self.package(order, 'PRE', [(0, 1)])
        order = self.package(order, 'POST', [(0, 1)])
        order = self.package(order, 'LATE', [(1, 1)])

        def advance(name, action, proof, expected):
            nonlocal order
            before = {p['package_ref']: p for p in order['packages']}
            order = self.event(before[name], action, proof)
            after = {p['package_ref']: p for p in order['packages']}
            self.assertEqual((after[name]['status'], after[name]['version']), (expected,before[name]['version']+1))
            for sibling in before.keys() - {name}:
                self.assertEqual((after[sibling]['status'],after[sibling]['version']),
                                 (before[sibling]['status'],before[sibling]['version']))
            self.assertEqual((order['events'][0]['package_id'],order['events'][0]['evidence']), (after[name]['id'],proof))

        advance('PRE', 'buyer_cancel', '买家请求-1', 'hold')
        advance('PRE', 'withdraw_cancel', '买家撤回-1', 'review')
        advance('PRE', 'buyer_cancel', '买家请求-2', 'hold')
        advance('PRE', 'confirm_cancel', '仓库仍未放行-2', 'cancelled')
        advance('POST', 'release', '放行签字-3', 'released')
        advance('POST', 'buyer_cancel', '买家请求-3', 'intercepting')
        advance('POST', 'intercept_failed', '拦截失败-3', 'stop_failed')
        advance('POST', 'retry_intercept', '再次催截-3', 'intercepting')
        advance('POST', 'intercept_failed', '仓方二次拒截-3', 'stop_failed')
        advance('POST', 'dispatch', '发运交接单-3', 'after_dispatch')
        advance('POST', 'delivered_after_cancel', '承运商签收回执-4', 'closed_shipped')
        advance('LATE', 'release', '放行签字-5', 'released')
        advance('LATE', 'buyer_cancel', '买家请求-5', 'intercepting')
        advance('LATE', 'dispatch', '晚到的发运回执-5', 'after_dispatch')
        advance('LATE', 'intercept_ok', '承运商拦截回执-5', 'cancelled')
        self.event(order['packages'][0], 'withdraw_cancel', '关单后变更', 409)

    def test_access_rechecked_on_replay_after_warehouse_revocation(self):
        create = {**self.order_body(), 'request_id': str(uuid4())}
        order = self.command('/orders', create, 201)
        path = '/orders/' + order['id']
        self.assertEqual(self.anonymous.get(BASE + path).status_code, 401)
        self.assertEqual(self.clients['outside'].get(BASE + path).status_code,404)
        self.command('/orders', create, 404, 'outside')
        self.post('/scopes', {**self.scope_config, 'member_ids': []}, 201, 'admin')
        self.assertEqual(self.clients['warehouse'].get(BASE + '/orders',params={'scope_id':self.scope}).status_code,404)
        self.command('/orders', create, 404)
        self.assertEqual(self.clients['admin'].get(BASE + path).json()['source_ref'], create['source_ref'])

    def test_concurrent_retries_quantities_and_version_races(self):
        order = self.command('/orders', self.order_body(), 201)
        path = '/orders/' + order['id'] + '/packages'
        def race(endpoint, pair):
            barrier = Barrier(2)
            def send(payload):
                barrier.wait(timeout=10)
                return self.clients['warehouse'].post(BASE + endpoint, json=payload)
            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(send, pair))

        same = {'request_id':str(uuid4()), 'package_ref':'RETRY',
                'lines':[{'line_id':order['lines'][0]['id'],'qty':2}]}
        first,second = race(path,[same,same])
        self.assertEqual((first.status_code,second.status_code),(201,201))
        package = first.json()['packages'][0]
        self.assertEqual(second.json()['packages'][0]['id'], package['id'])
        competing = [{**same,'request_id':str(uuid4()),'package_ref':name} for name in ('LEFT','RIGHT')]
        results = race(path,competing)
        self.assertEqual(sorted(r.status_code for r in results),[201,409])
        results = race('/packages/' + package['id'] + '/events', [
            {'action':'release','request_id':str(uuid4()),'expected_version':package['version'],'evidence':proof}
            for proof in ('仓库放行回执-201','仓库放行回执-202')])
        self.assertEqual(sorted(r.status_code for r in results),[200,409])
        detail = self.clients['warehouse'].get(BASE + '/orders/' + order['id']).json()
        self.assertEqual([l['remaining'] for l in detail['lines']], [0, 3])
        self.assertEqual(len(detail['packages']),2)
        self.assertEqual([e['event'] for e in reversed(detail['events'])],['create','allocate','allocate','release'])
