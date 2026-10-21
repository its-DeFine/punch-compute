"""Immutable router catalog tests; executable acceptance is recorded separately."""
import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('model_runtime', ROOT / 'app/model_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class ModelCatalogTest(unittest.TestCase):
    def test_append_preserves_previous_preset_and_retry_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / 'a.gguf', root / 'b.gguf'
            first.write_bytes(b'fixture-a')
            second.write_bytes(b'fixture-b')
            profile = dict(context_tokens=4096, max_output_tokens=16, parallel=1, gpu_layers=0, reasoning_effort='')
            old = runtime.add_model_preset(root, first, 'a'*64, profile)
            before = (root / 'models.ini').read_bytes()
            self.assertEqual(old, runtime.add_model_preset(root, first, 'a'*64, profile))
            self.assertEqual(before, (root / 'models.ini').read_bytes())
            new = runtime.add_model_preset(root, second, 'b'*64, profile)
            self.assertNotEqual(old, new)
            self.assertTrue((root / 'models.ini').read_bytes().startswith(before))
            self.assertTrue(first.exists())
            with self.assertRaises(runtime.ModelRuntimeError):
                runtime.add_model_preset(root, second, 'a'*64, profile)

    def test_same_artifact_at_second_cache_path_reuses_immutable_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / 'old-cache.gguf', root / 'new-cache.gguf'
            first.write_bytes(b'same verified artifact')
            second.write_bytes(first.read_bytes())
            sha = hashlib.sha256(first.read_bytes()).hexdigest()
            profile = dict(context_tokens=512, max_output_tokens=16, parallel=1, gpu_layers=0, reasoning_effort='low')
            model = runtime.add_model_preset(root, first, sha, profile)
            before = (root / 'models.ini').read_bytes()
            self.assertEqual(model, runtime.add_model_preset(root, second, sha, profile))
            self.assertEqual(before, (root / 'models.ini').read_bytes())
            first.write_bytes(b'changed after preparation')
            with self.assertRaises(runtime.ModelRuntimeError) as caught:
                runtime.add_model_preset(root, second, sha, profile)
            self.assertEqual(caught.exception.code, 'MODEL_ARTIFACT_CHANGED')
            self.assertEqual(before, (root / 'models.ini').read_bytes())

    def test_path_and_profile_cannot_inject_router_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / 'a.gguf'
            first.write_bytes(b'fixture')
            profile = dict(context_tokens=4096, max_output_tokens=16, parallel=1, gpu_layers=0, reasoning_effort='')
            with self.assertRaises(runtime.ModelRuntimeError):
                runtime.add_model_preset(root, first, 'a'*64, dict(profile, reasoning_effort='low\napi-key = injected'))
            with self.assertRaises((ValueError, runtime.ModelRuntimeError)):
                runtime.add_model_preset(root / 'other', first, 'a'*64, profile)


if __name__ == '__main__':
    unittest.main()
