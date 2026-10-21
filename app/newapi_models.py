#!/usr/bin/env python3
"""Native adapter for order model switching. Not a ledger and not a router.

Small explicit functions over the existing gateway ``api`` callable, which
already unwraps response envelopes to ``data``. Every function takes its
session explicitly: the owned (managed) session for token operations, the
admin session for channel operations. No global state, no retries, no
recreation, no refills.

Source references (work/punch-token-orders-native-20260919 @ c49b842ff):
- token disable/enable: PUT /api/token/?status_only=true
  (controller/token.go UpdateToken; narrow status path)
- token model swap: PUT /api/token/?model_only=true with
  {id, expected_model, model} (controller/token.go updateTokenStrictModel:
  requires disabled token, zero admitted requests, CAS model_limits alone)
- token readback: GET /api/token/{id}
- channel add/find: POST /api/channel/ and GET /api/channel/?p=1&page_size=100
  (controller/channel.go AddChannel/FetchChannels; creation payload mirrors
  setup.py ensure_channel)
- status constants: common/constants.go TokenStatusEnabled=1,
  TokenStatusDisabled=2, TokenStatusExpired=3, TokenStatusExhausted=4;
  ChannelStatusEnabled=1, ChannelStatusManuallyDisabled=2.

Drain honesty: disabling is one PUT plus one authoritative GET. If the GET
disagrees, or the later CAS refuses on admitted requests, the error
propagates with no retry loop and no claim about drain state. Ambiguous
readbacks raise instead of repairing anything.
"""
TOKEN_STATUS_ENABLED = 1
TOKEN_STATUS_DISABLED = 2
CHANNEL_STATUS_ENABLED = 1
CHANNEL_STATUS_MANUALLY_DISABLED = 2
OPENAI_CHANNEL_TYPE = 1


class AdapterError(Exception):
    def __init__(self, code, message=''):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _channels(api, admin_token):
    data = api('GET', '/api/channel/?p=1&page_size=100', token=admin_token)
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise AdapterError('CHANNEL_LIST_UNEXPECTED')
    if data.get('total', len(data['items'])) > len(data['items']):
        raise AdapterError('CHANNEL_LIST_INCOMPLETE')
    return data['items']


def ensure_model_channel(api, admin_token, *, name, router_url, model, group='default'):
    """Reuse exact native channel terms; never overwrite unrelated routing.

    The current single seller fixture follows setup.py's supported channel
    payload. New API returns success without a created row: read it back.
    """
    def find():
        rows = [c for c in _channels(api, admin_token) if c.get('name') == name]
        if len(rows) > 1:
            raise AdapterError('CHANNEL_IDENTITY_AMBIGUOUS')
        if not rows:
            return None
        channel = rows[0]
        if any(channel.get(k) != v for k, v in {
                'type': OPENAI_CHANNEL_TYPE, 'base_url': router_url,
                'models': model, 'group': group, 'status': CHANNEL_STATUS_ENABLED}.items()):
            raise AdapterError('CHANNEL_TERMS_CONFLICT')
        if channel.get('model_mapping') not in (None, '', '{}'):
            raise AdapterError('CHANNEL_TERMS_CONFLICT')
        return channel
    channel = find()
    if channel is not None:
        return channel
    try:
        api('POST', '/api/channel/', {'channel': {
            'type': OPENAI_CHANNEL_TYPE, 'key': 'sk-local-router',
            'base_url': router_url, 'name': name, 'group': group,
            'models': model, 'status': CHANNEL_STATUS_ENABLED}, 'mode': 'single'},
            token=admin_token)
    except Exception:
        # A response can be lost after creation. Read, never blindly create twice.
        channel = find()
        if channel is None:
            raise
        return channel
    channel = find()
    if channel is None:
        raise AdapterError('CHANNEL_CREATE_UNVERIFIED')
    return channel


def read_token(api, sess, token_id):
    """Authoritative single-token readback. No writes of any kind."""
    data = api('GET', '/api/token/%d' % token_id, token=sess)
    if not isinstance(data, dict) or data.get('id') != token_id:
        raise AdapterError('TOKEN_READBACK_AMBIGUOUS')
    return data


def check_token_steady(read, expected):
    """Compare a readback against expected identity fields without writing.
    Raises on the first mismatch; repairs nothing, refills nothing."""
    for field in ('id', 'name', 'remain_quota', 'used_quota', 'expired_time',
                  'model_limits', 'strict_text_policy'):
        if field in expected and read.get(field) != expected[field]:
            raise AdapterError('TOKEN_IDENTITY_CHANGED')
    return True


def set_token_status(api, sess, token_id, status):
    """One status_only write plus one authoritative readback. No retries:
    a disagreeing readback raises and the caller owns what happens next."""
    api('PUT', '/api/token/?status_only=true',
        {'id': token_id, 'status': status}, token=sess)
    read = read_token(api, sess, token_id)
    if read.get('status') != status:
        raise AdapterError('TOKEN_STATUS_UNRESOLVED')
    return read


def disable_token(api, sess, token_id):
    """Drain initiator: disable, then verify disabled via readback."""
    return set_token_status(api, sess, token_id, TOKEN_STATUS_DISABLED)


def enable_token(api, sess, token_id):
    """Re-enable only through the existing status_only path, verified."""
    return set_token_status(api, sess, token_id, TOKEN_STATUS_ENABLED)


def cas_token_model(api, sess, token_id, expected_model, new_model):
    """One model_only compare-and-swap. Native refuses unless the token is
    disabled with zero admitted requests; that refusal (or any other)
    propagates untouched: no retry, no drain claim."""
    data = api('PUT', '/api/token/?model_only=true',
               {'id': token_id, 'expected_model': expected_model,
                'model': new_model}, token=sess)
    if not isinstance(data, dict) or data.get('model_limits') != new_model:
        raise AdapterError('TOKEN_MODEL_UNRESOLVED')
    return data
