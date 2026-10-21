#!/usr/bin/env python3
"""Immutable optional model_coverage terms in seller offers.

Drives the real work/punch-token-release-20260920/app/order-gateway.py as a
subprocess against two synthetic loopback boundaries: a minimal New API stub
(buyer identity plus the strict-policy capability flag) and a native node
stub capturing POST /runners/heartbeat bodies so heartbeat metadata digests
are observable. All tokens, secrets, payers and responses are synthetic
fixtures. No chain, funds, signing, GPU, payments, or remote calls.

Causal rule: the digest-difference assertion is the same-input old-fail /
new-pass check — pre-change code drops model_coverage from the digest, so two
offers differing only in coverage produced identical digests there.
"""
import ast
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   '..', 'app', 'order-gateway.py')
BUYERS = {'buyer1-token': {'id': 42, 'username': 'buyer1'}}
PAYER = '0x' + 'aa' * 20
RECIPIENT = '0x' + 'cc' * 20
MASTER = 'synthetic-master-secret'
POLICY = {'input_rate': 1.0, 'output_rate': 5.0, 'cache_rate': 1.0,
          'context_tokens': 1024, 'max_output_tokens': 128,
          'reasoning_effort': 'low'}
COV1 = {'max_total_parameters': 8000000000, 'max_weight_bytes': 5000000000,
        'architectures': ['llama'], 'file_types': ['MOSTLY_Q4_K_M', 'MOSTLY_Q6_K', 'ALL_F32'],
        'min_context_tokens': 512, 'text_only': True}
COV2 = dict(COV1, max_weight_bytes=6000000000,
            file_types=['MOSTLY_Q4_K_M', 'MOSTLY_Q6_K', 'ALL_F32', 'MOSTLY_Q8_0'])


class SyntheticUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api/user/self':
            user = BUYERS.get(self.headers.get('Authorization', '').removeprefix('Bearer '))
            if user:
                return self.send_json(200, {'success': True, 'data': dict(user)})
            return self.send_json(401, {'success': False, 'code': 'BAD_TOKEN'})
        if self.path == '/api/status':
            return self.send_json(200, {'success': True,
                                        'data': {'punch_strict_text_policy': 1}})
        return self.send_json(500, {'success': False, 'code': 'UNEXPECTED_CALL'})


class SyntheticNative(BaseHTTPRequestHandler):
    bodies = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        SyntheticNative.bodies.append(body)
        payload = json.dumps({'runner_id': body.get('runner_id'),
                              'heartbeat_interval': '30s',
                              'heartbeat_ttl': '5m'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def call(base, method, path, body=None, headers=None):
    heads = {'Content-Type': 'application/json'}
    heads.update(headers or {})
    req = urllib.request.Request(base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers=heads)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}
        finally:
            e.close()


def digests_for(runner_id):
    out = []
    for body in SyntheticNative.bodies:
        if body.get('runner_id') == runner_id:
            out.append(json.loads(body['metadata'])['offer_digest'])
    return out


class OfferModelCoverageCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = HTTPServer(('127.0.0.1', 0), SyntheticUpstream)
        cls.upstream_base = f'http://127.0.0.1:{cls.upstream.server_address[1]}'
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.node = HTTPServer(('127.0.0.1', 0), SyntheticNative)
        cls.node_base = f'http://127.0.0.1:{cls.node.server_address[1]}'
        threading.Thread(target=cls.node.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        data = os.path.join(cls.tmp.name, 'data')
        os.makedirs(data, mode=0o700)

        def secret(name, value):
            path = os.path.join(cls.tmp.name, name)
            with open(path, 'w') as f:
                f.write(value)
            os.chmod(path, 0o600)
            return path

        cls.seller_file = secret('seller.token', 'seller-secret')
        cls.seller_token = 'seller-secret'
        cls.master_file = secret('master-secret', MASTER)
        for name in ('offers.json', 'orders.json', 'sessions.json', 'native_runners.json'):
            path = os.path.join(data, name)
            with open(path, 'w') as f:
                json.dump({}, f)
            os.chmod(path, 0o600)
        cls.port = free_port()
        cls.base = f'http://127.0.0.1:{cls.port}'
        env = dict(os.environ, GW_ADMIN_BASE=cls.upstream_base, GW_DATA_DIR=data,
                   GW_PORT=str(cls.port), GW_BIND='127.0.0.1',
                   GW_PUBLIC_API_BASE=cls.base,
                   GW_SELLER_TOKEN_FILE=cls.seller_file,
                   GW_ADMIN_USER_FILE=secret('gw-user', 'admin'),
                   GW_ADMIN_PASS_FILE=secret('gw-pass', 'pass'),
                   GW_LIVEPEER_CHECKOUT='true',
                   GW_NATIVE_NODE_BASE=cls.node_base,
                   GW_NATIVE_REGISTRATION_FILE=cls.master_file,
                   GW_NATIVE_RUNNER_URL=cls.base)
        cls.proc = subprocess.Popen([sys.executable, os.path.abspath(APP)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                status, _ = call(cls.base, 'GET', '/health')
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            cls.proc.terminate()
            raise RuntimeError('wrapper did not become ready')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.upstream.shutdown()
        cls.node.shutdown()
        cls.tmp.cleanup()

    def offer_body(self, offer_id, runner, coverage=COV1, policy=POLICY, **over):
        body = {'seller_token': self.seller_token, 'offer_id': offer_id,
                'model': 'punch-test-model', 'input_rate': 1.0, 'output_rate': 5.0,
                'cache_rate': 1.0, 'allowance_credits': 100, 'window_seconds': 3600,
                'strict_text_policy': dict(policy),
                'model_coverage': dict(coverage) if coverage is not None else None,
                'native_runner_id': runner,
                'native_recipient_address': RECIPIENT, 'price_wei': 2500}
        if coverage is None:
            del body['model_coverage']
        body.update(over)
        return body

    def publish(self, offer_id, runner, coverage=COV1, **over):
        return call(self.base, 'POST', '/offers',
                    self.offer_body(offer_id, runner, coverage, **over))

    def prepare(self, offer_id, key):
        return call(self.base, 'POST', '/purchases/prepare',
                    {'buyer_token': 'buyer1-token', 'offer_id': offer_id,
                     'idempotency_key': key, 'payer_address': PAYER})

    def test_publish_validates_and_pins(self):
        status, env = self.publish('cov1', 'cov-runner-1')
        self.assertEqual(status, 200, env)
        status, env = call(self.base, 'GET', '/offers/cov1')
        self.assertEqual(status, 200, env)
        self.assertEqual(env['data']['model_coverage'], COV1)
        self.assertEqual(env['data']['strict_text_policy'], POLICY)
        self.assertTrue(digests_for('cov-runner-1'))
        # Identical republish recovers registration state: the runner record
        # carries coverage, so its digest still matches the offer digest.
        status, env = self.publish('cov1', 'cov-runner-1')
        self.assertEqual(status, 200, env)

    def test_altered_coverage_changes_digest(self):
        # Execute the actual pure digest functions with IDENTICAL offer/runner
        # IDs and prices; varying IDs would mask a missing coverage binding.
        source = os.environ.get('PUNCH_COVERAGE_SOURCE', APP)
        with open(source) as f:
            tree = ast.parse(f.read())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in ('native_offer_digest', 'purchase_intent')]
        scope = {'json': json, 'hashlib': hashlib}
        exec(compile(ast.Module(body=functions, type_ignores=[]), source, 'exec'), scope)
        first = self.offer_body('same-offer', 'same-runner', COV1)
        second = self.offer_body('same-offer', 'same-runner', COV2)
        digest = scope['native_offer_digest']
        self.assertNotEqual(digest('same-offer', 'same-runner', RECIPIENT, 2500, first),
                            digest('same-offer', 'same-runner', RECIPIENT, 2500, second))
        self.assertNotEqual(scope['purchase_intent'](first), scope['purchase_intent'](second))

    def test_coverage_requires_policy_and_shape(self):
        body = self.offer_body('cov-bad', 'cov-runner-bad', coverage=COV1)
        del body['strict_text_policy']
        status, env = call(self.base, 'POST', '/offers', body)
        self.assertEqual(status, 400)
        self.assertEqual(env['code'], 'MODEL_COVERAGE_REQUIRES_POLICY')
        bad_coverages = [
            dict(COV1, text_only=False),
            {k: COV1[k] for k in COV1 if k != 'architectures'},
            dict(COV1, max_total_parameters='8000000000'),
            dict(COV1, max_weight_bytes=True),
            dict(COV1, min_context_tokens=0),
            dict(COV1, architectures=[]),
            dict(COV1, file_types=['Q4_K!']),
            dict(COV1, extra_key=1),
        ]
        for index, bad in enumerate(bad_coverages):
            status, env = self.publish(f'cov-bad-{index}', f'cov-runner-bad-{index}',
                                       coverage=bad)
            self.assertEqual(status, 400, (index, env))
            self.assertEqual(env['code'], 'INVALID_MODEL_COVERAGE', (index, env))

    def test_old_order_keeps_coverage_after_withdrawal_and_reoffer(self):
        status, env = self.publish('covK', 'cov-runner-K', coverage=COV1)
        self.assertEqual(status, 200, env)
        status, first = self.prepare('covK', 'k-cov-keep')
        self.assertEqual(status, 200, first)
        intent = first['data']['purchase_intent']
        self.assertEqual(first['data']['model_coverage'], COV1)
        status, env = call(self.base, 'POST', '/offers/withdraw',
                           {'seller_token': self.seller_token, 'offer_id': 'covK'})
        self.assertEqual(status, 200, env)
        status, env = self.publish('covKb', 'cov-runner-Kb', coverage=COV2)
        self.assertEqual(status, 200, env)
        status, readback = call(self.base, 'GET', '/purchases/' + first['data']['order_id'],
                                headers={'Authorization': 'Bearer buyer1-token'})
        self.assertEqual(status, 200, readback)
        self.assertEqual(readback['data']['model_coverage'], COV1)
        self.assertEqual(readback['data']['purchase_intent'], intent)
        self.assertEqual(readback['data']['state'], 'PREPARED')
        status, env = self.prepare('covK', 'k-cov-after-wd')
        self.assertEqual(status, 410)
        self.assertEqual(env['code'], 'OFFER_UNAVAILABLE')

    def test_retry_unchanged_keeps_identity(self):
        status, env = self.publish('covR', 'cov-runner-R', coverage=COV2)
        self.assertEqual(status, 200, env)
        _, first = self.prepare('covR', 'k-cov-retry')
        status, env = self.prepare('covR', 'k-cov-retry')
        self.assertEqual(status, 200, env)
        self.assertTrue(env['data']['duplicate'])
        self.assertEqual(env['data']['order_id'], first['data']['order_id'])
        self.assertEqual(env['data']['purchase_intent'], first['data']['purchase_intent'])
        self.assertEqual(env['data']['model_coverage'], COV2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
