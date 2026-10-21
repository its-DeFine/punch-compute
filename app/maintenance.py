"""Persisted seller maintenance gate for update/migration windows.

Smallest coordination the updates design needs: close buyer admission first,
prove no usable order, unsafe operation, model activity, or native
reservation hold remains, then hold the gate until the owning operation
releases it. No Docker, no service control, no balance/key/quota/history
writes here. The only native write used is the existing status-only disable
(via newapi_models) to drain expired or exhausted orders; a disable never
touches balances.

All entry points assume the caller holds the gateway STATE_LOCK (the same
discipline as ModelOrders) and take the gateway module namespace: DATA,
ORDERS_FILE, load/save, Failure, IDENTIFIER, api(), owned_session().
Root wires admission checks into dispatch and owns status routes; this file
only decides and persists. Restarts keep a closed admission until the owning
operation explicitly releases it; nothing here reopens automatically.
"""
import copy
import math
import os
import time

from newapi_models import TOKEN_STATUS_DISABLED, disable_token

# Native token statuses, read-only reference to common/constants.go in the
# native tree (newapi_models only exports enabled/disabled).
TOKEN_STATUS_EXPIRED = 3
TOKEN_STATUS_EXHAUSTED = 4

# Order states that may still move money or grants: never a quiet window.
UNSAFE_ORDER_STATES = ('PREPARED', 'PREPARING', 'FUNDED')

STATE_FILE = 'maintenance.json'




class Maintenance:
    def __init__(self, gateway):
        self.g = gateway
        self.file = os.path.join(gateway.DATA, STATE_FILE)

    def _blank(self):
        return {'admission': 'open', 'operation': None}

    def _load(self):
        g = self.g
        if not os.path.exists(self.file):
            return self._blank()
        try:
            state = g.load(self.file)
        except FileNotFoundError:
            return self._blank()
        except ValueError:
            raise g.Failure('MAINTENANCE_STATE_UNREADABLE')
        if (not isinstance(state, dict) or state.get('admission') not in ('open', 'closed')
                or ('operation' in state and not isinstance(state['operation'], (dict, type(None))))):
            raise g.Failure('MAINTENANCE_STATE_INVALID')
        return {'admission': state['admission'], 'operation': state.get('operation')}

    def _save(self, state):
        self.g.save(self.file, state)

    def _view(self, state, blockers=None):
        view = {'admission': state['admission'],
                'operation': copy.deepcopy(state['operation'])}
        if blockers is not None:
            view['blockers'] = blockers
        return view

    def admission_open(self):
        """Dispatch predicate. True only when no gate is held."""
        return self._load()['admission'] == 'open'

    def status(self):
        """Live read-only view. Native reads that fail surface as blockers
        (fail closed); nothing is written, nothing drained."""
        state = self._load()
        blockers, _ = self._evaluate(drains=False)
        return self._view(state, blockers)

    def acquire(self, operation_id, owner, kind='update'):
        """Close admission first, then prove the window is quiet. Idempotent
        replay of the same id/owner/kind returns the stored view. A held gate
        refuses strangers; the current released id cannot reactivate."""
        g = self.g
        if not isinstance(operation_id, str) or not g.IDENTIFIER.fullmatch(operation_id):
            raise g.Failure('OPERATION_ID_INVALID')
        if not isinstance(owner, str) or not owner:
            raise g.Failure('OPERATION_OWNER_REQUIRED')
        if not isinstance(kind, str) or not kind:
            raise g.Failure('OPERATION_KIND_REQUIRED')
        state = self._load()
        current = state['operation']
        if current is not None and current.get('id') == operation_id:
            if current.get('owner') != owner:
                raise g.Failure('OPERATION_OWNER_MISMATCH')
            if current.get('kind') != kind:
                raise g.Failure('OPERATION_CONFLICT')
            if current.get('state') == 'HELD':
                return self._view(state, [])
            if current.get('state') == 'RELEASED':
                raise g.Failure('OPERATION_RETIRED')
        elif current is not None and current.get('state') == 'HELD':
            raise g.Failure('OPERATION_BUSY')
        record = {'id': operation_id, 'owner': owner, 'kind': kind,
                  'state': 'CHECKING', 'detail': {}, 'updated_at': int(time.time())}
        previous = None
        if current is not None and current.get('id') != operation_id:
            previous = current
        state.update(admission='closed', operation=record)
        if previous is not None:
            state['previous_operation'] = previous
        self._save(state)
        blockers, drained = self._evaluate(drains=True)
        if blockers:
            record.update(state='REFUSED', detail={'blockers': blockers, 'drained': drained},
                          updated_at=int(time.time()))
        else:
            record.update(state='HELD', detail={'blockers': [], 'drained': drained},
                          updated_at=int(time.time()))
        self._save(state)
        return self._view(state, blockers)

    def release(self, operation_id, owner):
        """Open admission only for the owning operation. The record is
        retained (never deleted) so restarts and audits keep the evidence."""
        g = self.g
        state = self._load()
        current = state['operation']
        if current is None or current.get('id') != operation_id:
            raise g.Failure('NO_OPERATION')
        if current.get('owner') != owner:
            raise g.Failure('OPERATION_OWNER_MISMATCH')
        current.update(state='RELEASED', updated_at=int(time.time()))
        state['admission'] = 'open'
        self._save(state)
        return self._view(state)

    def _evaluate(self, drains):
        """Collect blockers. With drains=True, expired/exhausted token orders
        are status-only disabled first (safe, balance-preserving); every
        native failure becomes a blocker, never a pass."""
        g = self.g
        blockers, drained = [], []
        try:
            orders = g.load(g.ORDERS_FILE)
        except FileNotFoundError:
            return [{'code': 'ORDERS_UNAVAILABLE'}], drained
        if not isinstance(orders, dict):
            return [{'code': 'ORDERS_UNAVAILABLE'}], drained
        now = time.time()
        for order_id, order in orders.items():
            if not isinstance(order, dict):
                blockers.append({'code': 'ORDER_UNREADABLE', 'order_id': order_id})
                continue
            state = order.get('state')
            if type(order.get('expires_at')) not in (int, float) or not math.isfinite(order['expires_at']):
                blockers.append({'code': 'ORDER_UNREADABLE', 'order_id': order_id})
            if state == 'READY' and not order.get('token_id'):
                blockers.append({'code': 'ORDER_UNREADABLE', 'order_id': order_id})
            if state not in ('READY',) + UNSAFE_ORDER_STATES:
                blockers.append({'code': 'ORDER_UNCERTAIN', 'order_id': order_id, 'state': state})
            if state in UNSAFE_ORDER_STATES:
                blockers.append({'code': 'UNSAFE_ORDER_STATE', 'order_id': order_id, 'state': state})
            if order.get('native_receipt') and state != 'READY':
                blockers.append({'code': 'ORDER_UNCERTAIN', 'order_id': order_id, 'state': state})
        try:
            operations = g.load(os.path.join(g.DATA, 'model_operations.json'))
        except FileNotFoundError:
            operations = {}
        except Exception:
            return blockers + [{'code': 'MODEL_OPERATIONS_UNREADABLE'}], drained
        if not isinstance(operations, dict):
            return blockers + [{'code': 'MODEL_OPERATIONS_UNREADABLE'}], drained
        for op_id, op in operations.items():
            if not isinstance(op, dict):
                blockers.append({'code': 'MODEL_OPERATIONS_UNREADABLE'})
                continue
            op_state = op.get('state')
            if op_state not in ('READY', 'FAILED'):
                blockers.append({'code': 'MODEL_OPERATION_UNCERTAIN' if op_state == 'UNAVAILABLE' else 'MODEL_OPERATION_ACTIVE', 'operation_id': op_id})
            elif op_state == 'FAILED' and op.get('runtime_touched') and not op.get('restored'):
                blockers.append({'code': 'MODEL_OPERATION_UNCERTAIN', 'operation_id': op_id})
        for order_id, order in orders.items():
            if (not isinstance(order, dict) or not order.get('token_id')
                    or type(order.get('expires_at')) not in (int, float)
                    or not math.isfinite(order['expires_at'])):
                continue
            token, problem = self._read_token(order_id, order)
            if problem is not None:
                blockers.append(problem)
                continue
            if order.get('strict_text_policy') and token.get('strict_text_policy') != order['strict_text_policy']:
                blockers.append({'code': 'NATIVE_POLICY_MISMATCH', 'order_id': order_id})
                continue
            if order.get('state') == 'READY' and not self._drainable(order, token, now):
                blockers.append({'code': 'USABLE_ORDER', 'order_id': order_id})
            if self._holds(token, order_id, blockers):
                continue
            if drains and self._drainable(order, token, now):
                try:
                    disable_token(g.api, g.owned_session(order), order['token_id'])
                    drained.append(order_id)
                except Exception as error:
                    blockers.append({'code': 'DRAIN_FAILED', 'order_id': order_id,
                                     'detail': getattr(error, 'code', type(error).__name__)})
        return blockers, drained

    def _read_token(self, order_id, order):
        """Authoritative token readback through the existing api callable.
        Returns (token, None) or (None, blocker)."""
        g = self.g
        try:
            session = g.owned_session(order)
            token = g.api('GET', '/api/token/%d' % order['token_id'], token=session)
        except Exception as error:
            return None, {'code': 'NATIVE_UNAVAILABLE', 'order_id': order_id,
                          'detail': getattr(error, 'code', type(error).__name__)}
        if not isinstance(token, dict) or token.get('id') != order['token_id']:
            return None, {'code': 'NATIVE_UNAVAILABLE', 'detail': 'TOKEN_READBACK_AMBIGUOUS'}
        return token, None

    def _holds(self, token, order_id, blockers):
        """True when the token carries unresolved native reservation holds.
        A policy token without its summary fails closed."""
        summary = token.get('strict_reservations')
        fields = ('reserved_count', 'dispatched_count', 'held_quota')
        if not isinstance(summary, dict) or any(type(summary.get(k)) is not int or summary[k] < 0 for k in fields):
            blockers.append({'code': 'NATIVE_SUMMARY_UNAVAILABLE', 'order_id': order_id})
            return True
        if summary.get('reserved_count', 0) or summary.get('dispatched_count', 0) or summary.get('held_quota', 0):
            blockers.append({'code': 'RESERVATION_HOLD', 'order_id': order_id,
                             'detail': {k: summary.get(k) for k in
                                        ('reserved_count', 'dispatched_count', 'held_quota')}})
            return True
        return False

    def _drainable(self, order, token, now):
        expired = order['expires_at'] <= now
        exhausted = (token.get('status') == TOKEN_STATUS_EXHAUSTED
                     or (not token.get('unlimited_quota', False)
                         and isinstance(token.get('remain_quota'), int) and token['remain_quota'] <= 0))
        return expired or exhausted
