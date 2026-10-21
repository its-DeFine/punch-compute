#!/usr/bin/env python3
"""Seller default-model preparation. No ledger, no credentials, no daemons.

prepare: validate an offer file's default_model selection, model_coverage and
strict_text_policy with the existing registry/runtime modules, stage the exact
verified artifact under the models root, publish one immutable preset, and
save private root/default.json (identity/stats/model/profile/offer). The
saved offer's model becomes the immutable preset model ID; default_model is
the exact resolved selection. A retained default is reused on identical input
and refuses conflicting input instead of replacing the seller default.

ready: load only the retained default through the actual router and print the
public model/offer pair.
"""
import argparse
import json
import os
import sys

from model_registry import RegistryError, prepare_registry_model
from model_runtime import ModelRuntimeError, Router, add_model_preset

OFFER_KEYS = ('registry', 'repository', 'revision', 'file')


def fail(message):
    raise SystemExit('seller_model: ' + message)


def read_json_file(path):
    if not os.path.isfile(path) or os.path.islink(path):
        fail('unreadable file: ' + path)
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)


def selection_of(offer):
    try:
        selected = offer['default_model']
    except (KeyError, TypeError):
        fail('offer missing default_model')
    if not isinstance(selected, dict) or set(selected) != set(OFFER_KEYS):
        fail('default_model must hold exactly registry/repository/revision/file')
    return dict(selected)


def profile_of(offer, gpu_layers):
    try:
        policy = offer['strict_text_policy']
    except (KeyError, TypeError):
        fail('offer missing strict_text_policy')
    for key in ('context_tokens', 'max_output_tokens', 'reasoning_effort'):
        if key not in policy:
            fail('strict_text_policy missing ' + key)
    if type(gpu_layers) is not int:
        fail('gpu layers must be an integer')
    return {'context_tokens': policy['context_tokens'],
            'max_output_tokens': policy['max_output_tokens'],
            'parallel': 1, 'gpu_layers': gpu_layers,
            'reasoning_effort': policy['reasoning_effort']}


def canonical_input(offer, gpu_layers):
    return {'default_model': selection_of(offer),
            'model_coverage': offer.get('model_coverage'),
            'strict_text_policy': offer.get('strict_text_policy'),
            'gpu_layers': gpu_layers}


def write_private_json(path, value):
    text = json.dumps(value, sort_keys=True, separators=(',', ':'))
    temporary = path + '.tmp'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    fd = os.open(os.path.dirname(path) or '.', os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def cmd_prepare(offer_path, root, gpu_layers, api=None):
    offer = read_json_file(offer_path)
    if not isinstance(offer, dict):
        fail('offer must be a JSON object')
    root = os.path.abspath(root)
    os.umask(0o077)
    os.makedirs(root, mode=0o700, exist_ok=True)
    wanted = canonical_input(offer, gpu_layers)
    if not isinstance(wanted['model_coverage'], dict):
        fail('offer missing model_coverage')
    default_path = os.path.join(root, 'default.json')
    retained = None
    if os.path.lexists(default_path):
        retained = read_json_file(default_path)
        if not isinstance(retained, dict) or retained.get('offer') is None:
            fail('retained default malformed; refusing to replace')
    try:
        prepared = prepare_registry_model(wanted['default_model'],
                                          wanted['model_coverage'],
                                          os.path.join(root, 'cache'), api=api)
    except RegistryError as error:
        fail('registry: ' + error.message)
    resolved = {'default_model': {'registry': 'huggingface',
                                  'repository': prepared['identity']['repository'],
                                  'revision': prepared['identity']['revision'],
                                  'file': prepared['identity']['file']},
                'model_coverage': wanted['model_coverage'],
                'strict_text_policy': wanted['strict_text_policy'],
                'gpu_layers': gpu_layers}
    if retained is not None:
        have = {'default_model': retained['offer'].get('default_model'),
                'model_coverage': retained['offer'].get('model_coverage'),
                'strict_text_policy': retained['offer'].get('strict_text_policy'),
                'gpu_layers': (retained.get('profile') or {}).get('gpu_layers')}
        if have != resolved:
            fail('conflicting seller default input; refusing to replace')
    stats = prepared['stats']
    profile = profile_of(offer, gpu_layers)
    for key in ('context_tokens', 'max_output_tokens'):
        if type(profile[key]) is not int:
            fail('strict_text_policy ' + key + ' must be an integer')
    if profile['context_tokens'] > (stats.get('context_length') or 0):
        fail('offer context exceeds prepared artifact context')
    try:
        model_id = add_model_preset(root, prepared['path'], prepared['identity']['sha256'], profile)
    except ModelRuntimeError as error:
        fail('preset: ' + str(error))
    saved_offer = {k: v for k, v in offer.items() if k != 'model'}
    saved_offer['model'] = model_id
    saved_offer['default_model'] = {'registry': 'huggingface',
                                    'repository': prepared['identity']['repository'],
                                    'revision': prepared['identity']['revision'],
                                    'file': prepared['identity']['file']}
    default = {'identity': prepared['identity'], 'stats': prepared['stats'],
               'model': model_id, 'profile': profile, 'offer': saved_offer}
    write_private_json(default_path, default)
    return default


def cmd_ready(root, router_url, router=None):
    default = read_json_file(os.path.join(os.path.abspath(root), 'default.json'))
    if not isinstance(default, dict) or not default.get('model') or not default.get('offer'):
        fail('no retained seller default')
    router = router if router is not None else Router(router_url)
    try:
        loaded = router.load(default['model'])
    except Exception as error:
        fail('router: ' + str(error))
    return {'model': default['model'], 'offer': default['offer'],
            'router': loaded}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='seller_model')
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--offer', required=True)
    prepare.add_argument('--root', required=True)
    prepare.add_argument('--gpu-layers', required=True, type=int)
    ready = sub.add_parser('ready')
    ready.add_argument('--root', required=True)
    ready.add_argument('--router', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = cmd_prepare(args.offer, args.root, args.gpu_layers)
            print(json.dumps({'model': result['model'], 'offer': result['offer']},
                             sort_keys=True))
        else:
            print(json.dumps(cmd_ready(args.root, args.router), sort_keys=True))
    except RegistryError as error:
        fail('registry: ' + error.message)
    except ModelRuntimeError as error:
        fail('runtime: ' + str(error))
    return 0


if __name__ == '__main__':
    sys.exit(main())
