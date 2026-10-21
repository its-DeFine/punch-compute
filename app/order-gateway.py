#!/usr/bin/env python3
"""Private offer/order mapping. New API alone owns inference and credit balances."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import time
import threading
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit

BASE = os.environ.get('GW_ADMIN_BASE', 'http://127.0.0.1:18085').rstrip('/')
DATA = os.environ.get('GW_DATA_DIR', '/tmp/punch-token-orders-data')
PORT = int(os.environ.get('GW_PORT', '18086'))
PUBLIC_BASE = os.environ.get('GW_PUBLIC_API_BASE', BASE)
ORDER_PUBLIC_BASE = os.environ.get('GW_PUBLIC_ORDER_BASE', 'http://127.0.0.1:' + str(PORT)).rstrip('/')
BIND = os.environ.get('GW_BIND', '127.0.0.1')
MODEL = os.environ.get('GW_MODEL', 'punch-test-model')
RATES = dict(input_rate=1.0, output_rate=5.0, cache_rate=1.0)
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
PAYER_ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
# Paid checkout gate. When true the free /acquire route is blocked and paid
# orders are prepared via /purchases/prepare; native activation lands next.
CHECKOUT_LIVE = os.environ.get('GW_LIVEPEER_CHECKOUT', '').strip().lower() == 'true'
NATIVE_NODE = os.environ.get('GW_NATIVE_NODE_BASE', '').rstrip('/')
# Optional dynamic native runner registration. When GW_NATIVE_REGISTRATION_FILE
# (private master secret file), GW_NATIVE_RUNNER_URL (internal
# wrapper URL advertised as the runner endpoint), and NATIVE_NODE are all set,
# the wrapper heartbeats each published native runner on its own serial server
# loop. Without them every old behavior is unchanged.
NATIVE_REGISTRATION_FILE = os.environ.get('GW_NATIVE_REGISTRATION_FILE', '').strip()
NATIVE_RUNNER_URL = os.environ.get('GW_NATIVE_RUNNER_URL', '').rstrip('/')
NATIVE_APP = 'punch/compute-v1'
NATIVE_MODE = 'single-shot'
NATIVE_HEARTBEAT_TIMEOUT = 2
NATIVE_HEARTBEAT_DEFAULT_INTERVAL = 30.0
NATIVE_HEARTBEAT_RETRY_DELAY = 2.0


class Failure(Exception):
    def __init__(self, code, status=502):
        self.code, self.status = code, status


def read_secret(path):
    with open(path, 'rb') as f:
        mode = os.fstat(f.fileno()).st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise Failure('PRIVATE_FILE_REQUIRED', 500)
        return f.read().strip().decode()


os.umask(0o077)
os.makedirs(DATA, mode=0o700, exist_ok=True)
SELLER_TOKEN = read_secret(os.environ['GW_SELLER_TOKEN_FILE'])
ADMIN_USER = read_secret(os.environ['GW_ADMIN_USER_FILE'])
ADMIN_PASS = read_secret(os.environ['GW_ADMIN_PASS_FILE'])
OFFERS_FILE = os.path.join(DATA, 'offers.json')
ORDERS_FILE = os.path.join(DATA, 'orders.json')
SESSIONS_FILE = os.path.join(DATA, 'sessions.json')
RUNNERS_FILE = os.path.join(DATA, 'native_runners.json')
STATE_LOCK = threading.RLock()
MODELS = None
MAINTENANCE = None
DEFAULT_MODEL = None
if os.environ.get('GW_DEFAULT_MODEL_FILE'):
    DEFAULT_MODEL = json.loads(read_secret(os.environ['GW_DEFAULT_MODEL_FILE']))
    MODEL = DEFAULT_MODEL['model']
    if not re.fullmatch(r'punch-[0-9a-f]{64}', MODEL):
        raise Failure('DEFAULT_MODEL_INVALID', 500)


def load(path):
    try:
        value = json.loads(read_secret(path))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        raise Failure('STATE_UNREADABLE', 500)
    if not isinstance(value, dict):
        raise Failure('STATE_INVALID', 500)
    return value


def save(path, value):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f, sort_keys=True, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def api(method, path, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(BASE + path, headers=headers, method=method,
        data=None if body is None else json.dumps(body, allow_nan=False).encode())
    try:
        with urllib.request.urlopen(req, timeout=40 if path == "/api/token/?status_only=true" else 20) as response:
            value = json.load(response)
    except HTTPError as e:
        raise Failure('UPSTREAM_RATE_LIMITED' if e.code == 429 else 'UPSTREAM_HTTP_' + str(e.code))
    if not isinstance(value, dict) or value.get('success') is not True:
        raise Failure('UPSTREAM_REJECTED')
    return value.get('data')


def session(username, password):
    sessions = load(SESSIONS_FILE)
    saved = sessions.get(username)
    if saved and saved.get('expires_at', 0) > time.time() + 10:
        try:
            api('GET', '/api/user/self', token=saved['token'])
            return saved['token']
        except Failure as error:
            if error.code not in ('UPSTREAM_HTTP_401', 'UPSTREAM_HTTP_403', 'UPSTREAM_REJECTED'):
                raise
            # Real rc38 restart invalidates JWT signatures while retaining users,
            # orders and inference keys. Reauthenticate; never recreate a grant.

    result = api('POST', '/api/user/login', {'username': username, 'password': password})
    token = result['access_token']
    expires = result.get('access_expires_at')
    if not isinstance(expires, (int, float)):
        expires = time.time() + 60
    sessions[username] = {'token': token, 'expires_at': expires}
    save(SESSIONS_FILE, sessions)
    return token


def admin():
    return session(ADMIN_USER, ADMIN_PASS)


def buyer_identity(token):
    try:
        user = api('GET', '/api/user/self', token=token)
    except Failure:
        raise Failure('BUYER_AUTH_INVALID', 401)
    if not user or not isinstance(user.get('id'), int):
        raise Failure('BUYER_AUTH_INVALID', 401)
    # Managed order identities are deliberately not buyer login identities.
    if user.get('username', '').startswith('ord-'):
        raise Failure('BUYER_AUTH_INVALID', 401)
    return user['id']


def owned_session(order):
    return session(order['managed_username'], order['managed_password'])


def item_list(result):
    if not isinstance(result, dict) or not isinstance(result.get('items'), list):
        raise Failure('UPSTREAM_SHAPE_INVALID')
    return result['items']


def get_account(order):
    users = item_list(api('GET', '/api/user/search?keyword=' + quote(order['managed_username']), token=admin()))
    matches = [u for u in users if u.get('username') == order['managed_username']]
    if len(matches) > 1:
        raise Failure('ORDER_ACCOUNT_AMBIGUOUS')
    return matches[0] if matches else None


def native_terms(offer):
    """Seller-authorized native terms, validated together or not at all."""
    runner = offer.get('native_runner_id')
    recipient = offer.get('native_recipient_address')
    price = offer.get('price_wei')
    if (not isinstance(runner, str) or not 0 < len(runner) <= 128
            or not isinstance(recipient, str) or not PAYER_ADDRESS.fullmatch(recipient)
            or type(price) is not int or not 0 < price <= 9223372036854775807):
        raise Failure('NATIVE_TERMS_REQUIRED', 400)
    return runner, recipient.lower(), price


def strict_policy(body):
    policy = body.get('strict_text_policy')
    if policy is None:
        return None
    fields = set(RATES) | {'context_tokens', 'max_output_tokens', 'reasoning_effort'}
    if not isinstance(policy, dict) or set(policy) != fields:
        raise Failure('INVALID_BILLING_POLICY', 400)
    for field in RATES:
        value = policy[field]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (field != 'cache_rate' and value == 0) or value != body.get(field):
            raise Failure('INVALID_BILLING_POLICY', 400)
    if (policy['cache_rate'] > policy['input_rate']
        or not math.isfinite(policy['output_rate'] / policy['input_rate'])
        or policy['output_rate'] / policy['input_rate'] == 0):
        raise Failure('INVALID_BILLING_POLICY', 400)
    # Current verified model runtime bounds. Model enrollment will supply the
    # verified per-model limits; an offer cannot enlarge the running profile.
    if (type(policy['context_tokens']) is not int or not 0 < policy['context_tokens'] <= 4096
        or type(policy['max_output_tokens']) is not int or not 0 < policy['max_output_tokens'] <= min(256, policy['context_tokens'])
        or policy['reasoning_effort'] != 'low'):
        raise Failure('MODEL_PROFILE_UNVERIFIED', 400)
    status = api('GET', '/api/status')
    if not isinstance(status, dict) or status.get('punch_strict_text_policy') != 1:
        raise Failure('NATIVE_BILLING_POLICY_UNSUPPORTED', 503)
    return dict(policy)


COVERAGE_FIELDS = ('max_total_parameters', 'max_weight_bytes', 'architectures',
    'file_types', 'min_context_tokens', 'text_only')
QUANT_NAME = re.compile(r'^[A-Za-z0-9_]{1,32}$')


def model_coverage(body, policy):
    """Immutable optional model eligibility terms. Terms preservation only:
    validated and pinned here, verified against artifacts by model_registry."""
    coverage = body.get('model_coverage')
    if coverage is None:
        return None
    if policy is None:
        raise Failure('MODEL_COVERAGE_REQUIRES_POLICY', 400)
    if not isinstance(coverage, dict) or set(coverage) != set(COVERAGE_FIELDS):
        raise Failure('INVALID_MODEL_COVERAGE', 400)
    for field in ('max_total_parameters', 'max_weight_bytes', 'min_context_tokens'):
        value = coverage[field]
        if type(value) is not int or not 0 < value <= 9223372036854775807:
            raise Failure('INVALID_MODEL_COVERAGE', 400)
    for field in ('architectures', 'file_types'):
        values = coverage[field]
        if (not isinstance(values, list) or not values
                or any(not isinstance(v, str) or not v or len(v) > 128 for v in values)):
            raise Failure('INVALID_MODEL_COVERAGE', 400)
    if any(not QUANT_NAME.fullmatch(v) for v in coverage['file_types']):
        raise Failure('INVALID_MODEL_COVERAGE', 400)
    if coverage['text_only'] is not True:
        raise Failure('INVALID_MODEL_COVERAGE', 400)
    return dict(coverage)


def purchase_intent(terms):
    """SHA256 over canonical sorted JSON of public immutable terms only."""
    canonical = json.dumps(terms, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def native_registration_configured():
    return bool(NATIVE_NODE and NATIVE_REGISTRATION_FILE and NATIVE_RUNNER_URL)


def native_master_secret():
    try:
        value = read_secret(NATIVE_REGISTRATION_FILE)
    except OSError:
        raise Failure('NATIVE_REGISTRATION_UNREADABLE', 500)
    if not value or any(c.isspace() for c in value):
        raise Failure('NATIVE_REGISTRATION_INVALID', 500)
    return value


def native_offer_digest(offer_id, runner, recipient, price, offer):
    canonical = json.dumps({'offer_id': offer_id, 'runner_id': runner,
        'recipient': recipient, 'price_wei': price, 'model': offer.get('model'),
        'input_rate': offer.get('input_rate'), 'output_rate': offer.get('output_rate'),
        'cache_rate': offer.get('cache_rate'),
        'allowance_credits': offer.get('allowance_credits'),
        'window_seconds': offer.get('window_seconds'),
        **({'strict_text_policy': offer['strict_text_policy']} if offer.get('strict_text_policy') else {}),
        **({'model_coverage': offer['model_coverage']} if offer.get('model_coverage') else {})},
        sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def native_runner_metadata(offer_id, runner_id, record):
    raw = json.dumps({'offer_id': offer_id,
        'offer_digest': native_offer_digest(offer_id, runner_id,
            record['recipient'], record['price_wei'], record),
        'reference': ORDER_PUBLIC_BASE + '/offers/' + offer_id},
        sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(raw.encode()) > 1024:
        raise Failure('NATIVE_METADATA_TOO_LARGE', 500)
    return raw


def native_heartbeat_body(runner_id, record, offer_id):
    return {'runner_id': runner_id, 'runner_url': NATIVE_RUNNER_URL,
        'app': NATIVE_APP, 'capacity': 1, 'mode': NATIVE_MODE,
        'price_info': {'price': record['price_wei'], 'currency': 'wei', 'unit': 'fixed'},
        'metadata': native_runner_metadata(offer_id, runner_id, record),
        'status': 'ready'}


def native_post_heartbeat(body, secret):
    req = urllib.request.Request(NATIVE_NODE + '/runners/heartbeat',
        data=json.dumps(body, allow_nan=False).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': secret},
        method='POST')
    try:
        with urllib.request.build_opener(NoNativeRedirect).open(req, timeout=NATIVE_HEARTBEAT_TIMEOUT) as response:
            value = json.loads(response.read(65536))
    except HTTPError as e:
        code = e.code
        e.close()
        return code, {}
    except (OSError, ValueError):
        return None, {}
    if not isinstance(value, dict):
        return None, {}
    return 200, value


def parse_go_duration(text, default):
    try:
        if isinstance(text, str) and text.strip():
            total, number = 0.0, ''
            for ch in text.strip():
                if ch.isdigit() or ch == '.':
                    number += ch
                elif ch in ('h', 'm', 's') and number:
                    total += float(number) * {'h': 3600.0, 'm': 60.0, 's': 1.0}[ch]
                    number = ''
                else:
                    return default
            if number or total <= 0:
                return default
            return total
    except (ValueError, TypeError):
        pass
    return default


def native_heartbeat_once(runner_id):
    """One bounded heartbeat attempt for a runner record.

    Existing per-runner secret first; master secret only on 401/404 (or when
    no secret is stored yet, e.g. after a wrapper restart). A lost first
    response keeps retrying with the master secret and waits out the native
    TTL rather than changing identity or bypassing auth.
    """
    records = load(RUNNERS_FILE)
    record = records.get(runner_id)
    if not isinstance(record, dict):
        return 'unknown'
    secret = record.get('heartbeat_secret')
    code, resp = (None, {})
    if secret:
        code, resp = native_post_heartbeat(native_heartbeat_body(runner_id, record, record['offer_id']), secret)
    if code != 200 and (code in (401, 404) or not secret):
        try:
            master = native_master_secret()
        except Failure as error:
            record.update(ready=False, last_error=error.code, last_attempt=time.time(),
                next_heartbeat=time.time() + NATIVE_HEARTBEAT_RETRY_DELAY)
            save(RUNNERS_FILE, records)
            return 'pending'
        code, resp = native_post_heartbeat(native_heartbeat_body(runner_id, record, record['offer_id']), master)
    now = time.time()
    if code == 200 and resp.get('runner_id') != runner_id:
        code = None
    if code == 200:
        if isinstance(resp.get('heartbeat_secret'), str) and resp['heartbeat_secret']:
            record['heartbeat_secret'] = resp['heartbeat_secret']
        interval = parse_go_duration(resp.get('heartbeat_interval'), NATIVE_HEARTBEAT_DEFAULT_INTERVAL)
        ttl = parse_go_duration(resp.get('heartbeat_ttl'), 60.0)
        interval = min(interval, ttl / 2, 30.0)
        record.update(ready=True, last_error=None, last_attempt=now, expires_at=now+ttl,
            next_heartbeat=now + interval, interval_seconds=interval)
    else:
        record.update(ready=False,
            last_error='NATIVE_HEARTBEAT_HTTP_' + str(code) if code else 'NATIVE_HEARTBEAT_UNREACHABLE',
            last_attempt=now, next_heartbeat=now + NATIVE_HEARTBEAT_RETRY_DELAY)
    save(RUNNERS_FILE, records)
    return 'ready' if record['ready'] else 'pending'


def native_ensure_runner(offer_id, runner, recipient, price, offer):
    """Create or validate the persistent runner record for published terms."""
    records = load(RUNNERS_FILE)
    record = records.get(runner)
    if isinstance(record, dict):
        if (record.get('recipient') != recipient or record.get('price_wei') != price
                or record.get('offer_id') != offer_id
                or native_offer_digest(offer_id, runner, recipient, price, record) != native_offer_digest(offer_id, runner, recipient, price, offer)):
            raise Failure('NATIVE_RUNNER_TERMS_CONFLICT', 409)
        return record
    records[runner] = {'runner_url': NATIVE_RUNNER_URL, 'app': NATIVE_APP, 'mode': NATIVE_MODE,
        'price_wei': price, 'recipient': recipient, 'model': offer.get('model'),
        'input_rate': offer.get('input_rate'), 'output_rate': offer.get('output_rate'),
        'cache_rate': offer.get('cache_rate'), 'offer_id': offer_id,
        'allowance_credits': offer.get('allowance_credits'), 'window_seconds': offer.get('window_seconds'),
        'heartbeat_secret': None, 'ready': False, 'last_error': None,
        'last_attempt': 0.0, 'next_heartbeat': 0.0,
        'interval_seconds': NATIVE_HEARTBEAT_DEFAULT_INTERVAL,
        **({'strict_text_policy': offer['strict_text_policy']} if offer.get('strict_text_policy') else {}),
        **({'model_coverage': offer['model_coverage']} if offer.get('model_coverage') else {})}
    save(RUNNERS_FILE, records)
    return records[runner]


def native_offer_matches(stored, body):
    for key in ('allowance_credits', 'window_seconds', 'model', 'input_rate',
            'output_rate', 'cache_rate', 'native_runner_id',
            'native_recipient_address', 'price_wei', 'strict_text_policy',
            'model_coverage'):
        got = body.get(key)
        if key == 'native_recipient_address' and isinstance(got, str):
            got = got.lower()
        if got != stored.get(key):
            return False
    return True


def native_registration_view(runner_id):
    record = load(RUNNERS_FILE).get(runner_id)
    if not isinstance(record, dict):
        return {'state': 'unknown'}
    view = {'state': 'ready' if record.get('ready') and record.get('expires_at', 0) > time.time() else 'pending'}
    if record.get('last_error'):
        view['last_error'] = record['last_error']
    return view


def heartbeat_due_runners():
    """Serial-loop heartbeat driver. Same thread as request handling: no
    competing writers, no daemon. Unconfigured installations skip entirely."""
    if not native_registration_configured():
        return
    try:
        # Repair create-offer-before-runner-record interruption on the same identity.
        for oid, offer in load(OFFERS_FILE).items():
            if offer.get('native_runner_id'):
                runner, recipient, price = native_terms(offer)
                native_ensure_runner(oid, runner, recipient, price, offer)
        records = load(RUNNERS_FILE)
    except Failure:
        return
    now = time.time()
    for runner_id, record in records.items():
        if not isinstance(record, dict) or now < record.get('next_heartbeat', 0):
            continue
        try:
            native_heartbeat_once(runner_id)
        except Failure as error:
            try:
                current = load(RUNNERS_FILE)
                if isinstance(current.get(runner_id), dict):
                    current[runner_id].update(ready=False, last_error=error.code,
                        last_attempt=time.time(),
                        next_heartbeat=time.time() + NATIVE_HEARTBEAT_RETRY_DELAY)
                    save(RUNNERS_FILE, current)
            except Failure:
                pass
        except OSError:
            pass


def prepared_view(order_id, order, duplicate=False):
    """Public PREPARED readback. Never carries credentials or inference keys."""
    result = {k: order[k] for k in ['offer_id', 'model', 'allowance_credits',
        'window_seconds', 'buyer_user_id', 'payer_address', 'native_runner_id',
        'native_recipient_address', 'price_wei', 'expires_at', 'purchase_intent', 'state']}
    result.update(order_id=order_id, gateway=PUBLIC_BASE,
        duplicate=duplicate, **{k: order[k] for k in RATES})
    if order.get('model_coverage'):
        result['model_coverage'] = order['model_coverage']
    return result


class NoNativeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verified_native_purchase(order, headers):
    # Trust only the operator-configured node. Never fetch a caller-supplied
    # Livepeer-Session-Control URL or infer payment from forwarded paid headers.
    node = urlsplit(NATIVE_NODE)
    if (node.scheme not in ('http', 'https') or not node.netloc or node.username
            or node.password or node.query or node.fragment or node.path):
        raise Failure('NATIVE_NODE_NOT_CONFIGURED', 503)
    session_id = headers.get('Livepeer-Session-Id', '')
    token = headers.get('Livepeer-Session-Token', '')
    if not session_id or len(session_id) > 256 or not token or len(token) > 512:
        raise Failure('NATIVE_SESSION_REQUIRED', 403)
    url = NATIVE_NODE + '/runner/' + quote(order['native_runner_id'], safe='')
    url += '/session/' + quote(session_id, safe='') + '/purchase?'
    url += urlencode({'intent': order['purchase_intent'], 'payer': order['payer_address']})
    request = urllib.request.Request(url, headers={'Livepeer-Session-Token': token})
    try:
        with urllib.request.build_opener(NoNativeRedirect).open(request, timeout=10) as response:
            receipt = json.loads(response.read(16385))
    except (HTTPError, OSError, ValueError):
        raise Failure('NATIVE_PURCHASE_UNVERIFIED', 503)
    expected = dict(intent=order['purchase_intent'], runner_id=order['native_runner_id'],
        manifest_id=session_id, payer=order['payer_address'],
        recipient=order['native_recipient_address'].lower(),
        price_wei=str(order['price_wei']), state='ACCEPTED')
    if (not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items())
            or not re.fullmatch(r'0x[0-9a-fA-F]{64}', str(receipt.get('ticket_hash', '')))):
        raise Failure('NATIVE_PURCHASE_MISMATCH', 403)
    return dict(expected, ticket_hash=receipt['ticket_hash'].lower())


def complete_order(order_id, orders):
    order = orders[order_id]
    if order.get('state') == 'PREPARED':
        # Paid checkout orders never activate through the old free route.
        raise Failure('PREPARED_NOT_ACQUIRABLE', 409)
    account = get_account(order)
    if account is None:
        api('POST', '/api/user/', {'username': order['managed_username'],
            'password': order['managed_password'], 'display_name': order_id, 'role': 1}, token=admin())
        account = get_account(order)
    if account is None:
        raise Failure('ORDER_ACCOUNT_READBACK_FAILED')
    sess = owned_session(order)
    own = api('GET', '/api/user/self', token=sess)
    if own['id'] != account['id']:
        raise Failure('ORDER_ACCOUNT_MISMATCH')
    if order['state'] == 'PREPARING':
        # No key has been created or disclosed yet. Repeating this absolute grant
        # after a lost response cannot overwrite spent credits.
        if own.get('used_quota', 0) != 0:
            raise Failure('ORDER_ACCOUNT_ALREADY_USED')
        existing = item_list(api('GET', '/api/token/?p=1&page_size=100', token=sess))
        if existing:
            raise Failure('ORDER_ACCOUNT_UNEXPECTED_KEYS')
        api('POST', '/api/user/manage', {'id': account['id'], 'action': 'add_quota',
            'mode': 'override', 'value': order['allowance_credits']}, token=admin())
        funded = api('GET', '/api/user/self', token=sess)
        if funded.get('quota') != order['allowance_credits']:
            raise Failure('ORDER_GRANT_READBACK_FAILED')
        order['state'] = 'FUNDED'
        save(ORDERS_FILE, orders)
    if order['state'] == 'FUNDED':
        tokens = item_list(api('GET', '/api/token/?p=1&page_size=100', token=sess))
        named = [t for t in tokens if t.get('name') == order_id]
        if not named:
            api('POST', '/api/token/', {'name': order_id, 'remain_quota': order['allowance_credits'],
                'expired_time': order['expires_at'], 'model_limits_enabled': True,
                'model_limits': order['model'], 'unlimited_quota': False,
                **({'strict_text_policy': order['strict_text_policy']} if order.get('strict_text_policy') else {})}, token=sess)
            named = [t for t in item_list(api('GET', '/api/token/?p=1&page_size=100', token=sess))
                     if t.get('name') == order_id]
        if len(named) != 1:
            raise Failure('ORDER_KEY_AMBIGUOUS')
        if order.get('strict_text_policy') and named[0].get('strict_text_policy') != order['strict_text_policy']:
            raise Failure('ORDER_BILLING_POLICY_MISMATCH')
        key = api('POST', '/api/token/' + str(named[0]['id']) + '/key', {}, token=sess)
        if not isinstance(key, dict) or not key.get('key'):
            raise Failure('ORDER_KEY_READBACK_FAILED')
        order.update(token_id=named[0]['id'], order_key=key['key'], state='READY')
        save(ORDERS_FILE, orders)
    return order


def public_order(order_id, order, include_key=False):
    result = {k: order[k] for k in ['offer_id', 'model', 'expires_at', 'allowance_credits', 'state']}
    result.update(order_id=order_id, gateway=PUBLIC_BASE, **{k: order[k] for k in RATES})
    if order.get('active_model'):
        result.update(purchased_model=order['model'], model=order['active_model'])
    if order.get('strict_text_policy'):
        result['strict_text_policy'] = order['strict_text_policy']
    if order.get('model_coverage'):
        result['model_coverage'] = order['model_coverage']
    if order.get('purchase_intent'):
        result.update(purchase_intent=order['purchase_intent'], payer_address=order['payer_address'])
    if include_key:
        result['order_key'] = order['order_key']
    return result


def status(order_id, order):
    sess = owned_session(order)
    key = api('GET', '/api/token/' + str(order['token_id']), token=sess)
    user = api('GET', '/api/user/self', token=sess)
    result = public_order(order_id, order)
    result.update(remain_quota=min(key['remain_quota'], user['quota']),
        used_quota=key['used_quota'], token_status=key['status'])
    result['pending_reservations'] = key.get('strict_reservations', {'state': 'unavailable'})
    summary = key.get('strict_reservations')
    if summary is not None and 'settled_quota' in summary:
        # The native settlement record survives a crash between charging and
        # best-effort log publication. All counts are scoped to this order key.
        untracked = key['used_quota'] - summary['held_quota'] - summary['settled_quota']
        result.update(input_tokens=summary['input_tokens'], output_tokens=summary['output_tokens'],
            usage_source='native_settlement', settled_requests=summary['settled_count'],
            settled_quota=summary['settled_quota'], untracked_usage_quota=untracked,
            usage_complete=untracked == 0 and summary['unrecorded_settled_count'] == 0
                and summary['dispatched_count'] == 0)
        return result
    # Return actual usage records; never estimate completion tokens locally.
    logs = api('GET', '/api/log/self?p=1&page_size=100&type=2', token=sess)
    rows = item_list(logs)
    result.update(input_tokens=sum(x.get('prompt_tokens', 0) for x in rows),
        output_tokens=sum(x.get('completion_tokens', 0) for x in rows),
        usage_complete=logs.get('total', len(rows)) <= len(rows))
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = 'PunchOrderGateway/0.1'

    def log_message(self, *args):
        pass

    def send_json(self, status_code, value):
        body = json.dumps(value, allow_nan=False).encode()
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def run_request(self, method):
        request_id = secrets.token_hex(8)
        try:
            body = {}
            if method == 'POST':
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise Failure('INVALID_JSON', 400)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise Failure('INVALID_JSON', 400)
            with STATE_LOCK:
                result = self.dispatch(method, body)
            code, envelope = 200, {'success': True, 'data': result}
        except Failure as e:
            code, envelope = e.status, {'success': False, 'code': e.code}
        except (ValueError, KeyError, TypeError):
            code, envelope = 400, {'success': False, 'code': 'INVALID_REQUEST_OR_STATE'}
        except Exception:
            code, envelope = 503, {'success': False, 'code': 'SERVICE_UNAVAILABLE'}
        envelope['request_id'] = request_id
        print(json.dumps({'request_id': request_id, 'method': method,
            'status': code, 'code': envelope.get('code', 'OK')}), flush=True)
        self.send_json(code, envelope)

    def do_GET(self):
        self.run_request('GET')

    def do_POST(self):
        self.run_request('POST')

    def dispatch(self, method, body):
        if method == 'POST' and self.path in ('/maintenance/status', '/maintenance/acquire', '/maintenance/release'):
            if not hmac.compare_digest(str(body.get('seller_token', '')), SELLER_TOKEN):
                raise Failure('SELLER_AUTH_INVALID', 403)
            if MAINTENANCE is None:
                raise Failure('MAINTENANCE_UNAVAILABLE', 503)
            if self.path == '/maintenance/status':
                return MAINTENANCE.status()
            if self.path == '/maintenance/acquire':
                return MAINTENANCE.acquire(body.get('operation_id'), 'seller')
            return MAINTENANCE.release(body.get('operation_id'), 'seller')
        if (method == 'POST' and MAINTENANCE is not None and not MAINTENANCE.admission_open()
                and (self.path in ('/acquire', '/purchases/prepare', '/purchases/activate', '/offers')
                     or re.fullmatch(r'/orders/([A-Za-z0-9._-]+)/model', self.path))):
            raise Failure('MAINTENANCE_ACTIVE', 409)
        match = re.fullmatch(r'/orders/([A-Za-z0-9._-]+)/model', self.path)
        if match and method in ('GET', 'POST'):
            buyer = buyer_identity(self.headers.get('Authorization', '').removeprefix('Bearer '))
            order_id = match.group(1)
            order = load(ORDERS_FILE).get(order_id)
            if not order or order['buyer_user_id'] != buyer:
                raise Failure('ORDER_NOT_FOUND', 404)
            if MODELS is None:
                raise Failure('MODEL_SELECTION_UNAVAILABLE', 503)
            return MODELS.view(order_id, order) if method == 'GET' else MODELS.request(order_id, order, body)
        if (method == 'POST' and self.path in ('/acquire', '/purchases/prepare', '/purchases/activate')
                and MODELS is not None and MODELS.busy()):
            raise Failure('MODEL_OPERATION_BUSY', 409)
        if method == 'GET' and self.path == '/health':
            return {'ready': True, 'accounting': 'New API', 'model': MODEL}
        if method == 'GET' and self.path == '/offers':
            return [dict(v, offer_id=k) for k, v in load(OFFERS_FILE).items() if not v['withdrawn']]
        if method == 'GET' and self.path.startswith('/offers/'):
            oid = self.path[len('/offers/'):]
            offer = load(OFFERS_FILE).get(oid)
            if not offer:
                raise Failure('OFFER_NOT_FOUND', 404)
            return dict(offer, offer_id=oid)
        if method == 'GET' and self.path.startswith('/orders/'):
            auth = self.headers.get('Authorization', '')
            buyer = buyer_identity(auth.removeprefix('Bearer '))
            oid = self.path[len('/orders/'):]
            order = load(ORDERS_FILE).get(oid)
            if not order or order['buyer_user_id'] != buyer:
                raise Failure('ORDER_NOT_FOUND', 404)
            if order['state'] != 'READY':
                return public_order(oid, order)
            return status(oid, order)
        if method == 'POST' and self.path in ['/offers', '/offers/withdraw']:
            if not hmac.compare_digest(str(body.get('seller_token', '')), SELLER_TOKEN):
                raise Failure('SELLER_AUTH_INVALID', 403)
            offers = load(OFFERS_FILE)
            oid = body.get('offer_id', '')
            if not isinstance(oid, str) or not IDENTIFIER.fullmatch(oid):
                raise Failure('INVALID_OFFER', 400)
            if self.path == '/offers/withdraw':
                if oid not in offers:
                    raise Failure('OFFER_NOT_FOUND', 404)
                offers[oid]['withdrawn'] = True
                save(OFFERS_FILE, offers)
                return {'offer_id': oid, 'withdrawn': True}
            else:
                native_requested = CHECKOUT_LIVE or any(
                    k in body for k in ('native_runner_id', 'native_recipient_address', 'price_wei'))
                if oid in offers:
                    stored = offers[oid]
                    if stored.get('native_runner_id') and native_offer_matches(stored, body):
                        # Identical republish recovers the current registration
                        # state; terms and secrets are never rewritten to force it.
                        result = {'offer_id': oid, 'withdrawn': stored['withdrawn']}
                        if native_registration_configured():
                            runner, recipient, price = native_terms(stored)
                            native_ensure_runner(oid, runner, recipient, price, stored)
                            native_heartbeat_once(runner)
                            result['native_registration'] = native_registration_view(
                                stored['native_runner_id'])
                        return result
                    raise Failure('OFFER_EXISTS', 409)
                policy = strict_policy(body)
                coverage = model_coverage(body, policy)
                if DEFAULT_MODEL is not None:
                    from model_registry import RegistryError, validate_coverage
                    if policy is None or coverage is None:
                        raise Failure('MODEL_POLICY_REQUIRED', 400)
                    try:
                        validate_coverage(DEFAULT_MODEL['stats'], coverage)
                    except RegistryError as error:
                        raise Failure(error.code, 400)
                    profile = DEFAULT_MODEL['profile']
                    if (policy['context_tokens'] > profile['context_tokens'] or
                            policy['max_output_tokens'] > profile['max_output_tokens'] or
                            policy['reasoning_effort'] != profile['reasoning_effort']):
                        raise Failure('MODEL_PROFILE_OUTSIDE_DEFAULT', 400)

                allowance, window = body['allowance_credits'], body['window_seconds']
                if (type(allowance) is not int or not 0 < allowance <= 1000000
                    or type(window) is not int or not 0 < window <= 2592000
                    or body.get('model') != MODEL
                    or (policy is None and any(body.get(k) != v for k, v in RATES.items()))):
                    raise Failure('UNSUPPORTED_TRIAL_TERMS', 400)
                offers[oid] = dict(model=MODEL, allowance_credits=allowance,
                    window_seconds=window, withdrawn=False, **{k: body[k] for k in RATES})
                if policy is not None:
                    offers[oid]['strict_text_policy'] = policy
                if coverage is not None:
                    offers[oid]['model_coverage'] = coverage
                if native_requested:
                    runner, recipient, price = native_terms(body)
                    for other_id, other in offers.items():
                        if other_id != oid and other.get('native_runner_id') == runner:
                            raise Failure('NATIVE_RUNNER_TERMS_CONFLICT', 409)
                    offers[oid].update(native_runner_id=runner,
                        native_recipient_address=recipient, price_wei=price)
                save(OFFERS_FILE, offers)
                result = {'offer_id': oid, 'withdrawn': offers[oid]['withdrawn']}
                if native_requested and native_registration_configured():
                    native_ensure_runner(oid, runner, recipient, price, offers[oid])
                    try:
                        native_heartbeat_once(runner)
                    except Failure:
                        pass
                    result['native_registration'] = native_registration_view(runner)
                return result
        if method == 'GET' and self.path.startswith('/purchases/'):
            auth = self.headers.get('Authorization', '')
            buyer = buyer_identity(auth.removeprefix('Bearer '))
            oid = self.path[len('/purchases/'):]
            order = load(ORDERS_FILE).get(oid)
            if not order or order['buyer_user_id'] != buyer:
                raise Failure('ORDER_NOT_FOUND', 404)
            if order.get('state') == 'PREPARED':
                return prepared_view(oid, order)
            if order.get('state') == 'READY' and order.get('order_key'):
                return public_order(oid, order, include_key=True)
            return public_order(oid, order)
        if method == 'POST' and self.path == '/purchases/activate':
            buyer = buyer_identity(str(body.get('buyer_token', '')))
            oid = body.get('order_id')
            orders = load(ORDERS_FILE)
            order = orders.get(oid)
            if not order or order['buyer_user_id'] != buyer or not order.get('purchase_intent'):
                raise Failure('ORDER_NOT_FOUND', 404)
            receipt = verified_native_purchase(order, self.headers)
            if order.get('native_receipt') and order['native_receipt'] != receipt:
                raise Failure('NATIVE_PURCHASE_MISMATCH', 403)
            if order['state'] == 'PREPARED':
                order.update(native_receipt=receipt, state='PREPARING',
                    managed_username=oid, managed_password=secrets.token_urlsafe(24))
                save(ORDERS_FILE, orders)
            if order['state'] != 'READY':
                order = complete_order(oid, orders)
            return public_order(oid, order, include_key=True)
        if method == 'POST' and self.path == '/purchases/prepare':
            buyer = buyer_identity(str(body.get('buyer_token', '')))
            idem, offer_id, payer = (body.get('idempotency_key'),
                body.get('offer_id'), body.get('payer_address'))
            if not isinstance(idem, str) or not IDENTIFIER.fullmatch(idem):
                raise Failure('IDEMPOTENCY_REQUIRED', 400)
            if not isinstance(offer_id, str) or not offer_id:
                raise Failure('INVALID_OFFER', 400)
            if not isinstance(payer, str) or not PAYER_ADDRESS.fullmatch(payer):
                raise Failure('INVALID_PAYER_ADDRESS', 400)
            payer = payer.lower()
            order_id = 'ord-' + hashlib.sha256(f'{buyer}:{idem}'.encode()).hexdigest()[:16]
            orders = load(ORDERS_FILE)
            order = orders.get(order_id)
            if order is not None:
                if (not order.get('purchase_intent') or order['offer_id'] != offer_id
                        or order.get('payer_address') != payer):
                    raise Failure('IDEMPOTENCY_CONFLICT', 409)
                return prepared_view(order_id, order, duplicate=True)
            offer = load(OFFERS_FILE).get(offer_id)
            if not offer or offer.get('withdrawn'):
                raise Failure('OFFER_UNAVAILABLE', 410)
            if native_registration_configured() and offer.get('native_runner_id'):
                record = load(RUNNERS_FILE).get(offer['native_runner_id'])
                if native_registration_view(offer['native_runner_id'])['state'] != 'ready':
                    raise Failure('NATIVE_REGISTRATION_PENDING', 503)
            if MODELS is not None:
                MODELS.admit(offer['model'])
            runner, recipient, price = native_terms(offer)
            now = int(time.time())
            order = dict(offer, offer_id=offer_id, buyer_user_id=buyer,
                payer_address=payer, created_at=now,
                expires_at=now + offer['window_seconds'], state='PREPARED')
            terms = {k: order[k] for k in ['offer_id', 'model', 'allowance_credits',
                'window_seconds', 'input_rate', 'output_rate', 'cache_rate',
                'buyer_user_id', 'payer_address', 'native_runner_id',
                'native_recipient_address', 'price_wei', 'expires_at']}
            if order.get('strict_text_policy'):
                terms['strict_text_policy'] = order['strict_text_policy']
            if order.get('model_coverage'):
                terms['model_coverage'] = order['model_coverage']
            terms['order_id'] = order_id
            order['purchase_intent'] = purchase_intent(terms)
            orders[order_id] = order
            # Durable PREPARED snapshot before any native request; none made here.
            save(ORDERS_FILE, orders)
            return prepared_view(order_id, order)
        if method == 'POST' and self.path == '/acquire':
            if CHECKOUT_LIVE:
                raise Failure('FREE_ACQUIRE_DISABLED', 403)
            buyer = buyer_identity(str(body.get('buyer_token', '')))
            idem, offer_id = body.get('idempotency_key'), body.get('offer_id')
            if not isinstance(idem, str) or not IDENTIFIER.fullmatch(idem):
                raise Failure('IDEMPOTENCY_REQUIRED', 400)
            order_id = 'ord-' + hashlib.sha256(f'{buyer}:{idem}'.encode()).hexdigest()[:16]
            orders = load(ORDERS_FILE)
            order = orders.get(order_id)
            duplicate = order is not None
            if order is not None and order['offer_id'] != offer_id:
                raise Failure('IDEMPOTENCY_CONFLICT', 409)
            if order is None:
                offer = load(OFFERS_FILE).get(offer_id)
                if not offer or offer['withdrawn']:
                    raise Failure('OFFER_UNAVAILABLE', 410)
                if MODELS is not None:
                    MODELS.admit(offer['model'])
                now = int(time.time())
                order = dict(offer, offer_id=offer_id, buyer_user_id=buyer, created_at=now,
                    expires_at=now + offer['window_seconds'], state='PREPARING',
                    managed_username=order_id, managed_password=secrets.token_urlsafe(24))
                orders[order_id] = order
                save(ORDERS_FILE, orders)  # durable identity before any external grant
            if order['state'] != 'READY':
                if order['expires_at'] <= time.time():
                    raise Failure('ORDER_EXPIRED', 410)
                order = complete_order(order_id, orders)
            result = public_order(order_id, order, include_key=True)
            result['duplicate'] = duplicate
            return result
        raise Failure('NOT_FOUND', 404)


if __name__ == '__main__':
    from maintenance import Maintenance
    MAINTENANCE = Maintenance(sys.modules[__name__])
    if os.environ.get('GW_MODEL_ROOT') and os.environ.get('GW_MODEL_ROUTER'):
        from model_orders import ModelOrders
        MODELS = ModelOrders(sys.modules[__name__], os.environ['GW_MODEL_ROOT'],
            os.environ['GW_MODEL_ROUTER'], int(os.environ.get('GW_MODEL_GPU_LAYERS', '99')))
        MODELS.startup(MODEL)
    server = HTTPServer((BIND, PORT), Handler)
    server.timeout = 1
    while True:
        server.handle_request()
        with STATE_LOCK:
            heartbeat_due_runners()
        if MODELS is not None:
            MODELS.tick()
