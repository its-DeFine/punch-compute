"""One seller's durable model-selection operations; New API owns all balances.

The wrapper's existing state lock serializes order admission and these records.
Downloads/runtime work run outside that lock. Only one model operation runs at
once; a switch that would disrupt another usable order is refused.
"""
import copy
import hashlib
from pathlib import Path
import threading
import time

from model_registry import RegistryError, prepare_registry_model
from model_runtime import Router, add_model_preset, ModelRuntimeError
from newapi_models import (ensure_model_channel, read_token, disable_token,
                          cas_token_model, enable_token, check_token_steady)

TERMINAL = {'READY', 'FAILED', 'UNAVAILABLE'}


class ModelOrders:
    def __init__(self, gateway, model_root, router_url, gpu_layers):
        self.g = gateway
        self.root = Path(model_root).resolve()
        self.router_url = router_url
        self.router = Router(router_url)
        self.gpu_layers = gpu_layers
        self.file = str(Path(gateway.DATA) / 'model_operations.json')
        self.worker = None

    def startup(self, default_model):
        # Before the HTTP server admits new management requests, restore the
        # retained serving model. Pending changes use their own recovery path.
        if self.busy():
            return
        active = {order.get('active_model', order['model'])
                  for order in self.g.load(self.g.ORDERS_FILE).values()
                  if order['state'] in ('READY', 'FUNDED', 'PREPARING', 'PREPARED')
                  and order['expires_at'] > time.time()}
        if len(active) > 1:
            raise self.g.Failure('MODEL_CAPACITY_BUSY', 409)
        self.router.load(next(iter(active), default_model))

    def admit(self, model):
        # Called under the gateway state lock before any purchase/grant writes.
        # Prepared purchases reserve the same capacity while signing is pending.
        if self.busy():
            raise self.g.Failure('MODEL_OPERATION_BUSY', 409)
        if any(order['state'] in ('READY', 'FUNDED', 'PREPARING', 'PREPARED')
               and order['expires_at'] > time.time()
               and order.get('active_model', order['model']) != model
               for order in self.g.load(self.g.ORDERS_FILE).values()):
            raise self.g.Failure('MODEL_CAPACITY_BUSY', 409)
        # The previous buyer may have selected a different model. Expired
        # orders no longer reserve capacity, but their admitted requests must
        # drain before we unload the previous model to serve this new order.
        models = self.router.models()
        if model not in models:
            raise self.g.Failure('MODEL_ROUTER_NOT_READY', 503)
        if models[model]['status']['value'] == 'loaded':
            return
        for order in self.g.load(self.g.ORDERS_FILE).values():
            previous = order.get('active_model', order['model'])
            if previous == model or not order.get('token_id'):
                continue
            if not order.get('strict_text_policy') or order['expires_at'] > time.time():
                raise self.g.Failure('MODEL_CAPACITY_BUSY', 409)
            disable_token(self.g.api, self.session(order), order['token_id'])
        for previous, info in models.items():
            if previous != model and info['status']['value'] != 'unloaded':
                self.router.unload(previous)
        self.router.load(model)

    def busy(self):
        return any(op['state'] != 'FAILED' and op['state'] != 'READY'
                   for op in self.g.load(self.file).values())

    def view(self, order_id, order):
        ops = self.g.load(self.file)
        op = ops.get(order.get('model_operation'))
        public = None if op is None else {k: copy.deepcopy(op[k]) for k in
            ('operation_id', 'state', 'selection', 'target_model', 'error', 'restored', 'recovery_error', 'recovery_attempts', 'updated_at') if k in op}
        return {'order_id': order_id, 'active_model': order.get('active_model', order['model']),
                'operation': public}

    def request(self, order_id, order, body):
        g = self.g
        idem = body.get('idempotency_key')
        if not isinstance(idem, str) or not g.IDENTIFIER.fullmatch(idem):
            raise g.Failure('IDEMPOTENCY_REQUIRED', 400)
        selection = {k: body.get(k) for k in ('registry', 'repository', 'revision', 'file')}
        if any(not isinstance(v, str) or not v or len(v) > 512 for v in selection.values()):
            raise g.Failure('INVALID_SELECTION', 400)
        operation_id = 'model-' + hashlib.sha256((order_id + ':' + idem).encode()).hexdigest()
        ops = g.load(self.file)
        if operation_id in ops:
            if ops[operation_id]['selection'] != selection:
                raise g.Failure('IDEMPOTENCY_CONFLICT', 409)
            if ops[operation_id]['state'] == 'UNAVAILABLE':
                # Same operation retries restoration only, never a second switch.
                self.phase(operation_id, 'RESTORING', recovery_attempts=0)
            old = dict(order, model_operation=operation_id)
            return self.view(order_id, old)
        if order['state'] != 'READY' or order['expires_at'] <= time.time():
            raise g.Failure('ORDER_NOT_USABLE', 409)
        if not order.get('model_coverage') or not order.get('strict_text_policy'):
            raise g.Failure('MODEL_SELECTION_NOT_IN_OFFER', 409)
        if self.busy():
            raise g.Failure('MODEL_OPERATION_BUSY', 409)
        orders = g.load(g.ORDERS_FILE)
        # Initial single-GPU policy: never evict another purchased order's model.
        if any(key != order_id and item['state'] in ('READY', 'FUNDED', 'PREPARING', 'PREPARED')
               and item['expires_at'] > time.time() for key, item in orders.items()):
            raise g.Failure('MODEL_CAPACITY_BUSY', 409)
        previous = order.get('active_model', order['model'])
        op = {'operation_id': operation_id, 'order_id': order_id, 'selection': selection,
              'previous_model': previous, 'state': 'QUEUED', 'updated_at': int(time.time())}
        ops[operation_id] = op
        g.save(self.file, ops)
        # Recoverable if process stops between these two writes: tick uses op.order_id.
        orders[order_id]['model_operation'] = operation_id
        g.save(g.ORDERS_FILE, orders)
        return self.view(order_id, orders[order_id])

    def phase(self, op_id, state, **fields):
        with self.g.STATE_LOCK:
            ops = self.g.load(self.file)
            op = ops[op_id]
            op.update(fields, state=state, updated_at=int(time.time()))
            self.g.save(self.file, ops)
            return copy.deepcopy(op)

    def tick(self):
        with self.g.STATE_LOCK:
            if self.worker is not None and self.worker.is_alive():
                return
            pending = [op for op in self.g.load(self.file).values()
                       if op['state'] not in TERMINAL or
                       (op['state'] == 'UNAVAILABLE' and op.get('recovery_attempts', 0) < 3
                        and op.get('next_recovery_at', 0) <= time.time())]
            if not pending:
                return
            op = pending[0]
            orders = self.g.load(self.g.ORDERS_FILE)
            if orders[op['order_id']].get('model_operation') != op['operation_id']:
                orders[op['order_id']]['model_operation'] = op['operation_id']
                self.g.save(self.g.ORDERS_FILE, orders)
            self.worker = threading.Thread(target=self.run, args=(op['operation_id'],), daemon=True)
            self.worker.start()

    def session(self, order):
        with self.g.STATE_LOCK:
            return self.g.owned_session(order)

    def validate_current(self, op, require_usable=True):
        with self.g.STATE_LOCK:
            orders = self.g.load(self.g.ORDERS_FILE)
            order = copy.deepcopy(orders[op['order_id']])
            if require_usable and (order['state'] != 'READY' or order['expires_at'] <= time.time()):
                raise self.g.Failure('ORDER_NOT_USABLE', 409)
            if any(key != op['order_id'] and item['state'] in ('READY', 'FUNDED', 'PREPARING', 'PREPARED')
                   and item['expires_at'] > time.time() for key, item in orders.items()):
                raise self.g.Failure('MODEL_CAPACITY_BUSY', 409)
            return order

    def drain_expired(self, op):
        # Expiry blocks new admission, but an already admitted request can remain.
        # Drain those orders before evicting a shared model, too.
        with self.g.STATE_LOCK:
            others = [copy.deepcopy(item) for key, item in self.g.load(self.g.ORDERS_FILE).items()
                      if key != op['order_id'] and item.get('token_id')
                      and item.get('active_model', item['model']) in
                          (op['previous_model'], op.get('target_model'))]
        for item in others:
            if item['expires_at'] > time.time() or not item.get('strict_text_policy'):
                raise self.g.Failure('MODEL_CAPACITY_BUSY', 409)
            disable_token(self.g.api, self.session(item), item['token_id'])

    def restore(self, op_id, detail):
        g = self.g
        op = self.phase(op_id, 'RESTORING', error=detail)
        try:
            order = self.validate_current(op, require_usable=False)
            before = disable_token(g.api, self.session(order), order['token_id'])
            previous, target = op['previous_model'], op.get('target_model')
            if (before.get('strict_text_policy') != order['strict_text_policy'] or
                    before.get('model_limits') not in (previous, target)):
                raise g.Failure('MODEL_ACCOUNTING_BINDING_CHANGED', 409)
            self.drain_expired(op)
            if target and target != previous:
                self.router.unload(target)
            self.router.load(previous)
            sess = self.session(order)
            if before['model_limits'] != previous:
                cas_token_model(g.api, sess, order['token_id'], target, previous)
            check_token_steady(read_token(g.api, sess, order['token_id']), dict(before, model_limits=previous))
            if order['expires_at'] > time.time() and order['state'] == 'READY':
                enable_token(g.api, sess, order['token_id'])
            with g.STATE_LOCK:
                orders = g.load(g.ORDERS_FILE)
                orders[op['order_id']].update(active_model=previous, model_operation=op_id)
                g.save(g.ORDERS_FILE, orders)
                self.phase(op_id, 'FAILED', restored=True, error=detail)
        except Exception as error:
            attempts = op.get('recovery_attempts', 0) + 1
            self.phase(op_id, 'UNAVAILABLE', error=detail, restored=False,
                       recovery_error={'code': getattr(error, 'code', 'MODEL_RESTORATION_FAILED')},
                       recovery_attempts=attempts, next_recovery_at=int(time.time()) + 10 * attempts)

    def run(self, op_id):
        g = self.g
        try:
            with g.STATE_LOCK:
                op = copy.deepcopy(g.load(self.file)[op_id])
            # Recovery never depends on fetching the failed replacement again.
            if op.get('runtime_touched') or op['state'] in ('DRAINING', 'LOADING', 'COMMITTING', 'RESTORING', 'UNAVAILABLE'):
                self.restore(op_id, op.get('error', {'code': 'MODEL_OPERATION_INTERRUPTED'}))
                return
            order = self.validate_current(op)
            previous = op['previous_model']
            if previous not in self.router.models():
                raise g.Failure('MODEL_ROUTER_NOT_READY', 503)
            selection = op.get('identity', op['selection'])
            selection = {k: selection[k] for k in ('registry', 'repository', 'revision', 'file')}
            self.phase(op_id, 'DOWNLOADING')
            prepared = prepare_registry_model(selection, order['model_coverage'], self.root / 'cache')
            if op.get('identity') and prepared['identity'] != op['identity']:
                raise g.Failure('MODEL_ARTIFACT_CHANGED', 409)
            policy = order['strict_text_policy']
            if prepared['stats']['context_length'] < policy['context_tokens']:
                raise RegistryError('MODEL_OUTSIDE_OFFER', 'context below purchased request limit',
                                    'context_tokens', policy['context_tokens'], prepared['stats']['context_length'])
            profile = {'context_tokens': policy['context_tokens'], 'max_output_tokens': policy['max_output_tokens'],
                       'reasoning_effort': policy['reasoning_effort'], 'parallel': 1, 'gpu_layers': self.gpu_layers}
            target = add_model_preset(self.root, prepared['path'], prepared['identity']['sha256'], profile)
            self.phase(op_id, 'VERIFIED', identity=prepared['identity'], target_model=target)
            order = self.validate_current(op)
            with g.STATE_LOCK:
                administrator = g.admin()
            ensure_model_channel(g.api, administrator, name=target, router_url=self.router_url, model=target)
            order = self.validate_current(op)
            op = self.phase(op_id, 'DRAINING', runtime_touched=True)
            before = disable_token(g.api, self.session(order), order['token_id'])
            if before.get('strict_text_policy') != policy or before.get('model_limits') not in (previous, target):
                raise g.Failure('MODEL_ACCOUNTING_BINDING_CHANGED', 409)
            self.drain_expired(op)
            self.phase(op_id, 'LOADING')
            if previous != target:
                self.router.unload(previous)
            self.router.load(target)
            self.phase(op_id, 'COMMITTING')
            sess = self.session(order)
            if before['model_limits'] != target:
                cas_token_model(g.api, sess, order['token_id'], before['model_limits'], target)
            check_token_steady(read_token(g.api, sess, order['token_id']), dict(before, model_limits=target))
            if order['expires_at'] > time.time():
                enable_token(g.api, sess, order['token_id'])
            with g.STATE_LOCK:
                orders = g.load(g.ORDERS_FILE)
                orders[op['order_id']].update(active_model=target, model_operation=op_id)
                g.save(g.ORDERS_FILE, orders)
                self.phase(op_id, 'READY')
        except Exception as error:
            detail = error.to_dict() if isinstance(error, (RegistryError, ModelRuntimeError)) else {'code': getattr(error, 'code', 'MODEL_OPERATION_FAILED')}
            with g.STATE_LOCK:
                current = g.load(self.file)[op_id]
            if current.get('runtime_touched'):
                self.restore(op_id, detail)
            else:
                self.phase(op_id, 'FAILED', error=detail)
