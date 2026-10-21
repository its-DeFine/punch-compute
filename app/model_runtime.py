"""Pinned llama.cpp router control. No process manager or Docker access.

The order coordinator owns draining, capacity and durable operation state.
This module owns immutable presets and verifies the router's actual readback.
"""
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.request
from urllib.error import HTTPError


class ModelRuntimeError(Exception):
    def __init__(self, message, code='MODEL_RUNTIME_FAILED'):
        super().__init__(message)
        self.code = code

    def to_dict(self):
        return {'code': self.code, 'message': str(self)}


def add_model_preset(root, artifact_path, sha256, profile):
    """Add a verified artifact; never rewrite a previously published preset."""
    root = Path(root).resolve()
    artifact = Path(artifact_path).resolve(strict=True)
    relative = artifact.relative_to(root)
    if not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise ModelRuntimeError('invalid artifact identity')
    if any(c in relative.as_posix() for c in '\r\n\x00'):
        raise ModelRuntimeError('invalid artifact path')
    required = {'context_tokens', 'max_output_tokens', 'parallel', 'gpu_layers', 'reasoning_effort'}
    if set(profile) != required:
        raise ModelRuntimeError('incomplete model profile')
    for key, maximum in [('context_tokens', 1048576), ('max_output_tokens', 1048576), ('parallel', 16), ('gpu_layers', 999)]:
        value = profile[key]
        if type(value) is not int or not (0 if key == 'gpu_layers' else 1) <= value <= maximum:
            raise ModelRuntimeError('invalid model profile ' + key)
    if profile['max_output_tokens'] > profile['context_tokens'] or profile['reasoning_effort'] not in ('', 'low'):
        raise ModelRuntimeError('invalid model output profile')
    digest = hashlib.sha256(json.dumps({'sha256': sha256, 'profile': profile}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    model_id = 'punch-' + digest
    settings = {'model': '/models/' + relative.as_posix(), 'ctx-size': str(profile['context_tokens']),
                'n-predict': str(profile['max_output_tokens']), 'parallel': str(profile['parallel']),
                'n-gpu-layers': str(profile['gpu_layers']), 'jinja': 'true', 'threads': '2', 'threads-batch': '2'}
    if profile['reasoning_effort']:
        settings['reasoning-effort'] = profile['reasoning_effort']
    catalog = root / 'models.ini'
    parser = configparser.ConfigParser(interpolation=None)
    if catalog.exists():
        parser.read_string('[DEFAULT]\n' + catalog.read_text())
        if parser.defaults() != {'version': '1'}:
            raise ModelRuntimeError('unsupported catalog')
    if parser.has_section(model_id):
        existing = {k: v for k, v in parser[model_id].items() if k != 'version'}
        if existing != settings:
            without_path = lambda item: {k: v for k, v in item.items() if k != 'model'}
            if without_path(existing) != without_path(settings) or not existing.get('model', '').startswith('/models/'):
                raise ModelRuntimeError('immutable model preset changed', 'MODEL_PRESET_CONFLICT')
            # A verified artifact may occur at multiple registry cache paths.
            # Keep the published path; verify both files instead of rewriting it.
            previous = (root / existing['model'][len('/models/'):]).resolve(strict=True)
            previous.relative_to(root)
            for candidate in {previous, artifact}:
                digest = hashlib.sha256()
                with candidate.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                if digest.hexdigest() != sha256:
                    raise ModelRuntimeError('retained model artifact differs from its identity', 'MODEL_ARTIFACT_CHANGED')
        return model_id
    # Keep existing bytes exactly, so refresh cannot alter a loaded entry.
    prior = catalog.read_text() if catalog.exists() else 'version = 1\n'
    text = prior.rstrip() + '\n\n[' + model_id + ']\n' + ''.join(k + ' = ' + v + '\n' for k, v in settings.items())
    temporary = catalog.with_suffix('.ini.tmp')
    with temporary.open('w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, catalog)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return model_id


class Router:
    def __init__(self, base_url):
        self.base = base_url.rstrip('/')
        self.opener = urllib.request.build_opener(_NoRedirect())

    def request(self, path, body=None, timeout=5):
        request = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ModelRuntimeError('router response too large')
                return json.loads(data)
        except HTTPError as error:
            status = error.code
            error.close()
            raise ModelRuntimeError('router '+path+' returned HTTP '+str(status),
                                    'MODEL_ROUTER_HTTP_'+str(status)) from error

    def models(self, reload=False):
        result = self.request('/models?reload=1' if reload else '/models')
        return {item['id']: item for item in result['data']}

    def wait(self, model_id, expected, deadline):
        while time.monotonic() < deadline:
            item = self.models().get(model_id)
            if item is None:
                raise ModelRuntimeError('model absent from router catalog')
            status = item['status']
            if status.get('failed') and expected == 'loaded':
                raise ModelRuntimeError('model load failed; exit=' + str(status.get('exit_code')))
            if status['value'] == expected:
                return item
            time.sleep(.1)
        raise ModelRuntimeError('model ' + expected + ' deadline exceeded')

    def load(self, model_id, timeout=120):
        deadline = time.monotonic() + timeout
        item = self.models(reload=True).get(model_id)
        if item is None:
            raise ModelRuntimeError('model absent from router catalog')
        if item['status']['value'] not in ('loaded', 'loading'):
            self.request('/models/load', {'model': model_id})
        self.wait(model_id, 'loaded', deadline)
        # Same explicit model for template, tokenizer and generation, as in billing.
        body = {'model': model_id, 'messages': [{'role': 'user', 'content': 'Hello'}], 'max_tokens': 1}
        prompt = self.request('/apply-template', body)['prompt']
        tokens = self.request('/tokenize', {'model': model_id, 'content': prompt, 'add_special': True, 'parse_special': True})['tokens']
        if not isinstance(tokens, list) or not tokens:
            raise ModelRuntimeError('model tokenizer not ready')
        answer = self.request('/v1/chat/completions', body, timeout=max(1, min(30, deadline-time.monotonic())))
        if answer.get('model') != model_id or not answer.get('choices') or not answer.get('usage'):
            raise ModelRuntimeError('model generation identity/usage not ready')
        return {'model': model_id, 'prompt_tokens': len(tokens), 'usage': answer['usage']}

    def unload(self, model_id, timeout=30):
        item = self.models().get(model_id)
        if item is None:
            raise ModelRuntimeError('model absent from router catalog')
        if item['status']['value'] != 'unloaded':
            self.request('/models/unload', {'model': model_id})
        self.wait(model_id, 'unloaded', time.monotonic()+timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelRuntimeError('router redirect refused')
