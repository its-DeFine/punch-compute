#!/usr/bin/env python3
"""Shipped CLI against the actual wrapper HTTP process and private file state.
Synthetic orders only; no native service, funds, or non-loopback requests.
Native drain/balance preservation is exercised by test_newapi_models_native.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(ROOT, '..', 'bin', 'punch-token-orders')
APP = os.path.join(ROOT, '..', 'app', 'order-gateway.py')
SECRET = 'seller-secret-fixture'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def write_secret(path, value):
    with open(path, 'w') as f:
        f.write(value)
    os.chmod(path, 0o600)


class MaintenanceActualHandlerCase(unittest.TestCase):
    """Same CLI against the actual wrapper handler with synthetic private
    file state (root-owned maintenance routes). Proves the thin mapping end
    to end on loopback: owner fixed server-side, no secret leaks."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = os.path.join(cls.tmp.name, 'data')
        os.makedirs(cls.data, mode=0o700)
        for name in ('offers.json', 'orders.json'):
            path = os.path.join(cls.data, name)
            with open(path, 'w') as f:
                json.dump({}, f)
            os.chmod(path, 0o600)
        cls.seller_file = os.path.join(cls.tmp.name, 'seller.token')
        write_secret(cls.seller_file, SECRET)
        cls.admin_user = os.path.join(cls.tmp.name, 'gw-user')
        write_secret(cls.admin_user, 'admin')
        cls.admin_pass = os.path.join(cls.tmp.name, 'gw-pass')
        write_secret(cls.admin_pass, 'pass')
        cls.port = free_port()
        cls.base = f'http://127.0.0.1:{cls.port}'
        cls.proc = cls.start_wrapper(cls.port)

    @classmethod
    def start_wrapper(cls, port):
        base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, GW_ADMIN_BASE='http://127.0.0.1:1', GW_DATA_DIR=cls.data, GW_PORT=str(port),
                   GW_BIND='127.0.0.1',
                   GW_PUBLIC_API_BASE=base,
                   GW_SELLER_TOKEN_FILE=cls.seller_file,
                   GW_ADMIN_USER_FILE=cls.admin_user,
                   GW_ADMIN_PASS_FILE=cls.admin_pass)
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(APP)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + '/health', timeout=5) as r:
                    if r.status == 200:
                        return proc
            except OSError:
                time.sleep(0.1)
        proc.terminate()
        proc.wait(timeout=10)
        raise RuntimeError('wrapper did not become ready')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=30)
        cls.tmp.cleanup()

    def cli(self, *argv, base=None):
        env = dict(os.environ, PUNCH_ORDER_GATEWAY=base or self.base)
        proc = subprocess.run([sys.executable, os.path.abspath(CLI), *argv],
                              env=env, capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(proc.stdout)
        except ValueError:
            out = {'raw': proc.stdout}
        return proc.returncode, out, proc.stdout

    def write_orders(self, mapping):
        path = os.path.join(self.data, 'orders.json')
        with open(path, 'w') as f:
            json.dump(mapping, f)

    def test_actual_bad_seller_is_rejected(self):
        wrong=os.path.join(self.tmp.name,'wrong.token')
        write_secret(wrong,'wrong-fixture')
        code,out,stdout=self.cli('maintenance-status','--seller-token-file',wrong)
        self.assertNotEqual(code,0)
        self.assertEqual(out['code'],'SELLER_AUTH_INVALID')
        self.assertNotIn('wrong-fixture',stdout)

    def test_actual_handler_round_trip(self):
        code, out, stdout = self.cli(
            'maintenance-status', '--seller-token-file', self.seller_file)
        self.assertEqual(code, 0, out)
        self.assertTrue(out['ok'], out)
        self.assertIn(out['data']['admission'], ('open', 'closed'))
        self.assertNotIn(SECRET, stdout)
        code, out, stdout = self.cli(
            'maintenance-acquire', '--seller-token-file', self.seller_file,
            '--operation-id', 'op-live-1')
        self.assertEqual(code, 0, out)
        operation = out['data']['operation']
        self.assertEqual(operation['state'], 'HELD', out)
        self.assertEqual(out['data']['admission'], 'closed', out)
        self.assertEqual(operation['owner'], 'seller', out)
        self.assertNotIn(SECRET, stdout)
        code, out, stdout = self.cli(
            'maintenance-release', '--seller-token-file', self.seller_file,
            '--operation-id', 'op-live-1')
        self.assertEqual(code, 0, out)
        self.assertEqual(out['data']['operation']['state'], 'RELEASED', out)
        self.assertEqual(out['data']['admission'], 'open', out)
        self.assertNotIn(SECRET, stdout)

    def test_actual_refusal_blocks_acquire(self):
        self.write_orders({'o-live': {'state': 'PREPARED', 'model': 'm', 'expires_at': 9999999999}})
        try:
            code, out, stdout = self.cli(
                'maintenance-acquire', '--seller-token-file', self.seller_file,
                '--operation-id', 'op-live-2')
            self.assertNotEqual(code, 0, out)
            self.assertTrue(out['ok'], out)
            self.assertEqual(out['data']['admission'], 'closed', out)
            self.assertEqual(out['data']['operation']['state'], 'REFUSED', out)
            self.assertIn({'code': 'UNSAFE_ORDER_STATE', 'order_id': 'o-live', 'state': 'PREPARED'},
                          out['data']['blockers'], out)
            self.assertNotIn(SECRET, stdout)
            code, out, _ = self.cli(
                'maintenance-status', '--seller-token-file', self.seller_file)
            self.assertEqual(code, 0, out)
            self.assertEqual(out['data']['admission'], 'closed', out)
        finally:
            self.write_orders({})
            self.cli('maintenance-release', '--seller-token-file',
                     self.seller_file, '--operation-id', 'op-live-2')

    def test_actual_restart_preserves_closed_gate(self):
        code, out, _ = self.cli(
            'maintenance-acquire', '--seller-token-file', self.seller_file,
            '--operation-id', 'op-live-3')
        self.assertEqual(code, 0, out)
        self.assertEqual(out['data']['operation']['state'], 'HELD', out)
        self.proc.terminate()
        self.proc.wait(timeout=30)
        new_port = free_port()
        new_base = f'http://127.0.0.1:{new_port}'
        type(self).proc = self.start_wrapper(new_port)
        try:
            code, out, stdout = self.cli(
                'maintenance-status', '--seller-token-file', self.seller_file,
                base=new_base)
            self.assertEqual(code, 0, out)
            self.assertEqual(out['data']['admission'], 'closed', out)
            self.assertEqual(out['data']['operation']['id'], 'op-live-3', out)
            self.assertNotIn(SECRET, stdout)
            code, out, _ = self.cli(
                'maintenance-release', '--seller-token-file', self.seller_file,
                '--operation-id', 'op-live-3', base=new_base)
            self.assertEqual(code, 0, out)
            self.assertEqual(out['data']['admission'], 'open', out)
        finally:
            self.proc.terminate()
            self.proc.wait(timeout=30)
            type(self).proc = self.start_wrapper(self.port)


if __name__ == '__main__':
    unittest.main(verbosity=2)
