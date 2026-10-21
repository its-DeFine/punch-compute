"""Public diagnostic projection must not expose hostile metadata or fake readiness."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('diagnostics',Path(__file__).resolve().parents[1]/'packaging/diagnostics.py')
d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)


class DiagnosticsTest(unittest.TestCase):
    def test_allowlist_drops_credentials_and_extra_fields(self):
        for value in ('https://user:secret@registry/path', 'image?token=secret', 'secret', None):
            self.assertIsNone(d.image_id(value))
        image='sha256:'+'a'*64
        self.assertEqual(d.image_id(image),image)
        self.assertEqual(d.safe_probe({'state':'READY','secret':'secret','loaded_models':['punch-'+'a'*64]}),
                         {'state':'READY','loaded_models':['punch-'+'a'*64]})
        self.assertEqual(d.safe_probe({'state':'READY','loaded_models':['secret']}),{'state':'UNAVAILABLE'})
        self.assertEqual(d.safe_probe({'state':'secret','error':'secret'}),{'state':'UNAVAILABLE'})

    def test_compose_failure_has_no_raw_error_or_healthy_claim(self):
        with patch.object(d,'command',side_effect=OSError('secret diagnostic error')):
            report=d.build_report(Path('/nonexistent'))
        self.assertEqual(report['state'],'UNAVAILABLE')
        self.assertEqual(report['code'],'COMPOSE_UNAVAILABLE')
        self.assertNotIn('secret',str(report))
        self.assertEqual(report['payment_readiness']['state'],'NOT_TESTED')


if __name__=='__main__':unittest.main()
