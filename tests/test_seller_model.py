#!/usr/bin/env python3
"""Tests for app/seller_model.py. Run with the model venv interpreter:

    outputs/punch-token-orders-concept-20260920/model-venv/bin/python \
        work/punch-token-release-20260920/tests/test_seller_model.py

Real GGUFWriter-created artifacts parsed by the real GGUFReader; registry and
download boundaries use synthetic local function fixtures (stub api object
plus stubbed hf_hub_download returning a file already inside the cache, so no
network occurs). No ledger, credentials, or external commands.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

import gguf
import model_registry
import seller_model
from seller_model import cmd_prepare, cmd_ready, main
from unittest import mock

SHA = 'ab' * 20
ARCH = 'llama'
CONTEXT = 512


def namespace(**kwargs):
    holder = type('Stub', (), {})()
    for key, value in kwargs.items():
        setattr(holder, key, value)
    return holder


class StubApi:
    def __init__(self, digest, size):
        self.digest = digest
        self.size = size

    def model_info(self, repo, revision=None):
        return namespace(sha=SHA, private=False, gated=False)

    def list_repo_files(self, repo, revision=None):
        return ['tiny.gguf']

    def get_paths_info(self, repo, paths=None, revision=None):
        return [namespace(size=self.size,
                          lfs=namespace(sha256=self.digest))]


class StubRouter:
    def __init__(self, loaded):
        self.loaded = loaded
        self.seen = []

    def load(self, model_id, timeout=120):
        self.seen.append(model_id)
        return dict(self.loaded)


class SellerModelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='punch-seller-model-')
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, 'models')
        self.cache = os.path.join(self.root, 'cache')
        os.makedirs(self.cache)
        self.artifact = os.path.join(self.cache, 'tiny.gguf')
        writer = gguf.GGUFWriter(self.artifact, ARCH)
        writer.add_context_length(CONTEXT)
        writer.add_embedding_length(32)
        writer.add_block_count(1)
        writer.add_tokenizer_model('llama')
        writer.add_token_list([b'<unk>', b'a', b'b'])
        writer.add_file_type(gguf.LlamaFileType.ALL_F32)
        writer.add_chat_template("{{ messages[0]['content'] }}")
        writer.add_tensor('blk.0.attn_q.weight', np.zeros((8, 8), dtype=np.float32))
        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_tensors_to_file()
        writer.close()
        with open(self.artifact, 'rb') as f:
            self.digest = hashlib.sha256(f.read()).hexdigest()
        self.size = os.path.getsize(self.artifact)
        self.api = StubApi(self.digest, self.size)
        self.real_download = model_registry.hf_hub_download
        model_registry.hf_hub_download = lambda **kwargs: self.artifact
        self.addCleanup(setattr, model_registry, 'hf_hub_download', self.real_download)

    def offer(self, **over):
        base = {'model': 'punch-test-model', 'allowance_credits': 100,
                'window_seconds': 3600, 'input_rate': 1.0, 'output_rate': 5.0,
                'cache_rate': 1.0,
                'default_model': {'registry': 'huggingface', 'repository': 'owner/tiny',
                                  'revision': 'main', 'file': 'tiny.gguf'},
                'model_coverage': {'max_total_parameters': 1000,
                                   'max_weight_bytes': self.size,
                                   'architectures': ['llama'],
                                   'file_types': ['ALL_F32'],
                                   'min_context_tokens': 128, 'text_only': True},
                'strict_text_policy': {'input_rate': 1.0, 'output_rate': 5.0,
                                       'cache_rate': 1.0, 'context_tokens': CONTEXT,
                                       'max_output_tokens': 128,
                                       'reasoning_effort': 'low'}}
        base.update(over)
        path = os.path.join(self.tmp.name, 'offer.json')
        with open(path, 'w') as f:
            json.dump(base, f)
        return path

    def default_path(self):
        return os.path.join(self.root, 'default.json')

    def test_prepare_pins_default(self):
        result = cmd_prepare(self.offer(), self.root, 99, api=self.api)
        self.assertTrue(result['model'].startswith('punch-'))
        self.assertEqual(result['offer']['model'], result['model'])
        self.assertEqual(result['offer']['default_model']['revision'], SHA)
        self.assertEqual(result['stats']['total_parameters'], 64)
        self.assertEqual(result['stats']['architecture'], ARCH)
        self.assertEqual(result['profile']['gpu_layers'], 99)
        self.assertEqual(result['profile']['parallel'], 1)
        self.assertEqual(result['identity']['sha256'], self.digest)
        with open(self.default_path()) as f:
            saved = json.load(f)
        self.assertEqual(saved['model'], result['model'])
        self.assertEqual(oct(os.stat(self.default_path()).st_mode & 0o777), '0o600')
        self.assertFalse(os.path.exists(os.path.join(self.root, self.digest + '.gguf')))
        from model_runtime import add_model_preset
        self.assertEqual(add_model_preset(self.root, self.artifact, self.digest, result['profile']), result['model'])
        with open(os.path.join(self.root, 'models.ini')) as f:
            catalog = f.read()
        self.assertIn('[' + result['model'] + ']', catalog)

    def test_retained_same_input_reuses(self):
        first = cmd_prepare(self.offer(), self.root, 99, api=self.api)
        second = cmd_prepare(self.offer(), self.root, 99, api=self.api)
        self.assertEqual(second['model'], first['model'])
        self.assertEqual(second['offer'], first['offer'])

    def test_conflicting_input_refuses(self):
        cmd_prepare(self.offer(), self.root, 99, api=self.api)
        with open(self.default_path(), 'rb') as f:
            before = f.read()
        offer_path = self.offer()
        with open(offer_path) as f:
            offer = json.load(f)
        offer['model_coverage']['max_weight_bytes'] += 1
        with open(offer_path, 'w') as f:
            json.dump(offer, f)
        with self.assertRaises(SystemExit):
            cmd_prepare(offer_path, self.root, 99, api=self.api)
        with open(self.default_path(), 'rb') as f:
            self.assertEqual(f.read(), before)

    def test_context_exceeds_artifact_refuses(self):
        offer_path = self.offer()
        with open(offer_path) as f:
            offer = json.load(f)
        offer['strict_text_policy']['context_tokens'] = CONTEXT + 1
        with open(offer_path, 'w') as f:
            json.dump(offer, f)
        with self.assertRaises(SystemExit):
            cmd_prepare(offer_path, self.root, 99, api=self.api)

    def test_missing_coverage_refuses(self):
        offer_path = self.offer()
        with open(offer_path) as f:
            offer = json.load(f)
        del offer['model_coverage']
        with open(offer_path, 'w') as f:
            json.dump(offer, f)
        with self.assertRaises(SystemExit):
            cmd_prepare(offer_path, self.root, 99, api=self.api)

    def test_ready_loads_retained_only(self):
        result = cmd_prepare(self.offer(), self.root, 99, api=self.api)
        router = StubRouter({'model': result['model'], 'prompt_tokens': 1, 'usage': {}})
        ready = cmd_ready(self.root, 'http://router:8080', router=router)
        self.assertEqual(router.seen, [result['model']])
        self.assertEqual(ready['model'], result['model'])
        self.assertEqual(ready['offer'], result['offer'])
        with self.assertRaises(SystemExit):
            cmd_ready(os.path.join(self.tmp.name, 'empty-root'), 'http://router:8080',
                      router=router)

    def test_cli_smoke(self):
        prepared = {'identity': {'repository': 'owner/tiny', 'revision': SHA,
                                 'file': 'tiny.gguf', 'sha256': self.digest,
                                 'size': self.size},
                    'stats': {'context_length': CONTEXT}, 'path': self.artifact}
        with mock.patch.object(seller_model, 'prepare_registry_model',
                               return_value=prepared) as fake:
            self.assertEqual(main(['prepare', '--offer', self.offer(), '--root', self.root,
                                   '--gpu-layers', '99']), 0)
            self.assertTrue(fake.called)
        router = StubRouter({'model': 'punch-smoke', 'usage': {}})
        with mock.patch.object(seller_model, 'Router', return_value=router):
            self.assertEqual(main(['ready', '--root', self.root,
                                   '--router', 'http://router:8080']), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
