"""Bootstrap checks: local state/profile tests use synthetic function seams;
the opt-in NativeSetupTest uses the shipped native binary and actual HTTP API.
Run on Docker host: PUNCH_SETUP_NATIVE_BINARY=/absolute/new-api-strict1 python3 -m unittest discover -s tests -p test_setup.py -v
Only an owned, unfunded CPU container is used. No model, ticket or inference.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

spec = importlib.util.spec_from_file_location('punch_setup', Path(__file__).resolve().parents[1] / 'setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
IMAGE = 'calciumion/new-api@sha256:0a4d62b1b2b796a43a5e0ef92d49f12e0f229ab206b8f5a3a4cd42990121bfe1'
DIGEST = '2d18b9dc9b36bb1662b795dadae1ec9d4c41830fdfd860106a444bb76aa6bdb6'


class LocalStateTest(unittest.TestCase):
    def test_corrupt_journal_is_not_empty_state(self):
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / 'app'
            (app / 'data').mkdir(parents=True)
            setup.write_private(str(app / 'data' / setup.STATE_FILE), '{broken')
            with self.assertRaises(setup.Failure) as raised:
                setup.load_state(str(app))
            self.assertEqual(raised.exception.code, 'SETUP_STATE_UNREADABLE')


PROFILE_MODEL = 'punch-' + 'ab' * 32


def profile_manifest(model=PROFILE_MODEL, **over):
    manifest = {'model': model,
                'identity': {'registry': 'huggingface', 'repository': 'owner/repo',
                             'revision': 'a' * 40, 'file': 'm.gguf',
                             'size': 100, 'sha256': 'cd' * 32},
                'profile': {'context_tokens': 64, 'max_output_tokens': 8,
                            'parallel': 1, 'gpu_layers': 99,
                            'reasoning_effort': 'low'},
                'offer': {'input_rate': 1.0, 'output_rate': 5.0,
                          'cache_rate': 1.0, 'allowance_credits': 100,
                          'window_seconds': 3600}}
    manifest.update(over)
    return manifest


class FakeProfileTransport(setup.Transport):
    """Minimal native surface for profile installs. Any unlisted route (in
    particular /api/option/) is a failure: profile mode must not write
    global rates."""

    def __init__(self):
        self.paths = []
        self.channels = {}
        self.next_id = 1

    def call(self, base, method, path, body=None, headers=None):
        self.paths.append((method, path))
        if (method, path) == ('GET', '/api/setup'):
            return 200, {'success': True, 'data': {'status': False}}
        if (method, path) == ('POST', '/api/setup'):
            return 200, {'success': True, 'data': {}}
        if (method, path) == ('POST', '/api/user/login'):
            return 200, {'success': True, 'data': {
                'access_token': 'fake-admin-token', 'user': {'id': 1}}}
        if (method, path) == ('GET', '/api/user/self'):
            return 200, {'success': True, 'data': {'id': 1}}
        if (method, path) == ('GET', '/api/channel/'):
            items = list(self.channels.values())
            return 200, {'success': True, 'data': {'items': items, 'total': len(items)}}
        if (method, path) == ('POST', '/api/channel/'):
            record = dict(body['channel'], id=self.next_id)
            self.next_id += 1
            self.channels[record['name']] = record
            return 200, {'success': True, 'data': {'id': record['id']}}
        raise AssertionError('unexpected native call %r %r' % (method, path))


class ModelProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = FakeProfileTransport()

    def write_manifest(self, name='profile.json', manifest=None, mode=0o644):
        path = self.root / name
        path.write_text(json.dumps(manifest if manifest is not None else profile_manifest()))
        os.chmod(path, mode)
        return str(path)

    def state_snapshot(self):
        app = self.root / 'app'
        state_file = app / 'data' / setup.STATE_FILE
        return (state_file.read_bytes() if state_file.exists() else None,
                dict(self.transport.channels))

    def test_profile_fresh_install_and_repeat_idempotent(self):
        manifest_path = self.write_manifest()
        first = setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                                  transport=self.transport, model_profile=manifest_path)
        self.assertTrue(first['ok'])
        self.assertEqual(first['model'], PROFILE_MODEL)
        self.assertEqual(first['ratios_changed'], [])
        self.assertNotIn(('GET', '/api/option/'), self.transport.paths)
        self.assertNotIn(('PUT', '/api/option/'), self.transport.paths)
        channel = self.transport.channels[setup.CHANNEL_DEFAULT]
        self.assertEqual(channel['models'], PROFILE_MODEL)
        self.assertEqual(channel['base_url'], setup.BACKEND_DEFAULT)
        app = self.root / 'app'
        state = json.loads((app / 'data' / setup.STATE_FILE).read_text())
        self.assertEqual(state['binding']['model'], PROFILE_MODEL)
        self.assertEqual(state['model_profile']['model'], PROFILE_MODEL)
        self.assertEqual(state['model_profile']['identity']['sha256'], 'cd' * 32)
        self.assertEqual((app / 'data' / setup.STATE_FILE).stat().st_mode & 0o777, 0o600)
        before = self.state_snapshot()
        repeat = setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                                   transport=self.transport, model_profile=manifest_path)
        self.assertEqual(repeat['channel_id'], first['channel_id'])
        self.assertEqual(repeat['model'], PROFILE_MODEL)
        self.assertEqual(self.state_snapshot(), before)

    def test_profile_conflicting_binding_rejected_without_mutation(self):
        manifest_path = self.write_manifest()
        setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                          transport=self.transport, model_profile=manifest_path)
        before = self.state_snapshot()
        other = self.write_manifest('other.json',
                                    profile_manifest(model='punch-' + 'ef' * 32))
        with self.assertRaises(setup.Failure) as raised:
            setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                              transport=self.transport, model_profile=other)
        self.assertEqual(raised.exception.code, 'SETUP_BINDING_MISMATCH')
        self.assertEqual(self.state_snapshot(), before)
        same_model = self.write_manifest(
            'same-model.json',
            profile_manifest(identity=dict(profile_manifest()['identity'], sha256='ee' * 32)))
        with self.assertRaises(setup.Failure) as raised:
            setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                              transport=self.transport, model_profile=same_model)
        self.assertEqual(raised.exception.code, 'MODEL_PROFILE_MISMATCH')
        self.assertEqual(self.state_snapshot(), before)

    def test_profile_invalid_manifest_rejected_before_state(self):
        cases = {'bad-model': profile_manifest(model='punch-test-model'),
                 'no-identity': {k: v for k, v in profile_manifest().items() if k != 'identity'},
                 'bad-sha': profile_manifest(identity={'sha256': 'zzz'}),
                 'no-profile': {k: v for k, v in profile_manifest().items() if k != 'profile'},
                 'not-dict': ['punch-' + 'ab' * 32]}
        for name, manifest in cases.items():
            with self.subTest(name=name):
                path = self.write_manifest(name + '.json', manifest)
                with self.assertRaises(setup.Failure) as raised:
                    setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                                      transport=self.transport, model_profile=path)
                self.assertIn(raised.exception.code,
                              ('MODEL_PROFILE_INVALID', 'MODEL_PROFILE_UNREADABLE'))
                self.assertFalse((self.root / 'app').exists())
        with self.assertRaises(setup.Failure) as raised:
            setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                              transport=self.transport,
                              model_profile=str(self.root / 'missing.json'))
        self.assertEqual(raised.exception.code, 'MODEL_PROFILE_UNREADABLE')

    def test_legacy_paths_preserved(self):
        with self.assertRaises(setup.Failure) as raised:
            setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                              transport=self.transport, model='custom-model')
        self.assertEqual(raised.exception.code, 'PROFILE_NOT_SUPPORTED')
        with self.assertRaises(setup.Failure) as raised:
            setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                              transport=self.transport, backend='http://other:8080')
        self.assertEqual(raised.exception.code, 'PROFILE_NOT_SUPPORTED')
        manifest_path = self.write_manifest()
        with self.assertRaises(setup.Failure) as raised:
            setup.init_bundle(str(self.root), 'http://127.0.0.1:9999',
                              transport=self.transport, model='other-explicit',
                              model_profile=manifest_path)
        self.assertEqual(raised.exception.code, 'MODEL_PROFILE_MISMATCH')


@unittest.skipUnless(os.environ.get('PUNCH_SETUP_NATIVE_BINARY') and shutil.which('docker'),
                     'NOT_TESTED: requires exact native binary and Docker')
class NativeSetupTest(unittest.TestCase):
    def test_interrupted_setup_repeat_and_retained_identity(self):
        binary = Path(os.environ['PUNCH_SETUP_NATIVE_BINARY']).resolve(strict=True)
        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), DIGEST)
        with tempfile.TemporaryDirectory(prefix='punch-native-setup-') as d:
            root = Path(d)
            data = root / 'newapi-data'
            data.mkdir()
            name = 'punch-setup-test-' + uuid.uuid4().hex[:12]
            container = None
            try:
                launch = subprocess.run(['docker','run','-d','--name',name,
                    '--label','punch.fixture=native-setup-test','--user',f'{os.getuid()}:{os.getgid()}',
                    '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                    '--cpus=1','--memory=512m','--pids-limit=128',
                    '--tmpfs',f'/tmp:rw,noexec,nosuid,size=64m,uid={os.getuid()},gid={os.getgid()}',
                    '-p','127.0.0.1::3000','-w','/data',
                    '-e','SQLITE_PATH=/data/new-api.db','-e','BATCH_UPDATE_ENABLED=false',
                    '-v',str(data)+':/data','-v',str(binary)+':/new-api:ro',IMAGE],
                    capture_output=True,text=True,check=True)
                container = launch.stdout.strip()
                port = subprocess.run(['docker','port',container,'3000/tcp'],
                    capture_output=True,text=True,check=True).stdout.strip()
                self.assertTrue(port.startswith('127.0.0.1:'))
                base = 'http://' + port
                transport = setup.Transport()
                deadline = time.monotonic() + 30
                while True:
                    try:
                        code, body = transport.call(base,'GET','/api/setup')
                        if code == 200: break
                    except OSError: pass
                    if time.monotonic() >= deadline: self.fail('Native bootstrap readiness timeout')
                    time.sleep(.2)
                self.assertIs(body['data']['status'], False)

                class Interrupted(setup.Transport):
                    def call(self, base, method, path, body=None, headers=None):
                        if (method,path) == ('POST','/api/setup'):
                            raise RuntimeError('injected interruption before native dispatch')
                        return super().call(base,method,path,body,headers)

                with self.assertRaisesRegex(RuntimeError,'injected interruption'):
                    setup.init_bundle(str(root),base,transport=Interrupted())
                app = root / 'app'
                identity = {n:(app/n).read_bytes() for n in setup.CRED_FILES}
                first = setup.init_bundle(str(root),base)
                repeat = setup.init_bundle(str(root),base)
                self.assertEqual(first['state'],'READY')
                self.assertEqual(first['channel_id'],repeat['channel_id'])
                self.assertEqual(repeat['ratios_changed'],[])
                self.assertEqual(identity,{n:(app/n).read_bytes() for n in setup.CRED_FILES})
                for n in (*setup.CRED_FILES,'data/setup-state.json','data/setup.lock'):
                    self.assertEqual((app/n).stat().st_mode & 0o777,0o600)

                # Partial publication recovers the SAME credential from durable state.
                (app/'seller.token').unlink()
                setup.init_bundle(str(root),base)
                self.assertEqual((app/'seller.token').read_bytes(),identity['seller.token'])
                with self.assertRaises(setup.Failure) as raised:
                    setup.init_bundle(str(root/'missing'),base)
                self.assertEqual(raised.exception.code,'RETAINED_DB_NO_CREDS')
                self.assertFalse((root/'missing/app/admin_pass').exists())

                import sqlite3
                with sqlite3.connect('file:'+str(data/'new-api.db')+'?mode=ro',uri=True) as db:
                    self.assertEqual(db.execute('select count(*) from users').fetchone()[0],1)
                    self.assertEqual(db.execute('select count(*) from channels').fetchone()[0],1)
                    self.assertEqual(db.execute('select count(*) from tokens').fetchone()[0],0)
                # Corrupt retained state refuses before credentials or native state change.
                setup.write_private(str(app/'data'/setup.STATE_FILE),'{broken')
                with self.assertRaises(setup.Failure) as raised:
                    setup.init_bundle(str(root),base)
                self.assertEqual(raised.exception.code,'SETUP_STATE_UNREADABLE')
                self.assertEqual(identity,{n:(app/n).read_bytes() for n in setup.CRED_FILES})
            finally:
                if container:
                    subprocess.run(['docker','rm','-f',container],capture_output=True,check=True)
                    check = subprocess.run(['docker','inspect',container],capture_output=True)
                    self.assertNotEqual(check.returncode,0,'Owned fixture cleanup unverified')


if __name__ == '__main__':
    unittest.main()
