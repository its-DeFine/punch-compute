#!/usr/bin/env python3
"""Synthetic boundary tests for app/newapi_models.py.

A recording fake stands in for the gateway api callable: it returns canned
unwrapped-``data`` payloads shaped like the real New API envelopes (channel
rows and token rows per controller/channel.go and controller/token.go) and
records every call so exact methods, paths and bodies are asserted. These
cover adapter errors and wiring only; the required real-binary acceptance
against actual New API is run separately by root.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

import newapi_models
from newapi_models import (
    AdapterError,
    cas_token_model,
    check_token_steady,
    disable_token,
    enable_token,
    ensure_model_channel,
    read_token,
)

ADMIN = 'admin-session-synthetic'
OWNED = 'owned-session-synthetic'
CHANNEL = {'id': 7, 'name': 'llama-model-b', 'type': 1, 'group': 'default', 'status': 1,
           'base_url': 'http://router:8080', 'models': 'model-b'}
TOKEN = {'id': 11, 'name': 'ord-abc', 'status': 1, 'remain_quota': 100,
         'used_quota': 0, 'expired_time': 999, 'model_limits': 'model-a',
         'model_limits_enabled': True,
         'strict_text_policy': {'input_rate': 1.0}}


class FakeApi:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, method, path, body=None, token=None):
        self.calls.append((method, path, body, token))
        if not self.script:
            raise AssertionError('unexpected call %r %r' % (method, path))
        expect_method, expect_path, result = self.script.pop(0)
        assert (method, path) == (expect_method, expect_path), (method, path)
        if isinstance(result, Exception):
            raise result
        return result

    def writes(self):
        return [c for c in self.calls if c[0] in ('PUT', 'POST', 'DELETE')]


def channel_list(*rows):
    return {'items':list(rows),'total':len(rows)}


class ChannelTest(unittest.TestCase):
    def test_find_exact_without_create(self):
        api = FakeApi([('GET', '/api/channel/?p=1&page_size=100', channel_list({'id': 1}, CHANNEL))])
        found = ensure_model_channel(api, ADMIN, name='llama-model-b',
                                     router_url='http://router:8080', model='model-b')
        self.assertEqual(found['id'], 7)
        self.assertFalse(api.writes())

    def test_create_with_setup_payload_then_verify(self):
        api = FakeApi([
            ('GET', '/api/channel/?p=1&page_size=100', channel_list()),
            ('POST', '/api/channel/', None),
            ('GET', '/api/channel/?p=1&page_size=100', channel_list(CHANNEL)),
        ])
        found = ensure_model_channel(api, ADMIN, name='llama-model-b',
                                     router_url='http://router:8080', model='model-b')
        self.assertEqual(found, CHANNEL)
        post = api.calls[1]
        self.assertEqual(post[0], 'POST')
        self.assertEqual(post[1], '/api/channel/')
        self.assertEqual(post[2], {'channel': {
            'type': 1, 'key': 'sk-local-router', 'base_url': 'http://router:8080',
            'name': 'llama-model-b', 'group': 'default', 'models': 'model-b', 'status': 1},
            'mode': 'single'})
        self.assertEqual(api.calls[0][3], ADMIN)
        self.assertEqual(post[3], ADMIN)

    def test_same_name_different_terms_refused(self):
        other = dict(CHANNEL, models='model-c')
        api = FakeApi([('GET', '/api/channel/?p=1&page_size=100', channel_list(other))])
        with self.assertRaises(AdapterError) as raised:
            ensure_model_channel(api, ADMIN, name='llama-model-b',
                                 router_url='http://router:8080', model='model-b')
        self.assertEqual(raised.exception.code, 'CHANNEL_TERMS_CONFLICT')
        self.assertFalse(api.writes())

    def test_create_unverified(self):
        api = FakeApi([
            ('GET', '/api/channel/?p=1&page_size=100', channel_list()),
            ('POST', '/api/channel/', None),
            ('GET', '/api/channel/?p=1&page_size=100', channel_list()),
        ])
        with self.assertRaises(AdapterError) as raised:
            ensure_model_channel(api, ADMIN, name='llama-model-b',
                                 router_url='http://router:8080', model='model-b')
        self.assertEqual(raised.exception.code, 'CHANNEL_CREATE_UNVERIFIED')


class TokenTest(unittest.TestCase):
    def test_disable_writes_once_then_verifies(self):
        api = FakeApi([
            ('PUT', '/api/token/?status_only=true', dict(TOKEN, status=2)),
            ('GET', '/api/token/11', dict(TOKEN, status=2)),
        ])
        read = disable_token(api, OWNED, 11)
        self.assertEqual(read['status'], 2)
        put = api.calls[0]
        self.assertEqual(put[2], {'id': 11, 'status': 2})
        self.assertEqual(put[3], OWNED)
        self.assertEqual(len(api.writes()), 1)

    def test_disable_unresolved_no_retry(self):
        api = FakeApi([
            ('PUT', '/api/token/?status_only=true', dict(TOKEN, status=2)),
            ('GET', '/api/token/11', dict(TOKEN, status=1)),
        ])
        with self.assertRaises(AdapterError) as raised:
            disable_token(api, OWNED, 11)
        self.assertEqual(raised.exception.code, 'TOKEN_STATUS_UNRESOLVED')
        self.assertEqual(len(api.writes()), 1)

    def test_enable_uses_status_only(self):
        api = FakeApi([
            ('PUT', '/api/token/?status_only=true', dict(TOKEN, status=1)),
            ('GET', '/api/token/11', dict(TOKEN, status=1)),
        ])
        read = enable_token(api, OWNED, 11)
        self.assertEqual(read['status'], 1)
        self.assertEqual(api.calls[0][2], {'id': 11, 'status': 1})

    def test_cas_posts_exact_body(self):
        api = FakeApi([
            ('PUT', '/api/token/?model_only=true', dict(TOKEN, model_limits='model-b')),
        ])
        data = cas_token_model(api, OWNED, 11, 'model-a', 'model-b')
        self.assertEqual(data['model_limits'], 'model-b')
        put = api.calls[0]
        self.assertEqual(put[2], {'id': 11, 'expected_model': 'model-a', 'model': 'model-b'})
        self.assertEqual(put[3], OWNED)

    def test_cas_unresolved(self):
        api = FakeApi([
            ('PUT', '/api/token/?model_only=true', dict(TOKEN, model_limits='model-a')),
        ])
        with self.assertRaises(AdapterError) as raised:
            cas_token_model(api, OWNED, 11, 'model-a', 'model-b')
        self.assertEqual(raised.exception.code, 'TOKEN_MODEL_UNRESOLVED')

    def test_read_and_steady_never_write(self):
        api = FakeApi([('GET', '/api/token/11', dict(TOKEN))])
        read = read_token(api, OWNED, 11)
        self.assertTrue(check_token_steady(read, dict(TOKEN)))
        self.assertFalse(api.writes())
        with self.assertRaises(AdapterError) as raised:
            check_token_steady(read, dict(TOKEN, remain_quota=50))
        self.assertEqual(raised.exception.code, 'TOKEN_IDENTITY_CHANGED')
        self.assertFalse(api.writes())

    def test_ambiguous_readback_raises(self):
        api = FakeApi([('GET', '/api/token/11', [dict(TOKEN)])])
        with self.assertRaises(AdapterError) as raised:
            read_token(api, OWNED, 11)
        self.assertEqual(raised.exception.code, 'TOKEN_READBACK_AMBIGUOUS')

    def test_no_refill_or_recreate_surface(self):
        with open(newapi_models.__file__, encoding='utf-8') as f:
            source = f.read()
        for marker in ('add_quota', '/api/user/', '/api/user/manage', 'top_up', 'TopUp'):
            self.assertNotIn(marker, source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
