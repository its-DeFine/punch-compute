#!/usr/bin/env python3
"""Exact-artifact model eligibility: registry identity, GGUF stats, coverage.

Three stages with errors the wrapper can reuse directly (see RegistryError):

- resolve_registry_selection(selection, max_bytes, api=None): immutable
  repo/revision/file/size/SHA256 identity from HfApi(token=False), using the
  exact selected RepoFile.lfs.sha256. Mutable refs resolve once to a full
  commit. Private/gated/unknown/split artifacts are rejected: this stage
  supports one regular single GGUF file only.
- inspect_gguf(path, identity): verifies size/SHA256, then actual GGUFReader
  stats (architecture, total parameters from actual tensor elements, context,
  file type/quantization, tokenizer/template presence, modality). No
  handwritten binary parser.
- validate_coverage(stats, coverage): compares seller explicit
  max_total_parameters/max_weight_bytes/architectures/file_types/
  min_context_tokens/text-only. Unknown essential stats reject with
  MODEL_REQUIREMENTS_UNVERIFIED; exceeded known limits reject with
  MODEL_OUTSIDE_OFFER naming field, allowed and actual values.

No price/ledger changes. No weight downloads here: only bounded public
registry metadata fetches. The buyer never supplies stats, URLs, or paths.
"""
import hashlib
import os
import re

from huggingface_hub import HfApi, hf_hub_download
from pathlib import Path
import shutil
from huggingface_hub.utils import HfHubHTTPError
import gguf

MODEL_OUTSIDE_OFFER = 'MODEL_OUTSIDE_OFFER'
MODEL_REQUIREMENTS_UNVERIFIED = 'MODEL_REQUIREMENTS_UNVERIFIED'
INVALID_SELECTION = 'INVALID_SELECTION'

_SUPPORTED_REGISTRY = 'huggingface'


class RegistryError(Exception):
    """Wrapper-reusable eligibility failure."""

    def __init__(self, code, message='', field=None, allowed=None, actual=None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.field = field
        self.allowed = allowed
        self.actual = actual

    def to_dict(self):
        result = {'code': self.code, 'message': self.message}
        if self.field is not None:
            result['field'] = self.field
        if self.allowed is not None:
            result['allowed'] = self.allowed
        if self.actual is not None:
            result['actual'] = self.actual
        return result


def _check_selection_shape(selection):
    if not isinstance(selection, dict):
        raise RegistryError(INVALID_SELECTION, 'selection must be a mapping')
    for key in ('registry', 'repository', 'revision', 'file'):
        value = selection.get(key)
        if not isinstance(value, str) or not value:
            raise RegistryError(INVALID_SELECTION, 'selection.%s must be a nonempty string' % key)
    if selection['registry'] != _SUPPORTED_REGISTRY:
        raise RegistryError(INVALID_SELECTION,
                            'unsupported registry: %s' % selection['registry'])
    repo = selection['repository']
    if '://' in repo or repo.startswith('/') or '..' in repo.split('/'):
        raise RegistryError(INVALID_SELECTION, 'repository must be owner/name')
    if '/' not in repo.strip('/'):
        raise RegistryError(INVALID_SELECTION, 'repository must be owner/name')
    filename = selection['file']
    if not filename.endswith('.gguf') or filename.startswith('/') or '..' in filename.split('/'):
        raise RegistryError(INVALID_SELECTION, 'file must be a single relative .gguf path')
    return repo, selection['revision'], filename


def resolve_registry_selection(selection, max_bytes, api=None):
    """Resolve one immutable artifact identity. See module docstring."""
    repo, revision, filename = _check_selection_shape(selection)
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise RegistryError(INVALID_SELECTION, 'max_bytes must be a positive integer')
    api = api if api is not None else HfApi(token=False)
    try:
        info = api.model_info(repo, revision=revision)
    except HfHubHTTPError as e:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED,
                            'registry identity unavailable: %s' % e)
    sha = getattr(info, 'sha', None)
    if not isinstance(sha, str) or len(sha) != 40 or any(
            c not in '0123456789abcdefABCDEF' for c in sha):
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'unresolved revision commit')
    if getattr(info, 'private', False):
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'private repository unsupported')
    if getattr(info, 'gated', False):
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'gated repository unsupported')
    if re.search(r'-\d{5}-of-\d{5}\.gguf$', filename):
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'split GGUF unsupported')
    try:
        found = api.get_paths_info(repo, paths=[filename], revision=sha)
    except HfHubHTTPError as e:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED,
                            'artifact metadata unavailable: %s' % e)
    if not found:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'artifact metadata absent')
    entry = found[0]
    size = getattr(entry, 'size', None)
    lfs = getattr(entry, 'lfs', None)
    digest = getattr(lfs, 'sha256', None) if lfs is not None else None
    if not isinstance(size, int) or size <= 0:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'artifact size unknown')
    if size > max_bytes:
        raise RegistryError(MODEL_OUTSIDE_OFFER, 'artifact exceeds download bound',
                            field='size', allowed=max_bytes, actual=size)
    if not isinstance(digest, str) or len(digest) != 64 or any(
            c not in '0123456789abcdefABCDEF' for c in digest):
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'artifact SHA256 unknown')
    return {'registry': _SUPPORTED_REGISTRY, 'repository': repo, 'revision': sha,
            'file': filename, 'size': size, 'sha256': digest.lower()}


def inspect_gguf(path, identity):
    """Verify file identity, then collect actual GGUF stats. See module doc."""
    if not isinstance(identity, dict):
        raise RegistryError(INVALID_SELECTION, 'identity must be a mapping')
    for key in ('size', 'sha256'):
        if key not in identity:
            raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'identity missing ' + key)
    try:
        actual_size = os.path.getsize(path)
    except OSError:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'artifact file unreadable')
    if actual_size != identity['size']:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'artifact size drift',
                            field='size', allowed=identity['size'], actual=actual_size)
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
                digest.update(chunk)
    except OSError:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'artifact file unreadable')
    if digest.hexdigest() != str(identity['sha256']).lower():
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'artifact SHA256 mismatch')
    try:
        reader = gguf.GGUFReader(path)
    except Exception as e:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'GGUF unreadable: %s' % e)
    # Use the pinned library's public decoded-value API, not raw byte parts.
    architecture_field = reader.fields.get('general.architecture')
    architecture_name = architecture_field.contents() if architecture_field else None
    needed = {'general.architecture', 'general.file_type', 'general.quantization_version',
              'split.count', 'tokenizer.chat_template', str(architecture_name) + '.context_length'}
    kv = {name: field.contents() for name, field in reader.fields.items() if name in needed}
    if kv.get('split.count', 1) != 1:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'split GGUF unsupported')
    architecture = kv.get('general.architecture')
    if not isinstance(architecture, str) or not architecture:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'model architecture unknown')
    tensors = list(getattr(reader, 'tensors', None) or [])
    if not tensors:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'no tensors present')
    total = 0
    quants = set()
    names = []
    for tensor in tensors:
        count = int(tensor.n_elements)
        if count is None or count <= 0:
            raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'tensor element count unknown')
        total += count
        quant = tensor.tensor_type.name
        if quant:
            quants.add(quant)
        name = getattr(tensor, 'name', '')
        if isinstance(name, str) and name:
            names.append(name)
    if not quants:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'tensor quantization unknown')
    context = kv.get(architecture + '.context_length')
    tokens = reader.fields.get('tokenizer.ggml.tokens')
    has_tokenizer = tokens is not None and bool(tokens.data)
    if not has_tokenizer:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'tokenizer absent')
    try:
        file_type = gguf.LlamaFileType(kv['general.file_type']).name
    except (KeyError, ValueError):
        file_type = None
    lowered = [n.lower() for n in names]
    multimodal = any('mmproj' in n for n in lowered) or any(
        str(name).startswith(('clip.', 'vision.', 'audio.')) for name in reader.fields)
    return {'architecture': architecture, 'total_parameters': total,
            'quant_types': sorted(quants), 'context_length': context,
            'file_type': file_type,
            'quantization_version': kv.get('general.quantization_version'),
            'has_tokenizer': True,
            'has_chat_template': isinstance(kv.get('tokenizer.chat_template'), str) and bool(kv['tokenizer.chat_template'].strip()),
            'multimodal': multimodal if multimodal or architecture in {'llama', 'qwen3', 'gpt-oss'} else None, 'size': actual_size,
            'sha256': digest.hexdigest()}


def validate_coverage(stats, coverage):
    """Compare actual stats against explicit seller coverage. See module doc."""
    if not isinstance(stats, dict) or not isinstance(coverage, dict):
        raise RegistryError(INVALID_SELECTION, 'stats and coverage must be mappings')
    for key in ('max_total_parameters', 'max_weight_bytes', 'architectures',
                'file_types', 'min_context_tokens'):
        if key not in coverage:
            raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'coverage missing ' + key)
    text_only = coverage.get('text_only', coverage.get('text-only', True))
    total = stats.get('total_parameters')
    if not isinstance(total, int) or total <= 0:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'total parameter count unknown')
    if total > coverage['max_total_parameters']:
        raise RegistryError(MODEL_OUTSIDE_OFFER, 'parameter count exceeds offer maximum',
                            field='max_total_parameters',
                            allowed=coverage['max_total_parameters'], actual=total)
    size = stats.get('size')
    if not isinstance(size, int) or size <= 0:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'weight bytes unknown')
    if size > coverage['max_weight_bytes']:
        raise RegistryError(MODEL_OUTSIDE_OFFER, 'weight bytes exceed offer maximum',
                            field='max_weight_bytes',
                            allowed=coverage['max_weight_bytes'], actual=size)
    architecture = stats.get('architecture')
    if not isinstance(architecture, str) or not architecture:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'model architecture unknown')
    if architecture not in coverage['architectures']:
        raise RegistryError(MODEL_OUTSIDE_OFFER, 'architecture outside offer coverage',
                            field='architectures',
                            allowed=coverage['architectures'], actual=architecture)
    observed = stats.get('file_type')
    if not isinstance(observed, str) or not observed:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'GGUF file type unknown')
    allowed_types = coverage['file_types']
    if not isinstance(allowed_types, list) or not allowed_types:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'coverage file types unknown')
    if observed not in allowed_types:
        raise RegistryError(MODEL_OUTSIDE_OFFER, 'quantization outside offer coverage',
                            field='file_types', allowed=allowed_types, actual=observed)
    if stats.get('has_tokenizer') is not True or stats.get('has_chat_template') is not True:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'tokenizer or chat template missing')
    if text_only and stats.get('multimodal') is None:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'text-only compatibility unknown')
    context = stats.get('context_length')
    if context is None:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'context length unknown')
    if not isinstance(context, int) or context <= 0:
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'context length unknown')
    if context < coverage['min_context_tokens']:
        raise RegistryError(MODEL_OUTSIDE_OFFER, 'context below offer minimum',
                            field='min_context_tokens',
                            allowed=coverage['min_context_tokens'], actual=context)
    if text_only and stats.get('multimodal'):
        raise RegistryError(MODEL_OUTSIDE_OFFER, 'multimodal artifact outside text-only offer',
                            field='modality', allowed='text-only', actual='multimodal')
    return True


def prepare_registry_model(selection, coverage, cache_dir, *, api=None):
    """Prepare one exact public artifact using the upstream resumable cache.

    Identity/statistics are always verified before returning a usable path.
    No ledger, inference, model unloading, or credential operation occurs here.
    """
    identity = resolve_registry_selection(selection, coverage['max_weight_bytes'], api=api)
    cache = Path(cache_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    # Check for a complete cached artifact before reserving disk for a download.
    args = dict(repo_id=identity['repository'], filename=identity['file'],
                revision=identity['revision'], cache_dir=str(cache), token=False)
    try:
        path = hf_hub_download(**args, local_files_only=True)
    except Exception as error:
        from huggingface_hub.errors import LocalEntryNotFoundError
        if not isinstance(error, LocalEntryNotFoundError):
            raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'model cache unavailable') from error
        if shutil.disk_usage(cache).free < identity['size']:
            raise RegistryError('MODEL_CAPACITY_UNAVAILABLE', 'insufficient disk for model artifact')
        try:
            path = hf_hub_download(**args)
        except Exception as error:
            raise RegistryError('MODEL_DOWNLOAD_FAILED', 'public artifact download failed; retained cache can resume') from error
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_relative_to(cache):
        raise RegistryError(MODEL_REQUIREMENTS_UNVERIFIED, 'artifact escaped model cache')
    stats = inspect_gguf(resolved, identity)
    validate_coverage(stats, coverage)
    return {'identity': identity, 'stats': stats, 'path': str(resolved)}
