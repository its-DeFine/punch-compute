#!/usr/bin/env python3
"""Focused maintenance-gate coverage with real module/file state.

Drives the actual app/maintenance.py against file-backed gateway state
(orders + maintenance + model-operation JSON files, no HTTP): the native
side is a synthetic api() callback seam returning GetToken-shaped readbacks
(masked key, status, remain/used quotas, strict_text_policy plus the
strict_reservations summary exactly where native attaches it), and the real
newapi_models.disable_token contract performs the drains. No Docker,
services, secrets, or chain. Run with the model venv python (model-operation
classification reuses model_orders terminal states, like the gateway).
"""
import copy
import json
import os
import re
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'app'))

from maintenance import Maintenance

POLICY = {'input_rate': 1.0, 'output_rate': 5.0, 'cache_rate': 1.0,
          'context_tokens': 64, 'max_output_tokens': 8,
          'reasoning_effort': 'low'}


class Failure(Exception):
    def __init__(self, code, status=502):
        super().__init__(code)
        self.code, self.status = code, status


class FakeGateway:
    def __init__(self, data_dir):
        self.DATA = data_dir
        self.ORDERS_FILE = os.path.join(data_dir, 'orders.json')
        self.IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
        self.Failure = Failure
        self.STATE_LOCK = threading.RLock()
        self.tokens = {}
        self.api_calls = []

    def load(self, path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    def save(self, path, value):
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(value, f)
        os.replace(tmp, path)

    def owned_session(self, order):
        return 'sess'

    def admin(self):
        return 'admin-token'

    def api(self, method, path, body=None, token=None):
        self.api_calls.append((method, path, body))
        if method == 'GET' and path.startswith('/api/token/'):
            tid = int(path[len('/api/token/'):])
            if tid not in self.tokens:
                raise Failure('UPSTREAM_HTTP_404', 404)
            return copy.deepcopy(self.tokens[tid])
        if method == 'PUT' and path == '/api/token/?status_only=true':
            tid = body['id']
            if tid not in self.tokens:
                raise Failure('UPSTREAM_HTTP_404', 404)
            self.tokens[tid]['status'] = body['status']
            return {'id': tid, 'status': body['status']}
        raise AssertionError('unexpected api call %r %r' % (method, path))


def token_fixture(tid=7, summary=None, **over):
    token = {'id': tid, 'user_id': 42, 'name': 'ord-1', 'status': 1,
             'remain_quota': 100, 'used_quota': 0, 'unlimited_quota': False,
             'expired_time': -1, 'model_limits': 'm',
             'model_limits_enabled': True, 'strict_text_policy': dict(POLICY),
             'key': 'sk-***masked***'}
    if summary is not None:
        token['strict_reservations'] = dict(summary)
    token.update(over)
    return token


def order_fixture(**over):
    order = {'buyer_user_id': 42, 'managed_username': 'ord-1',
             'managed_password': 'x', 'state': 'READY', 'model': 'm',
             'token_id': 7, 'offer_id': 'o', 'allowance_credits': 100,
             'window_seconds': 3600, 'expires_at': 9999999999,
             'purchase_intent': 'pi'}
    order.update(over)
    return order


class MaintenanceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gw = FakeGateway(self.tmp.name)
        self.gw.save(self.gw.ORDERS_FILE, {})
        self.maint = Maintenance(self.gw)

    def save_orders(self, mapping):
        self.gw.save(self.gw.ORDERS_FILE, mapping)

    def save_ops(self, mapping):
        with open(os.path.join(self.tmp.name, 'model_operations.json'), 'w') as f:
            json.dump(mapping, f)

    def codes(self, view):
        return [b['code'] for b in view.get('blockers', [])]

    def test_acquire_clean_and_idempotent_replay(self):
        view = self.maint.acquire('up-1', 'seller')
        self.assertEqual(view['admission'], 'closed')
        self.assertEqual(view['operation']['state'], 'HELD')
        self.assertEqual(view['blockers'], [])
        self.assertFalse(self.maint.admission_open())
        replay = self.maint.acquire('up-1', 'seller')
        self.assertEqual(replay['operation']['state'], 'HELD')
        status = self.maint.status()
        self.assertEqual(status['admission'], 'closed')
        self.assertEqual(status['blockers'], [])

    def test_acquire_refuses_usable_order(self):
        self.save_orders({'o1': order_fixture()})
        self.gw.tokens[7] = token_fixture(summary={'reserved_count': 0, 'dispatched_count': 0, 'held_quota': 0})
        view = self.maint.acquire('up-1', 'seller')
        self.assertEqual(view['operation']['state'], 'REFUSED')
        self.assertIn('USABLE_ORDER', self.codes(view))
        self.assertEqual(view['admission'], 'closed')
        self.assertFalse(self.maint.admission_open())

    def test_unsafe_states_and_uncertain_receipt(self):
        for state in ('PREPARED', 'PREPARING', 'FUNDED'):
            with self.subTest(state=state):
                self.save_orders({'o1': order_fixture(state=state)})
                view = Maintenance(self.gw).acquire('up-1', 'seller')
                self.assertEqual(view['operation']['state'], 'REFUSED')
                self.assertIn('UNSAFE_ORDER_STATE', self.codes(view))
        self.save_orders({'o1': order_fixture(
            state='PREPARING', native_receipt={'ticket_hash': '0' * 64})})
        view = Maintenance(self.gw).acquire('up-1', 'seller')
        self.assertIn('ORDER_UNCERTAIN', self.codes(view))

    def test_model_operation_activity_and_uncertainty(self):
        self.save_orders({'o1': order_fixture(state='READY', expires_at=1)})
        self.gw.tokens[7] = token_fixture(
            status=2, summary={'reserved_count': 0, 'dispatched_count': 0,
                               'held_quota': 0})
        self.save_ops({'op-1': {'state': 'QUEUED'}})
        view = self.maint.acquire('up-1', 'seller')
        self.assertIn('MODEL_OPERATION_ACTIVE', self.codes(view))
        self.save_ops({'op-1': {'state': 'UNAVAILABLE'}})
        view = Maintenance(self.gw).acquire('up-2', 'seller')
        self.assertIn('MODEL_OPERATION_UNCERTAIN', self.codes(view))
        self.save_ops({'op-1': {'state': 'FAILED', 'restored': True}})
        view = Maintenance(self.gw).acquire('up-3', 'seller')
        self.assertEqual(view['operation']['state'], 'HELD', view)

    def test_reservation_hold_blocks(self):
        self.save_orders({'o1': order_fixture(state='READY', expires_at=1)})
        self.gw.tokens[7] = token_fixture(
            status=2, summary={'reserved_count': 0, 'dispatched_count': 1,
                               'held_quota': 600})
        view = self.maint.acquire('up-1', 'seller')
        self.assertIn('RESERVATION_HOLD', self.codes(view))
        hold = next(b for b in view['blockers'] if b['code'] == 'RESERVATION_HOLD')
        self.assertEqual(hold['detail']['dispatched_count'], 1)

    def test_native_unavailable_fails_closed(self):
        self.save_orders({'o1': order_fixture(state='READY', expires_at=1)})
        view = self.maint.acquire('up-1', 'seller')
        self.assertIn('NATIVE_UNAVAILABLE', self.codes(view))
        # Policy token whose readback carries no summary fails closed.
        self.gw.tokens[7] = token_fixture(status=2)
        self.assertNotIn('strict_reservations', self.gw.tokens[7])
        view = Maintenance(self.gw).acquire('up-2', 'seller')
        self.assertIn('NATIVE_SUMMARY_UNAVAILABLE', self.codes(view))

    def test_drain_expired_preserves_balances(self):
        self.save_orders({'o1': order_fixture(state='READY', expires_at=1)})
        self.gw.tokens[7] = token_fixture(
            summary={'reserved_count': 0, 'dispatched_count': 0, 'held_quota': 0})
        before = copy.deepcopy(self.gw.tokens[7])
        view = self.maint.acquire('up-1', 'seller')
        self.assertEqual(view['operation']['state'], 'HELD', view)
        self.assertEqual(view['operation']['detail']['drained'], ['o1'])
        token = self.gw.tokens[7]
        self.assertEqual(token['status'], 2)
        for key in ('remain_quota', 'used_quota'):
            self.assertEqual(token[key], before[key])

    def test_drain_exhausted_token(self):
        self.save_orders({'o1': order_fixture()})
        self.gw.tokens[7] = token_fixture(
            remain_quota=0,
            summary={'reserved_count': 0, 'dispatched_count': 0, 'held_quota': 0})
        view = self.maint.acquire('up-1', 'seller')
        self.assertEqual(view['operation']['state'], 'HELD', view)
        self.assertEqual(self.gw.tokens[7]['remain_quota'], 0)
        self.assertEqual(self.gw.tokens[7]['used_quota'], 0)
        self.assertIn('o1', view['operation']['detail']['drained'])
        self.assertEqual(self.gw.tokens[7]['status'], 2)

    def test_unknown_expiry_never_drains_an_order(self):
        self.save_orders({'o1': order_fixture(expires_at='unknown')})
        self.gw.tokens[7] = token_fixture(summary={'reserved_count':0,'dispatched_count':0,'held_quota':0})
        view = self.maint.acquire('up-1','seller')
        self.assertEqual(view['operation']['state'],'REFUSED',view)
        self.assertIn('ORDER_UNREADABLE',self.codes(view))
        self.assertFalse(any(call[0]=='PUT' for call in self.gw.api_calls))

    def test_release_ownership_and_retirement(self):
        self.maint.acquire('up-1', 'seller')
        with self.assertRaises(Failure) as raised:
            self.maint.release('up-1', 'intruder')
        self.assertEqual(raised.exception.code, 'OPERATION_OWNER_MISMATCH')
        self.assertFalse(self.maint.admission_open())
        with self.assertRaises(Failure) as raised:
            self.maint.release('up-9', 'seller')
        self.assertEqual(raised.exception.code, 'NO_OPERATION')
        view = self.maint.release('up-1', 'seller')
        self.assertEqual(view['admission'], 'open')
        self.assertEqual(view['operation']['state'], 'RELEASED')
        self.assertTrue(self.maint.admission_open())
        with self.assertRaises(Failure) as raised:
            self.maint.acquire('up-1', 'seller')
        self.assertEqual(raised.exception.code, 'OPERATION_RETIRED')
        view = self.maint.acquire('up-2', 'seller')
        self.assertEqual(view['operation']['state'], 'HELD')

    def test_busy_second_operation_and_bad_identity(self):
        self.maint.acquire('up-1', 'seller')
        with self.assertRaises(Failure) as raised:
            self.maint.acquire('up-2', 'seller')
        self.assertEqual(raised.exception.code, 'OPERATION_BUSY')
        with self.assertRaises(Failure) as raised:
            self.maint.acquire('up-1', 'intruder')
        self.assertEqual(raised.exception.code, 'OPERATION_OWNER_MISMATCH')
        with self.assertRaises(Failure) as raised:
            self.maint.acquire('bad id!', 'seller')
        self.assertEqual(raised.exception.code, 'OPERATION_ID_INVALID')

    def test_restart_preserves_closed_admission(self):
        self.maint.acquire('up-1', 'seller')
        restarted = Maintenance(self.gw)
        self.assertFalse(restarted.admission_open())
        status = restarted.status()
        self.assertEqual(status['operation']['state'], 'HELD')
        view = restarted.release('up-1', 'seller')
        self.assertTrue(restarted.admission_open())
        self.assertEqual(view['operation']['state'], 'RELEASED')


if __name__ == '__main__':
    unittest.main(verbosity=2)
