"""Provider packaging and actual HTTP startup without retail configuration."""
import glob
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

class ServiceSeparation(unittest.TestCase):

    def test_provider_package_and_start_without_storefront(self):
        dockerfile = (ROOT / 'packaging/Dockerfile.order-api').read_text()
        copies = []
        for line in dockerfile.splitlines():
            if line.startswith('COPY '):
                for pattern in shlex.split(line)[2:-1]:
                    copies += list((ROOT / 'app').glob(pattern))
        self.assertTrue(copies)
        self.assertFalse(any(p.name.startswith(('storefront_', 'stripe_', 'commerce_')) for p in copies),
                         'Provider image includes operator retail modules')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for p in copies: shutil.copyfile(p, root / p.name)
            env = {'PATH': os.environ['PATH'], 'GW_DATA_DIR': str(root/'data'),
                   'GW_BIND': '127.0.0.1', 'PYTHONDONTWRITEBYTECODE': '1'}
            for name, var in [('user','GW_ADMIN_USER_FILE'), ('pass','GW_ADMIN_PASS_FILE'),
                              ('seller','GW_SELLER_TOKEN_FILE')]:
                p=root/name; p.write_text('synthetic-'+name); p.chmod(0o600); env[var]=str(p)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port=sock.getsockname()[1]
            env['GW_PORT']=str(port)
            # -I -S excludes user paths and all installed retail dependencies.
            code='import sys,runpy; sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[1]+"/order-gateway.py",run_name="__main__")'
            proc=subprocess.Popen([sys.executable,'-I','-S','-c',code,tmp],env=env,
                                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=.2) as response:
                            result=json.load(response)
                        break
                    except OSError:
                        if proc.poll() is not None: self.fail('Provider exited')
                        time.sleep(.05)
                else: self.fail('Provider did not start')
                self.assertTrue(result['success'])
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/offers',timeout=1) as response:
                    self.assertEqual(json.load(response)['data'], [])
            finally:
                proc.terminate(); proc.wait(timeout=5)

