#!/usr/bin/env python3
"""Automatic dynamic native runner heartbeat in the existing wrapper.

Drives the real work/punch-token-release-20260920/app/order-gateway.py as a
subprocess against two synthetic loopback boundaries: a minimal New API stub
(buyer identity only, mirroring tests/test_purchase_prepare.py) and a native
node stub implementing only POST /runners/heartbeat with scripted answers.
All tokens, secrets, payers and responses here are synthetic fixtures. No
chain, funds, signing, GPU, or remote calls. Honest boundary: timing-sensitive
recovery cases sleep past the documented 2s retry throttle; nothing here
proves instantaneous recovery.
"""
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
RUNNER = 'runner-1'
MASTER = 'synthetic-master-secret'


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
        return self.send_json(500, {'success': False, 'code': 'UNEXPECTED_CALL'})


class SyntheticNative(BaseHTTPRequestHandler):
    runners = {}
    fail = False
    calls = []
    counter = [0]

    def log_message(self, *args):
        pass

    def send_json(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        auth = self.headers.get('Authorization', '')
        SyntheticNative.calls.append((auth, body))
        if self.path != '/runners/heartbeat':
            return self.send_json(404, {})
        if SyntheticNative.fail:
            return self.send_json(500, {})
        rid = body.get('runner_id', '')
        if rid in SyntheticNative.runners and auth == SyntheticNative.runners[rid]:
            return self.send_json(200, {'runner_id': rid, 'heartbeat_interval': '30s',
                                        'heartbeat_ttl': '5m', 'session_ids': []})
        if rid not in SyntheticNative.runners and auth == MASTER:
            SyntheticNative.counter[0] += 1
            secret = 'synthetic-hb-%d' % SyntheticNative.counter[0]
            SyntheticNative.runners[rid] = secret
            return self.send_json(200, {'runner_id': rid, 'heartbeat_interval': '30s',
                                        'heartbeat_ttl': '5m', 'heartbeat_secret': secret,
                                        'session_ids': []})
        return self.send_json(401, {})


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def call(base, method, path, body=None, buyer_token=None):
    heads = {'Content-Type': 'application/json'}
    if buyer_token:
        heads['Authorization'] = 'Bearer ' + buyer_token
    req = urllib.request.Request(base + path, method=method,
        data=None if body is None else json.dumps(body).encode(), headers=heads)
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


def offer_body(**extra):
    body = {'seller_token': 'synthetic-seller', 'offer_id': 'offer-1',
            'model': 'punch-test-model', 'allowance_credits': 100,
            'window_seconds': 3600, 'input_rate': 1.0, 'output_rate': 5.0,
            'cache_rate': 1.0, 'native_runner_id': RUNNER,
            'native_recipient_address': RECIPIENT, 'price_wei': 2500}
    body.update(extra)
    return body


class RunnerRegistrationTest(unittest.TestCase):
    def start_gateway(self, extra_env=None, *, advertise_gateway=False):
        tmp = tempfile.mkdtemp(prefix='gw-reg-')
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        data = os.path.join(tmp, 'data')
        os.makedirs(data, mode=0o700)
        secrets = {}
        for name, value in (('seller', 'synthetic-seller'), ('admin_user', 'admin'),
                            ('admin_pass', 'admin-pass')):
            path = os.path.join(tmp, name)
            write_private(path, value.encode())
            secrets[name] = path
        upstream = HTTPServer(('127.0.0.1', 0), SyntheticUpstream)
        threading.Thread(target=upstream.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        gw_port = free_port()
        env = dict(os.environ, GW_ADMIN_BASE='http://127.0.0.1:%d' % upstream.server_port,
                   GW_DATA_DIR=data, GW_PORT=str(gw_port), GW_BIND='127.0.0.1',
                   GW_PUBLIC_API_BASE='http://127.0.0.1:%d' % gw_port,
                   GW_MODEL='punch-test-model',
                   GW_SELLER_TOKEN_FILE=secrets['seller'],
                   GW_ADMIN_USER_FILE=secrets['admin_user'],
                   GW_ADMIN_PASS_FILE=secrets['admin_pass'])
        if extra_env:
            env.update(extra_env)
        if advertise_gateway:
            env['GW_NATIVE_RUNNER_URL'] = 'http://127.0.0.1:%d' % gw_port
        proc = subprocess.Popen([sys.executable, APP], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=5)
        self.addCleanup(stop)
        base = 'http://127.0.0.1:%d' % gw_port
        end = time.monotonic() + 20
        while True:
            try:
                status, _ = call(base, 'GET', '/health')
                if status == 200:
                    return base, tmp
            except OSError:
                pass
            if time.monotonic() > end:
                self.fail('gateway did not start')
            time.sleep(0.1)

    def start_native(self):
        SyntheticNative.runners = {}
        SyntheticNative.fail = False
        SyntheticNative.calls = []
        SyntheticNative.counter = [0]
        stub = HTTPServer(('127.0.0.1', 0), SyntheticNative)
        threading.Thread(target=stub.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        return 'http://127.0.0.1:%d' % stub.server_port

    def registration_env(self, tmp, native_base):
        master = os.path.join(tmp, 'master-secret')
        write_private(master, b'synthetic-master-secret')
        reg = os.path.join(tmp, 'registration.json')
        write_private(reg, json.dumps({'master_secret_file': master}).encode())
        return {'GW_LIVEPEER_CHECKOUT': 'true',
                'GW_NATIVE_NODE_BASE': native_base,
                'GW_NATIVE_REGISTRATION_FILE': master,
                'GW_NATIVE_RUNNER_URL': 'http://127.0.0.1:9/wrapper'}

    def publish(self, base, body):
        return call(base, 'POST', '/offers', body)

    def prepare(self, base, idem='idem-1', payer=PAYER):
        return call(base, 'POST', '/purchases/prepare',
                    {'buyer_token': 'buyer1-token', 'idempotency_key': idem,
                     'offer_id': 'offer-1', 'payer_address': payer})

    def pump(self, base):
        call(base, 'GET', '/health')

    def test_unconfigured_publish_prepare_unchanged(self):
        base, _ = self.start_gateway()
        status, result = self.publish(base, offer_body())
        self.assertEqual(200, status)
        self.assertNotIn('native_registration', result.get('data', {}))
        status, result = self.prepare(base)
        self.assertEqual(200, status)
        self.assertEqual('PREPARED', result['data']['state'])

    def test_publish_registers_ready(self):
        tmp_native = tempfile.mkdtemp(prefix='gw-native-')
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp_native, ignore_errors=True))
        native_base = self.start_native()
        base, tmp = self.start_gateway(self.registration_env(tmp_native, native_base))
        status, result = self.publish(base, offer_body())
        self.assertEqual(200, status)
        reg = result['data'].get('native_registration', {})
        self.assertEqual('ready', reg.get('state'))
        self.assertEqual(1, len(SyntheticNative.calls))
        auth, body = SyntheticNative.calls[0]
        self.assertEqual(MASTER, auth)
        self.assertEqual(RUNNER, body['runner_id'])
        self.assertEqual('http://127.0.0.1:9/wrapper', body['runner_url'])
        self.assertEqual('punch/compute-v1', body['app'])
        self.assertEqual('single-shot', body['mode'])
        self.assertEqual(1, body['capacity'])
        self.assertEqual({'price': 2500, 'currency': 'wei', 'unit': 'fixed'}, body['price_info'])
        self.assertEqual('ready', body['status'])
        meta = json.loads(body['metadata'])
        self.assertEqual('offer-1', meta['offer_id'])
        self.assertTrue(meta['offer_digest'])
        self.assertLessEqual(len(body['metadata'].encode()), 1024)

    def test_identical_republish_recovers(self):
        native_base = self.start_native()
        base, tmp = self.start_gateway_with_tmp(native_base)
        status, _ = self.publish(base, offer_body())
        self.assertEqual(200, status)
        master_calls = sum(1 for auth, _ in SyntheticNative.calls if auth == MASTER)
        status, result = self.publish(base, offer_body())
        self.assertEqual(200, status)
        self.assertEqual('ready', result['data']['native_registration']['state'])
        self.assertEqual(master_calls,
                         sum(1 for auth, _ in SyntheticNative.calls if auth == MASTER))
        # Second heartbeat reused the stored per-runner secret, not the master.
        self.assertEqual(2, len(SyntheticNative.calls))
        self.assertNotEqual(MASTER, SyntheticNative.calls[1][0])

    def test_conflicting_runner_terms_rejected(self):
        native_base = self.start_native()
        base, tmp = self.start_gateway_with_tmp(native_base)
        status, _ = self.publish(base, offer_body())
        self.assertEqual(200, status)
        status, result = self.publish(base, offer_body(offer_id='offer-2', price_wei=2600))
        self.assertEqual(409, status)
        self.assertEqual('NATIVE_RUNNER_TERMS_CONFLICT', result.get('code'))

    def test_master_fallback_after_native_restart(self):
        native_base = self.start_native()
        base, tmp = self.start_gateway_with_tmp(native_base)
        status, _ = self.publish(base, offer_body())
        self.assertEqual(200, status)
        SyntheticNative.runners = {}
        time.sleep(2.5)
        self.pump(base)
        end = time.monotonic() + 10
        while True:
            status, result = self.publish(base, offer_body())
            self.assertEqual(200, status)
            if result['data']['native_registration'].get('state') == 'ready':
                break
            if time.monotonic() > end:
                self.fail('registration did not recover after native restart')
            time.sleep(0.5)
        master_calls = sum(1 for auth, _ in SyntheticNative.calls if auth == MASTER)
        self.assertGreaterEqual(master_calls, 2)

    def test_prepare_gated_until_ready(self):
        native_base = self.start_native()
        SyntheticNative.fail = True
        base, tmp = self.start_gateway_with_tmp(native_base)
        status, result = self.publish(base, offer_body())
        self.assertEqual(200, status)
        self.assertEqual('pending', result['data']['native_registration']['state'])
        status, result = self.prepare(base)
        self.assertEqual(503, status)
        self.assertEqual('NATIVE_REGISTRATION_PENDING', result.get('code'))
        SyntheticNative.fail = False
        time.sleep(2.5)
        self.pump(base)
        end = time.monotonic() + 10
        while True:
            status, result = self.prepare(base)
            if status == 200:
                break
            self.assertEqual(503, status)
            if time.monotonic() > end:
                self.fail('prepare stayed gated after native recovery')
            time.sleep(0.5)
        self.assertEqual('PREPARED', result['data']['state'])

    def test_withdrawn_runner_retained(self):
        native_base = self.start_native()
        base, tmp = self.start_gateway_with_tmp(native_base)
        status, _ = self.publish(base, offer_body())
        self.assertEqual(200, status)
        status, _ = call(base, 'POST', '/offers/withdraw',
                         {'seller_token': 'synthetic-seller', 'offer_id': 'offer-1'})
        self.assertEqual(200, status)
        with open(os.path.join(tmp, 'native_runners.json'), encoding='utf-8') as f:
            records = json.load(f)
        record = records.get(RUNNER)
        self.assertIsNotNone(record)
        # Presence and readiness only; secret values are never asserted.
        self.assertTrue(bool(record.get('heartbeat_secret')))
        self.assertTrue(record.get('ready'))

    # Small helper to keep the registration tests readable. Returns the
    # gateway base URL and the gateway DATA directory (which holds the
    # private runner records under test).
    def start_gateway_with_tmp(self, native_base):
        tmp = tempfile.mkdtemp(prefix='gw-reg-')
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, ignore_errors=True))
        base, gw_tmp = self.start_gateway(self.registration_env(tmp, native_base))
        return base, os.path.join(gw_tmp, 'data')



@unittest.skipUnless(os.environ.get('PUNCH_NATIVE_RUNNER_BINARY'),
                     'NOT_TESTED: explicit rebuilt native binary required')
class NativeRunnerRegistrationTest(unittest.TestCase):
    """Real offchain node and real gateway; run inside --network none.

    Only registration, HTTP health proxying and retained republish are used.
    No purchase, model execution, payer, chain endpoint or signing is invoked.
    """
    start_gateway = RunnerRegistrationTest.start_gateway
    registration_env = RunnerRegistrationTest.registration_env

    def test_real_node_registers_and_proxies_gateway_health(self):
        with tempfile.TemporaryDirectory(prefix='punch-native-runner-') as root:
            native_port, cli_port = free_port(), free_port()
            native = 'http://127.0.0.1:%d' % native_port
            with open(os.environ.get('PUNCH_NATIVE_LOG', os.path.join(root, 'native.log')), 'wb') as log:
                proc = subprocess.Popen([
                    os.environ['PUNCH_NATIVE_RUNNER_BINARY'],
                    '-network', 'offchain', '-orchestrator', '-useLiveRunners',
                    '-orchSecret', MASTER, '-serviceAddr', native,
                    '-httpAddr', native, '-liveRunnerAddr', native,
                    '-cliAddr', '127.0.0.1:%d' % cli_port,
                    '-datadir', os.path.join(root, 'node'),
                    '-monitor=false', '-startupAvailabilityCheck=false',
                    '-pricePerUnit', '1', '-pixelsPerUnit', '1'],
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 30
                    while True:
                        self.assertIsNone(proc.poll(), 'Native process exited; inspect owned fixture log')
                        try:
                            code, _ = call('http://127.0.0.1:%d' % cli_port, 'GET', '/status')
                            if code == 200:
                                with socket.create_connection(('127.0.0.1', native_port), timeout=1):
                                    break
                        except OSError:
                            pass
                        self.assertLess(time.monotonic(), deadline, 'Native readiness timeout')
                        time.sleep(.1)
                    base, gateway_root = self.start_gateway(
                        self.registration_env(root, native), advertise_gateway=True)
                    code, result = call(base, 'POST', '/offers', offer_body())
                    self.assertEqual(code, 200)
                    self.assertEqual(result['data']['native_registration']['state'], 'ready')
                    state_path = os.path.join(gateway_root, 'data', 'native_runners.json')
                    with open(state_path, 'rb') as state:
                        retained = json.load(state)[RUNNER]
                    direct_code, direct = call(base, 'GET', '/health')
                    self.assertEqual(direct_code, 200)
                    self.assertTrue(direct['success'])
                    self.assertTrue(direct['data']['ready'])
                    code, proxied = call(native, 'GET', '/apps/' + RUNNER + '/app/health')
                    self.assertEqual(code, 200)
                    self.assertEqual(proxied['success'], direct['success'])
                    self.assertEqual(proxied['data'], direct['data'])
                    code, repeat = call(base, 'POST', '/offers', offer_body())
                    self.assertEqual(code, 200)
                    self.assertEqual(repeat['data']['native_registration']['state'], 'ready')
                    with open(state_path, 'rb') as state:
                        repeated = json.load(state)[RUNNER]
                    for key in ('heartbeat_secret', 'runner_url', 'recipient',
                                'price_wei', 'offer_id', 'model'):
                        self.assertEqual(repeated[key], retained[key])
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
