"""Read-only Compose acceptance. Requires Docker Compose; creates no services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid


@unittest.skipUnless(shutil.which('docker'), 'NOT_TESTED: Docker Compose unavailable')
class PackagingTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('PUNCH_STOPPED_COMPOSE_FIXTURE'),
                         'NOT_TESTED: owned stopped Compose fixture required')
    def test_status_shows_retained_stopped_services(self):
        # Read-only actual Compose projection; no fabricated response schema.
        root = Path(os.environ['PUNCH_STOPPED_COMPOSE_FIXTURE']).resolve()
        env = dict(os.environ)
        if (root/'models/default.json').exists():
            env['COMPOSE_FILE'] = 'compose.yaml:compose.models.yaml'
            env['MODEL_GGUF_PATH'] = str(root/'models/default.gguf')
        before = subprocess.check_output(['docker', 'compose', 'ps', '--all',
            '--format', 'json'], cwd=root, env=env, text=True)
        expected = {r['Service']: r['State'] for r in
                    (json.loads(line) for line in before.splitlines() if line.strip())}
        self.assertTrue(expected, 'Fixture has no retained containers')
        self.assertTrue(all(state == 'exited' for state in expected.values()),
                        'Fixture must already be fully stopped; this test never stops it')
        result = subprocess.run(['sh', str(root/'punch-stack'), 'status',
                                 '--format', 'json'], cwd=root, env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = {r['Service']: r['State'] for r in
                  (json.loads(line) for line in result.stdout.splitlines() if line.strip())}
        self.assertEqual(actual, expected)

    def test_retained_mounts_and_lifecycle_commands(self):
        source = Path(__file__).resolve().parents[1] / 'packaging'
        with tempfile.TemporaryDirectory(prefix='punch-compose-check-') as directory:
            root = Path(directory)
            for name in ('compose.yaml', 'punch-stack'):
                shutil.copy2(source / name, root / name)
            env = dict(os.environ, MODEL_GGUF_PATH='/nonexistent/read-only-fixture.gguf',
                       COMPOSE_PROJECT_NAME='punch-config-check-' + uuid.uuid4().hex[:12])
            # Rendering validates the real Compose schema, without creating bind paths.
            command = ['docker', 'compose', '-f', str(root / 'compose.yaml')]
            result = subprocess.run(command + ['config', '--format', 'json'],
                                    env=env, capture_output=True, text=True, check=True)
            services = json.loads(result.stdout)['services']
            mounts = {name: {v['target']: v['source'] for v in services[name]['volumes']}
                      for name in ('new-api', 'order-api')}
            self.assertEqual(mounts['new-api']['/data'], str(root / 'newapi-data'))
            self.assertEqual(mounts['order-api']['/data'], str(root / 'app/data'))
            self.assertNotEqual(mounts['new-api']['/data'], mounts['order-api']['/data'])
            self.assertNotIn('/app',mounts['order-api'])
            self.assertNotIn('/new-api',mounts['new-api'])
            self.assertEqual(mounts['order-api']['/run/punch'],str(root/'app'))
            for service in services.values():
                self.assertNotIn('container_name', service)
                self.assertRegex(service['image'], r'^(?:.+@)?sha256:[a-f0-9]{64}$')
                for port in service.get('ports', []):
                    self.assertEqual(port['host_ip'], '127.0.0.1')
            for args in (['logs'], ['logs', 'order-api'], ['status']):
                # Unique, unstarted project: actual CLI validation, no private logs.
                result = subprocess.run(['sh', str(root / 'punch-stack'), *args],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get('PUNCH_RELEASE_IMAGES_READY'), 'NOT_TESTED: release images unavailable')
    def test_preflight_uses_compose_dotenv_without_starting(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='punch-preflight-') as directory:
            root=Path(directory)
            for name in ('compose.yaml','punch-stack'):
                shutil.copy2(source/'packaging'/name,root/name)
            shutil.copy2(source/'setup.py',root/'setup.py')
            (root/'app').mkdir()
            model=root/'model fixture.gguf'
            model.write_bytes(b'preflight only; never loaded')
            (root/'.env').write_text('MODEL_GGUF_PATH='+str(model)+'\nGATEWAY_PORT=18385\n')
            env={k:v for k,v in os.environ.items() if k not in ('MODEL_GGUF_PATH','GATEWAY_PORT')}
            env['COMPOSE_PROJECT_NAME']='punch-preflight-'+uuid.uuid4().hex[:12]
            p=subprocess.run(['sh',str(root/'punch-stack'),'preflight'],env=env,text=True,capture_output=True)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
            self.assertIn('gateway=127.0.0.1:18385',p.stdout)
            self.assertFalse((root/'newapi-data').exists())
            self.assertFalse((root/'app/data').exists())
            p=subprocess.run(['docker','compose','-f',str(root/'compose.yaml'),'ps','-aq'],env=env,text=True,capture_output=True,check=True)
            self.assertEqual(p.stdout.strip(),'')

    @unittest.skipUnless(os.environ.get('PUNCH_RELEASE_IMAGES_READY'), 'NOT_TESTED: release images unavailable')
    def test_native_retained_account_selects_profile_without_starting(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='punch-native-preflight-') as directory:
            root = Path(directory)
            for name in ('compose.yaml', 'punch-stack'):
                shutil.copy2(source / 'packaging' / name, root / name)
            shutil.copy2(source / 'setup.py', root / 'setup.py')
            (root / 'app/data').mkdir(parents=True)
            model = root / 'model.gguf'
            model.write_bytes(b'preflight only')
            env = dict(os.environ, MODEL_GGUF_PATH=str(model),
                       COMPOSE_PROJECT_NAME='punch-native-preflight-' + uuid.uuid4().hex[:12])
            (root / 'app/data/setup-state.json').write_text(json.dumps({
                'native_account': {'address': '0x' + 'ab' * 20}}))
            # Missing native directory must not silently select the free profile.
            # Compose's native image is unavailable in this isolated config;
            # selecting it must fail before any service is started.
            compose = (root / 'compose.yaml').read_text()
            start = compose.index('  livepeer:')
            end = compose.index('  model:')
            import re
            native = re.sub(r'sha256:[a-f0-9]{64}', 'sha256:' + '0' * 64, compose[start:end])
            (root / 'compose.yaml').write_text(compose[:start] + native + compose[end:])
            result = subprocess.run(['sh', str(root / 'punch-stack'), 'preflight'],
                                    env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('release images unavailable', result.stderr)
            self.assertFalse((root / 'native').exists())
            self.assertEqual(subprocess.check_output(['docker', 'compose', '-f',
                str(root / 'compose.yaml'), 'ps', '-aq'], env=env, text=True).strip(), '')

    @unittest.skipUnless(os.environ.get('PUNCH_COLD_IMAGE_ARCHIVE'), 'NOT_TESTED: explicit owned cold-image fixture required')
    def test_verified_archive_load_precedes_model_start(self):
        # Explicit opt-in: only the three unused task-built images may be removed.
        source=Path(__file__).resolve().parents[1]
        archive=Path(os.environ['PUNCH_COLD_IMAGE_ARCHIVE']).resolve()
        with tempfile.TemporaryDirectory(prefix='punch-image-load-') as directory:
            root=Path(directory)
            for n in ('compose.yaml','punch-stack'):
                shutil.copy2(source/'packaging'/n,root/n)
            shutil.copy2(source/'setup.py',root/'setup.py')
            (root/'images.tar.gz').symlink_to(archive)
            shutil.copy2(archive.with_name('images.sha256'),root/'images.sha256')
            env=dict(os.environ,MODEL_GGUF_PATH='/nonexistent/punch-cold-load.gguf',
                     COMPOSE_PROFILES='livepeer',COMPOSE_PROJECT_NAME='punch-cold-'+uuid.uuid4().hex[:12])
            (root/'app/data').mkdir(parents=True)
            (root/'app/data/setup-state.json').write_text(json.dumps({
                'native_account': {'address': '0x' + 'ab' * 20}}))
            config=json.loads(subprocess.check_output(['docker','compose','-f',str(root/'compose.yaml'),'config','--format','json'],env=env))
            images=[]
            for name in ('new-api','order-api','livepeer'):
                image=config['services'][name]['image'];tag='punch-token-'+name+':20261021-rebuilt'
                self.assertEqual(subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',tag],text=True).strip(),image)
                self.assertEqual(subprocess.check_output(['docker','ps','-aq','--filter','ancestor='+image],text=True).strip(),'','Image has a consumer; cold test must not remove it')
                images.append((image,tag))
            for image,tag in images:
                subprocess.run(['docker','image','rm',tag],capture_output=True,check=True)
                self.assertNotEqual(subprocess.run(['docker','image','inspect',image],capture_output=True).returncode,0)
            try:
                p=subprocess.run(['sh',str(root/'punch-stack'),'up'],env=env,text=True,capture_output=True)
                # Missing model intentionally ends before startup, AFTER successful cold load.
                self.assertEqual(p.returncode,1)
                self.assertIn('model weights unavailable',p.stderr)
                self.assertNotIn('AttributeError',p.stderr)
                for image,_ in images:
                    self.assertEqual(subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image],text=True).strip(),image)
                self.assertEqual(subprocess.check_output(['docker','compose','-f',str(root/'compose.yaml'),'ps','-aq'],env=env,text=True).strip(),'')
            finally:
                # Preserve availability even when assertion fails; archive is owned/public bytes.
                subprocess.run(['docker','load','-i',str(archive)],capture_output=True,check=True)
                for image,tag in images:
                    subprocess.run(['docker','tag',image,tag],capture_output=True,check=True)


if __name__ == '__main__':
    unittest.main()

class RetainedOfferAddressCase(unittest.TestCase):
    def test_actual_retained_offer_comparison(self):
        # Exact mismatch observed from the shipped CLI and GET /offers/<id>.
        script=(Path(__file__).resolve().parents[1]/'packaging/punch-stack').read_text()
        condition=next(line[5:-1] for line in script.splitlines()
                       if line.startswith('elif any(existing.get(k)'))
        offer={'native_recipient_address':'0xabCD1234abCD1234abCD1234abCD1234abCD1234',
               'price_wei':10000000000000}
        existing={'native_recipient_address':'0xabcd1234abcd1234abcd1234abcd1234abcd1234',
                  'price_wei':10000000000000,'withdrawn':True}
        def differs():
            return eval(condition, {'offer':offer,'existing':existing})
        self.assertFalse(differs())
        existing['native_recipient_address']='0x'+'11'*20
        self.assertTrue(differs())
        existing['native_recipient_address']=offer['native_recipient_address'].lower()
        existing['price_wei']+=1
        self.assertTrue(differs())
