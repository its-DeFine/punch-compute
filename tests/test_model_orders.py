#!/usr/bin/env python3
"""Focused ModelOrders crash/drain/expiry/restore coverage.

Runs with outputs/punch-token-orders-concept-20260920/model-venv/bin/python
(real gguf/hub; no module stubs). Executes the real model_orders module
against a synthetic file-backed gateway (JSON state files, no HTTP): router,
registry preparation, preset install and all native token/channel operations
are stubbed at the function seam, never via fabricated HTTP response schemas.
Native accounting is a tiny per-token ledger behind the real
newapi_models.check_token_steady. No network, secrets, funds, GPU, or chain.

Causal run: MODEL_ORDERS_UNDER_TEST=outputs/.../model-orders-before.py runs
the same cases against root's retained pre-fix snapshot. Cases C, D1, D2, E2,
E3, E4 and F are RED there; A, B and E1 pin seams both versions share.
"""
import copy
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.abspath(__file__)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
APP = os.path.abspath(os.path.join(os.path.dirname(HERE), '..', 'app'))
sys.path.insert(0, APP)
UNDER_TEST = os.environ.get(
    'MODEL_ORDERS_UNDER_TEST', os.path.join(APP, 'model_orders.py'))


def load_module(path):
    name = 'model_orders_case_' + hashlib.sha1(
        path.encode()).hexdigest()[:8]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mo = load_module(UNDER_TEST)

from model_registry import RegistryError  # noqa: E402
from model_runtime import ModelRuntimeError  # noqa: E402
from newapi_models import check_token_steady  # noqa: E402

mo.check_token_steady = check_token_steady

POLICY = {'input_rate': 1.0, 'output_rate': 5.0, 'cache_rate': 1.0,
          'context_tokens': 64, 'max_output_tokens': 8,
          'reasoning_effort': 'low'}
COVERAGE = {'max_total_parameters': 8000000000,
            'max_weight_bytes': 5000000000, 'architectures': ['llama'],
            'file_types': ['Q4_K_M'], 'min_context_tokens': 8,
            'text_only': True}
SELECTION = {'registry': 'huggingface', 'repository': 'owner/repo',
             'revision': 'a' * 40, 'file': 'm.gguf'}
IDENTITY = dict(SELECTION, size=100, sha256='ab' * 32)


class Failure(Exception):
    def __init__(self, code, status=502):
        super().__init__(code)
        self.code, self.status = code, status


class FileGateway:
    """Synthetic file-backed gateway. Direct api use is a failure: every
    external operation must arrive through a stubbed seam."""

    def __init__(self, data_dir):
        self.DATA = data_dir
        self.ORDERS_FILE = 'orders.json'
        self.IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
        self.Failure = Failure
        self.STATE_LOCK = threading.RLock()

    def _path(self, name):
        return os.path.join(self.DATA, name)

    def load(self, name):
        with open(self._path(name), encoding='utf-8') as f:
            return json.load(f)

    def save(self, name, value):
        tmp = self._path(name) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(value, f)
        os.replace(tmp, self._path(name))

    def api(self, *args, **kwargs):
        raise AssertionError('unstubbed api call: %r %r' % (args, kwargs))

    def owned_session(self, order):
        return 'sess'

    def admin(self):
        return 'admin-token'


class NativeLedger:
    """In-memory order-token ledger behind stubbed native operations."""

    def __init__(self, policy):
        self.policy = dict(policy)
        self.tokens = {}
        self.calls = []

    def add(self, tid, model='model-a', status=1):
        self.tokens[tid] = {'model_limits': model, 'status': status}

    def _read(self, tid):
        tok = self.tokens[tid]
        return {'id': tid, 'name': 'order-token', 'status': tok['status'],
                'model_limits': tok['model_limits'],
                'strict_text_policy': dict(self.policy),
                'remain_quota': 100, 'used_quota': 0, 'expired_time': -1}

    def disable_token(self, api, sess, tid):
        self.calls.append(('disable', tid))
        self.tokens[tid]['status'] = 2
        return self._read(tid)

    def enable_token(self, api, sess, tid):
        self.calls.append(('enable', tid))
        self.tokens[tid]['status'] = 1
        return self._read(tid)

    def cas_token_model(self, api, sess, tid, expected, new):
        self.calls.append(('cas', tid, expected, new))
        if expected != self.tokens[tid]['model_limits']:
            raise Failure('TOKEN_MODEL_UNRESOLVED')
        self.tokens[tid]['model_limits'] = new
        return dict(self._read(tid), model_limits=new)

    def read_token(self, api, sess, tid):
        self.calls.append(('read', tid))
        return self._read(tid)

    def ensure_model_channel(self, api, admin_token, **kwargs):
        self.calls.append(('channel', kwargs.get('name')))
        return {'id': 1, 'name': kwargs.get('name')}


class RouterStub:
    def __init__(self, loaded):
        self.loaded = set(loaded)
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0
        self.fail_loads = set()

    def models(self, reload=False):
        self.calls.append(('models', reload))
        return {m: {'status': {'value': 'loaded'}} for m in self.loaded}

    def unload(self, model_id, timeout=30):
        self.calls.append(('unload', model_id))
        self.loaded.discard(model_id)

    def load(self, model_id, timeout=120):
        self.calls.append(('load', model_id))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if model_id in self.fail_loads:
                raise ModelRuntimeError('injected load failure')
        finally:
            self.inflight -= 1
        self.loaded.add(model_id)


def operation_id(order_id, idem):
    return 'model-' + hashlib.sha256(
        (order_id + ':' + idem).encode()).hexdigest()


class ModelOrdersCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gw = FileGateway(self.tmp.name)
        self.router = RouterStub(['model-a'])
        self.native = NativeLedger(POLICY)
        self.native.add(7)
        self.prepared = {
            'identity': dict(IDENTITY), 'stats': {'context_length': 4096},
            'path': os.path.join(self.tmp.name, 'm.gguf')}
        self.prepare_error = None
        self.prepare_calls = []
        self.preset_calls = []

        saved = {name: getattr(mo, name) for name in (
            'Router', 'prepare_registry_model', 'add_model_preset',
            'ensure_model_channel', 'disable_token', 'cas_token_model',
            'read_token', 'enable_token')}
        self.addCleanup(lambda: [setattr(mo, k, v)
                                 for k, v in saved.items()])
        router = self.router
        mo.Router = lambda url: router
        mo.add_model_preset = self._preset
        mo.prepare_registry_model = self._prepare
        native = self.native
        mo.ensure_model_channel = native.ensure_model_channel
        mo.disable_token = native.disable_token
        mo.cas_token_model = native.cas_token_model
        mo.read_token = native.read_token
        mo.enable_token = native.enable_token
        self.orders = mo.ModelOrders(self.gw, self.tmp.name,
                                     'http://127.0.0.1:9', 99)
        self.gw.save('model_operations.json', {})

    def _preset(self, root, path, sha256, profile):
        self.preset_calls.append((path, sha256, dict(profile)))
        return 'punch-t1'

    def _prepare(self, selection, coverage, cache_dir):
        self.prepare_calls.append(dict(selection))
        if self.prepare_error is not None:
            raise self.prepare_error
        assert selection == SELECTION, selection
        return copy.deepcopy(self.prepared)

    def base_order(self, **over):
        order = {'buyer_user_id': 42, 'managed_username': 'ord-1',
                 'managed_password': 'x', 'state': 'READY', 'model': 'model-a',
                 'token_id': 7, 'offer_id': 'o', 'allowance_credits': 100,
                 'window_seconds': 3600, 'expires_at': time.time() + 3600,
                 'purchase_intent': 'pi', 'model_coverage': dict(COVERAGE),
                 'strict_text_policy': dict(POLICY)}
        order.update(over)
        return order

    def save_orders(self, mapping):
        self.gw.save(self.gw.ORDERS_FILE, mapping)

    def save_op(self, op_id, **over):
        op = {'operation_id': op_id, 'order_id': 'order-1',
              'selection': dict(SELECTION), 'previous_model': 'model-a',
              'state': 'QUEUED', 'updated_at': int(time.time())}
        op.update(over)
        self.gw.save('model_operations.json', {op_id: op})
        return op

    def load_op(self, op_id):
        return self.gw.load('model_operations.json')[op_id]

    def load_order(self, order_id='order-1'):
        return self.gw.load(self.gw.ORDERS_FILE)[order_id]

    def join_worker(self):
        worker = self.orders.worker
        if worker is not None:
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive())

    def test_startup_restores_retained_active_model(self):
        self.save_orders({'order-1': self.base_order(active_model='punch-retained')})
        self.orders.startup('model-a')
        self.assertEqual(self.router.calls, [('load', 'punch-retained')])

    def test_startup_leaves_pending_runtime_to_operation_recovery(self):
        self.save_orders({'order-1': self.base_order()})
        self.save_op('pending', state='UNAVAILABLE', runtime_touched=True)
        self.orders.startup('model-a')
        self.assertEqual(self.router.calls, [])
        self.assertTrue(self.orders.busy())

    def test_a_request_queues_without_router_contact(self):
        self.save_orders({'order-1': self.base_order()})
        body = dict(SELECTION, idempotency_key='op-1')
        view = self.orders.request('order-1', self.load_order(), body)
        op_id = operation_id('order-1', 'op-1')
        self.assertEqual(view['operation']['state'], 'QUEUED')
        self.assertEqual(self.load_op(op_id)['state'], 'QUEUED')
        self.assertEqual(self.load_order()['model_operation'], op_id)
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.prepare_calls, [])

    def test_b_crash_without_order_pointer_heals(self):
        self.save_orders({'order-1': self.base_order()})
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id)
        original_prepare = mo.prepare_registry_model
        def prepare_with_pointer(*args):
            self.assertEqual(self.load_order()['model_operation'], op_id)
            return original_prepare(*args)
        mo.prepare_registry_model = prepare_with_pointer
        self.orders.tick()
        self.join_worker()
        op = self.load_op(op_id)
        self.assertEqual(op['state'], 'READY')
        order = self.load_order()
        self.assertEqual(order['active_model'], 'punch-t1')
        self.assertEqual(order['model_operation'], op_id)
        self.assertIn(('cas', 7, 'model-a', 'punch-t1'), self.native.calls)
        self.assertIn(('enable', 7), self.native.calls)
        self.assertEqual(self.native.tokens[7]['model_limits'], 'punch-t1')

    def test_c_expired_order_before_drain_no_mutation(self):
        self.save_orders(
            {'order-1': self.base_order(expires_at=time.time() - 1)})
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id)
        self.orders.run(op_id)
        self.assertEqual(self.router.calls, [])
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.preset_calls, [])
        op = self.load_op(op_id)
        self.assertEqual(op['state'], 'FAILED')
        self.assertNotIn('active_model', self.load_order())

    def test_d1_touched_runtime_restores_directly_no_download(self):
        self.save_orders({'order-1': self.base_order()})
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id, state='LOADING', runtime_touched=True,
                     target_model='punch-t1')
        self.orders.run(op_id)
        self.assertEqual(self.prepare_calls, [])
        self.assertIn(('load', 'model-a'), self.router.calls)
        op = self.load_op(op_id)
        self.assertEqual(op['state'], 'FAILED')
        self.assertTrue(op.get('restored'), op)
        order = self.load_order()
        self.assertEqual(order.get('active_model', 'model-a'), 'model-a')
        self.assertEqual(self.native.tokens[7]['model_limits'], 'model-a')

    def test_d2_touched_runtime_restore_failure_stays_unavailable(self):
        self.save_orders({'order-1': self.base_order()})
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id, state='LOADING', runtime_touched=True,
                     target_model='punch-t1')
        self.router.fail_loads.add('model-a')
        self.orders.run(op_id)
        op = self.load_op(op_id)
        self.assertEqual(op['state'], 'UNAVAILABLE', op)
        self.assertFalse(op.get('restored'), op)
        self.assertEqual(op.get('recovery_attempts'), 1, op)
        self.assertIn('recovery_error', op)

    def test_e1_unavailable_keeps_busy(self):
        self.save_orders({'order-1': self.base_order()})
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id, state='UNAVAILABLE')
        self.assertTrue(self.orders.busy())
        with self.assertRaises(Failure) as caught:
            self.orders.request('order-1', self.load_order(),
                                dict(SELECTION, idempotency_key='op-2'))
        self.assertEqual(caught.exception.code, 'MODEL_OPERATION_BUSY')

    def test_e2_unavailable_tick_attempts_bounded_restoration(self):
        self.save_orders({'order-1': self.base_order()})
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id, state='UNAVAILABLE', runtime_touched=True)
        for _ in range(3):
            self.orders.tick()
            self.join_worker()
        loads = [c for c in self.router.calls if c == ('load', 'model-a')]
        self.assertGreaterEqual(len(loads), 1, self.router.calls)
        self.assertLessEqual(len(loads), 3, self.router.calls)
        self.assertLessEqual(self.router.max_inflight, 1)
        op = self.load_op(op_id)
        self.assertTrue(
            op['state'] == 'UNAVAILABLE'
            or (op['state'] == 'FAILED' and op.get('restored')), op)
        if op['state'] != 'UNAVAILABLE':
            self.assertEqual(
                self.load_order().get('active_model', 'model-a'), 'model-a')
            self.assertEqual(self.native.tokens[7]['model_limits'], 'model-a')

    def test_e3_restoration_attempts_cap_at_three(self):
        self.save_orders({'order-1': self.base_order()})
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id, state='UNAVAILABLE', runtime_touched=True,
                     recovery_attempts=2)
        self.router.fail_loads.add('model-a')
        self.orders.tick()
        self.join_worker()
        op = self.load_op(op_id)
        self.assertEqual(op['state'], 'UNAVAILABLE', op)
        self.assertEqual(op.get('recovery_attempts'), 3, op)
        self.orders.worker = None
        self.orders.tick()
        self.assertIsNone(self.orders.worker)
        self.assertEqual(self.load_op(op_id).get('recovery_attempts'), 3)

    def test_e4_same_key_request_permits_retry_restoration(self):
        self.save_orders({'order-1': self.base_order()})
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id, state='UNAVAILABLE', runtime_touched=True,
                     recovery_attempts=2)
        view = self.orders.request('order-1', self.load_order(),
                                   dict(SELECTION, idempotency_key='op-1'))
        self.assertEqual(view['operation']['state'], 'RESTORING')
        op = self.load_op(op_id)
        self.assertEqual(op['state'], 'RESTORING')
        self.assertEqual(op.get('recovery_attempts'), 0, op)

    def test_f_expired_other_order_is_drained_not_blocking(self):
        self.native.add(8)
        self.save_orders({
            'order-1': self.base_order(),
            'order-2': self.base_order(
                managed_username='ord-2', token_id=8,
                expires_at=time.time() - 1),
        })
        op_id = operation_id('order-1', 'op-1')
        self.save_op(op_id)
        self.orders.run(op_id)
        self.assertIn(('disable', 8), self.native.calls)
        op = self.load_op(op_id)
        self.assertEqual(op['state'], 'READY', op)
        self.assertEqual(self.load_order()['active_model'], 'punch-t1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
