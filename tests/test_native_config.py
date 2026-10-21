#!/usr/bin/env python3
"""Synthetic local unit tests for opt-in native node configuration.

Pure file/state logic; no subprocess, network, keys, funds, or builds. The
fixed Compose/node runtime path is proven separately by root.
"""
import importlib.util
import json
import os
import stat
import subprocess
import uuid
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'punch_setup_native_config', Path(__file__).resolve().parents[1] / 'setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

ADDR = '0x' + '22' * 20
OTHER = '0x' + '33' * 20
FULL = {'recipient': OTHER, 'network': 'arbitrum-one-mainnet',
        'rpc': 'https://rpc.example:8545', 'service_url': 'https://node.example:8935'}


class NativeConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='punch-native-config-')
        self.root = Path(self.tmp.name)
        self.app = self.root / 'app'
        (self.app / 'data').mkdir(parents=True)
        self.state = {'native_account': {'address': ADDR}}

    def tearDown(self):
        self.tmp.cleanup()

    def input_file(self, data):
        path = self.root / 'native-input.json'
        path.write_text(json.dumps(data))
        os.chmod(path, 0o600)
        return str(path)

    def call(self, config='__unset__', state=None):
        if config == '__unset__':
            config = self.input_file(dict(FULL, account=ADDR))
        if state is None:
            state = self.state
        return setup.ensure_native_config(str(self.app), str(self.root), state, config)

    def secret(self):
        path = self.app / 'native.registration'
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        return path.read_text().strip()

    def node_conf(self):
        return (self.root / 'native' / 'node.conf').read_text()

    def test_pin_full_config(self):
        result = self.call()
        self.assertEqual(result['account'], ADDR)
        self.assertEqual(result['recipient'], OTHER)
        self.assertNotIn('rpc', result)
        secret = self.secret()
        self.assertTrue(len(secret) >= 32)
        conf = self.node_conf()
        flags = dict(line.split(' ', 1) for line in conf.splitlines())
        self.assertEqual(flags['ethOrchAddr'], OTHER)
        self.assertEqual(flags['orchSecret'], secret)
        self.assertEqual(flags['ethAcctAddr'], ADDR)
        self.assertEqual(flags['orchestrator'], 'true')
        self.assertEqual(flags['useLiveRunners'], 'true')
        self.assertEqual(flags['httpAddr'], 'http://0.0.0.0:8935')
        self.assertEqual(flags['cliAddr'], '127.0.0.1:7935')
        saved = setup.load_state(str(self.app))
        self.assertEqual(saved['native_config'], result)
        rerun = self.call()
        self.assertEqual(rerun, result)
        self.assertEqual(self.secret(), secret)

    def test_fixed_ticket_uses_funded_deposit_and_retains_identity(self):
        self.call()
        secret = self.secret()
        saved = setup.load_state(str(self.app))
        self.assertIn('fullFundsCoverage true\n', self.node_conf())
        previous = self.node_conf().replace('fullFundsCoverage true\n', '')
        (self.root / 'native/node.conf').write_text(previous)
        self.call(None)
        self.assertIn('fullFundsCoverage true\n', self.node_conf())
        self.assertEqual(self.secret(), secret)
        self.assertEqual(setup.load_state(str(self.app)), saved)

    def test_retained_implicit_tls_config_upgrades_without_identity_reset(self):
        self.call()
        saved = setup.load_state(str(self.app))
        secret = self.secret()
        previous = self.node_conf().replace('httpAddr http://0.0.0.0:8935',
                                            'httpAddr 0.0.0.0:8935')
        (self.root / 'native/node.conf').write_text(previous)
        self.call(None)
        self.assertIn('httpAddr http://0.0.0.0:8935\n', self.node_conf())
        self.assertEqual(self.secret(), secret)
        self.assertEqual(setup.load_state(str(self.app)), saved)

    def test_required_base_price_and_retained_generated_upgrade(self):
        self.call()
        flags = dict(line.split(' ', 1) for line in self.node_conf().splitlines())
        self.assertEqual(flags.get('pricePerUnit'), '1')
        self.assertEqual(flags.get('pixelsPerUnit'), '1')
        secret = self.secret()
        saved = setup.load_state(str(self.app))
        legacy = '\n'.join(line for line in self.node_conf().splitlines()
                           if not line.startswith(('pricePerUnit ', 'pixelsPerUnit '))) + '\n'
        legacy = legacy.replace('httpAddr http://0.0.0.0:8935', 'httpAddr 0.0.0.0:8935')
        (self.root / 'native/node.conf').write_text(legacy)
        self.call(None)
        self.assertIn('pricePerUnit 1\n', self.node_conf())
        self.assertEqual(self.secret(), secret)
        self.assertEqual(setup.load_state(str(self.app)), saved)
        # Only the exact previous generator output is repairable.
        (self.root / 'native/node.conf').write_text(legacy + 'pricePerUnit 99\n')
        with self.assertRaises(setup.Failure) as raised:
            self.call(None)
        self.assertEqual(raised.exception.code, 'NATIVE_CONFIG_MISMATCH')

    def test_retained_conflict_refuses_without_reset(self):
        self.call()
        before_conf, before_secret = self.node_conf(), self.secret()
        changed = dict(FULL, account=ADDR, recipient=ADDR, network='other-net')
        with self.assertRaises(setup.Failure) as raised:
            self.call(self.input_file(changed))
        self.assertEqual(raised.exception.code, 'NATIVE_CONFIG_MISMATCH')
        self.assertEqual(self.node_conf(), before_conf)
        self.assertEqual(self.secret(), before_secret)
        self.assertEqual(setup.load_state(str(self.app))['native_config']['recipient'], OTHER)

    def test_missing_or_disagreeing_account_refuses(self):
        with self.assertRaises(setup.Failure) as raised:
            setup.ensure_native_config(str(self.app), str(self.root), {},
                                       self.input_file(dict(FULL, account=ADDR)))
        self.assertEqual(raised.exception.code, 'NATIVE_ACCOUNT_REQUIRED')
        with self.assertRaises(setup.Failure) as raised:
            self.call(self.input_file(dict(FULL, account=OTHER)))
        self.assertEqual(raised.exception.code, 'NATIVE_ACCOUNT_MISMATCH')
        self.assertFalse((self.root / 'native' / 'node.conf').exists())

    def test_invalid_shapes_write_nothing(self):
        bad_inputs = [
            dict(FULL, recipient='not-an-address'),
            dict(FULL, rpc='https://user:pass@rpc.example'),
            dict(FULL, service_url='ftp://node.example'),
            {k: FULL[k] for k in ('recipient', 'rpc', 'service_url')},
            'just a string',
        ]
        for data in bad_inputs:
            with self.assertRaises(setup.Failure, msg=repr(data)):
                self.call(self.input_file(data))
        with self.assertRaises(setup.Failure) as raised:
            setup.ensure_native_config(str(self.app), str(self.root), self.state,
                                       str(self.root / 'absent.json'))
        self.assertEqual(raised.exception.code, 'NATIVE_CONFIG_UNREADABLE')
        self.assertFalse((self.root / 'native' / 'node.conf').exists())

    def test_retained_reuse_heals_and_validates(self):
        self.call()
        secret, conf = self.secret(), self.node_conf()
        # No input file: retained binding reused without secret re-entry.
        self.assertEqual(self.call(None)['account'], ADDR)
        (self.root / 'native' / 'node.conf').unlink()
        self.assertEqual(self.call(None)['account'], ADDR)
        self.assertEqual(self.node_conf(), conf)
        self.assertEqual(self.secret(), secret)
        # Drifted retained file refuses instead of resetting.
        with open(self.root / 'native' / 'node.conf', 'w') as f:
            f.write(conf.replace('ethOrchAddr ' + OTHER, 'ethOrchAddr ' + ADDR))
        with self.assertRaises(setup.Failure) as raised:
            self.call(None)
        self.assertEqual(raised.exception.code, 'NATIVE_CONFIG_MISMATCH')
        # Missing master secret refuses instead of replacing.
        with open(self.root / 'native' / 'node.conf', 'w') as f:
            f.write(conf)
        (self.app / 'native.registration').unlink()
        with self.assertRaises(setup.Failure) as raised:
            self.call(None)
        self.assertEqual(raised.exception.code, 'NATIVE_STATE_INCOMPLETE')

    def test_rpc_credentials_never_leave_private_native_config(self):
        rpc = 'https://rpc.example/v1/synthetic-api-key'
        result = self.call(self.input_file(dict(FULL, rpc=rpc)))
        self.assertNotIn(rpc, json.dumps(result))
        self.assertNotIn(rpc, json.dumps(setup.load_state(str(self.app))))
        self.assertIn(rpc, self.node_conf())

    def test_config_injection_refused_before_writes(self):
        for key, value in [('network', 'arbitrum-one-mainnet\norchestrator false'),
                           ('rpc', 'https://rpc.example/\nethOrchAddr ' + ADDR),
                           ('service_url', 'https://node.example/#hidden')]:
            with self.assertRaises(setup.Failure):
                self.call(self.input_file(dict(FULL, **{key: value})))
        self.assertFalse((self.root / 'native' / 'node.conf').exists())

    def test_no_secrets_in_result(self):
        result = self.call()
        secret = self.secret()
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(sorted(result), ['account', 'network', 'recipient', 'rpc_digest', 'service_url'])


@unittest.skipUnless(os.environ.get('PUNCH_NATIVE_CONFIG_IMAGE'), 'NOT_TESTED: explicit native image required')
class NativeConfigParserCase(unittest.TestCase):
    setUp = NativeConfigCase.setUp
    tearDown = NativeConfigCase.tearDown
    input_file = NativeConfigCase.input_file
    call = NativeConfigCase.call
    secret = NativeConfigCase.secret
    def test_actual_binary_parses_generated_config_without_network(self):
        synthetic_rpc = 'https://rpc.example/v1/synthetic-api-key'
        self.call(self.input_file(dict(FULL, rpc=synthetic_rpc)))
        name = 'punch-native-config-' + uuid.uuid4().hex[:12]
        command = ['docker', 'run', '--rm', '--name', name, '--network', 'none',
                   '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '-v', str(self.root / 'native') + ':/config:ro',
                   os.environ['PUNCH_NATIVE_CONFIG_IMAGE'], '-config', '/config/node.conf', '-version']
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, 'native config parser failed')
            self.assertNotIn(synthetic_rpc, output)
            self.assertNotIn(self.secret(), output)
            self.assertIn('Livepeer Node Version:', output)
            self.assertRegex(output, r'Orchestrator\s*\|\s*true')
            self.assertRegex(output, r'UseLiveRunners\s*\|\s*true')
            # PrintConfig omits fields whose DefaultLivepeerConfig value is nil;
            # pricePerUnit is checked by the generated/retained-config regression.
            self.assertIn('127.0.0.1:7935', output)
            self.assertIn('http://0.0.0.0:8935', output)
        finally:
            ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'], text=True).strip()
            if ids:
                subprocess.run(['docker', 'rm', '-f', name], check=True, capture_output=True)
            self.assertEqual(subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'], text=True).strip(), '')


if __name__ == '__main__':
    unittest.main()
