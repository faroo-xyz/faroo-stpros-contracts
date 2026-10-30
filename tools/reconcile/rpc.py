"""Credential-blind read-only JSON-RPC with a persistent conservative CU ledger."""
import json
import copy
import uuid
import http.client
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from .storage import EvidenceError

WEIGHTS = {'eth_chainId': 20, 'eth_blockNumber': 20, 'eth_getBlockByNumber': 40,
           'eth_getCode': 80, 'eth_getStorageAt': 80, 'eth_getBalance': 40,
           'eth_call': 100, 'eth_getLogs': 150, 'eth_getTransactionReceipt': 100,
           'eth_getTransactionByHash': 100, 'debug_traceTransaction': 10000}


class BudgetExceeded(EvidenceError):
    pass


class RpcError(EvidenceError):
    def __init__(self, category):
        self.category = category
        super().__init__('RPC ' + category)  # Never include provider message or URL.


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RpcError('redirect_refused')


class Rpc:
    def __init__(self, store, budget=5_000_000, interval=0.5, transport=None, run_budget=100_000):
        self.store, self.budget, self.interval = store, budget, interval
        self.ledger = store.get('checkpoints/rpc-usage.json',
                                {'requests': 0, 'estimatedCU': 0, 'methods': {}, 'errors': {}, 'retries': 0})
        if not 0 < budget <= 5_000_000 or run_budget <= 0:
            raise EvidenceError('invalid RPC budget')
        self.run_budget = run_budget
        self.run_id = uuid.uuid4().hex
        self.run_start = copy.deepcopy(self.ledger)
        self.transport = transport
        self._url = None
        self.last = 0
        self._connection = None
        if transport is None:
            self._url = os.environ.get('PHAROS_RPC_URL')
            if not self._url:
                raise EvidenceError('PHAROS_RPC_URL is absent from the Codex process environment')
            try:
                u = urllib.parse.urlsplit(self._url)
                valid = u.scheme == 'https' and u.hostname and not u.username and not u.password and not u.fragment
            except ValueError:
                valid = False
            if not valid:
                raise EvidenceError('PHAROS_RPC_URL must be an HTTPS endpoint without userinfo or fragment')
            self._host, self._port = u.hostname, u.port
            self._path = urllib.parse.urlunsplit(('', '', u.path or '/', u.query, ''))

    def save(self):
        self.store.put('checkpoints/rpc-usage.json', self.ledger)
        run = self.run_usage()
        self.store.put('checkpoints/rpc-run.json', run)
        self.store.put('output/rpc-usage/runs/' + self.run_id + '.json', run)

    def run_usage(self):
        return {'id': self.run_id, 'ceilingCU': self.run_budget,
                'cumulativeCeilingCU': self.budget,
                'requests': self.ledger['requests'] - self.run_start['requests'],
                'estimatedCU': self.ledger['estimatedCU'] - self.run_start['estimatedCU'],
                'retries': self.ledger['retries'] - self.run_start['retries'],
                'cumulativeRequests': self.ledger['requests'],
                'cumulativeEstimatedCU': self.ledger['estimatedCU']}

    def check_budget(self, cost):
        if self.ledger['estimatedCU'] + cost > self.budget:
            self.save()
            raise BudgetExceeded('cumulative estimated CU ceiling reached; cache preserved')
        if self.run_usage()['estimatedCU'] + cost > self.run_budget:
            self.save()
            raise BudgetExceeded('per-run estimated CU ceiling reached; cache preserved')

    def call(self, method, params, retries=2):
        if method not in WEIGHTS:
            raise EvidenceError('non-allowlisted RPC method refused')
        for attempt in range(retries + 1):
            weight = WEIGHTS[method]
            self.check_budget(weight)
            time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            self.ledger['requests'] += 1
            self.ledger['estimatedCU'] += weight
            row = self.ledger['methods'].setdefault(method, {'requests': 0, 'estimatedCU': 0})
            row['requests'] += 1
            row['estimatedCU'] += weight
            if attempt:
                self.ledger['retries'] += 1
            self.save()  # Charge BEFORE sending, including crashes and failed calls.
            try:
                if self.transport:
                    return self.transport(method, params)
                raw = self._send({'jsonrpc': '2.0', 'id': self.ledger['requests'],
                                  'method': method, 'params': params})
                obj = json.loads(raw)
                if not isinstance(obj, dict) or obj.get('id') != self.ledger['requests']:
                    raise RpcError('invalid_response')
                if 'error' in obj:
                    code = obj['error'].get('code')
                    category = 'unsupported' if code == -32601 else 'provider_rejected'
                    if code in (429, -32005):
                        category = 'limit'
                    raise RpcError(category)
                if 'result' not in obj:
                    raise RpcError('invalid_response')
                return obj['result']
            except urllib.error.HTTPError as exc:
                category = 'throttled' if exc.code == 429 else ('temporary' if exc.code >= 500 else ('range_rejected' if exc.code in (400, 413) else 'http_rejected'))
            except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException):
                self._connection = None
                category = 'transport'
            except (ValueError, TypeError):
                category = 'invalid_response'
            except RpcError as exc:
                category = exc.category
            self.ledger['errors'][category] = self.ledger['errors'].get(category, 0) + 1
            self.save()
            # Log ranges are shrunk by caller; never blindly retry a rejected range.
            if category not in ('throttled', 'temporary', 'transport') or attempt == retries:
                raise RpcError(category) from None
            time.sleep(min(2 ** attempt, 4))

    def retry(self, method, params):
        self.ledger['retries'] += 1
        self.save()
        return self.call(method, params)

    def _send(self, payload):
        if self._connection is None:
            self._connection = http.client.HTTPSConnection(self._host, self._port, timeout=25)
        try:
            self._connection.request('POST', self._path, json.dumps(payload).encode(),
                                     {'Content-Type': 'application/json'})
            response = self._connection.getresponse()
            raw = response.read(16 * 1024 * 1024 + 1)
            if response.status != 200:
                category = ('throttled' if response.status == 429 else
                            'temporary' if response.status >= 500 else
                            'range_rejected' if response.status in (400, 413) else 'http_rejected')
                raise RpcError(category)
            if len(raw) > 16 * 1024 * 1024:
                self._connection.close()
                self._connection = None
                raise RpcError('response_too_large')
            return raw
        except (OSError, http.client.HTTPException):
            if self._connection:
                self._connection.close()
            self._connection = None
            raise RpcError('transport') from None

    def batch(self, requests):
        """Bounded read batch. Every element is charged; batching saves latency only."""
        if not 1 <= len(requests) <= 10 or any(m not in WEIGHTS for m, p in requests):
            raise EvidenceError('invalid read-only batch')
        cost = sum(WEIGHTS[m] for m, p in requests)
        self.check_budget(cost)
        time.sleep(max(0, len(requests) * self.interval - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        payload = []
        for method, params in requests:
            self.ledger['requests'] += 1
            self.ledger['estimatedCU'] += WEIGHTS[method]
            row = self.ledger['methods'].setdefault(method, {'requests': 0, 'estimatedCU': 0})
            row['requests'] += 1
            row['estimatedCU'] += WEIGHTS[method]
            payload.append(dict(jsonrpc='2.0', id=self.ledger['requests'], method=method, params=params))
        self.save()
        try:
            if self.transport:
                return [self.transport(m, p) for m, p in requests]
            result = json.loads(self._send(payload))
            if not isinstance(result, list) or len(result) != len(payload):
                raise RpcError('invalid_batch_response')
            by_id = {r['id']: r for r in result}
            if len(by_id) != len(payload):
                raise RpcError('duplicate_batch_id')
            output = []
            for request in payload:
                r = by_id[request['id']]
                if 'error' in r:
                    self.ledger['errors']['batch_item_rejected'] = self.ledger['errors'].get('batch_item_rejected', 0) + 1
                    output.append(RpcError('provider_rejected'))
                else:
                    output.append(r['result'])
            self.save()
            return output
        except (RpcError, ValueError, KeyError, TypeError, OSError, http.client.HTTPException):
            self.ledger['errors']['batch_rejected'] = self.ledger['errors'].get('batch_rejected', 0) + len(requests)
            self.save()
            raise RpcError('batch_rejected') from None


def load_project_rpc_override(project):
    """Explicit owner-authorized exception: extract only PHAROS_RPC_URL, never source/eval.

    Not used by default. This deliberately does not call dotenv or inspect any other
    configuration path. Values must be literal; shell expansion is rejected.
    """
    import re
    import shlex
    value = None
    try:
        path = project / '.env'
        if path.is_symlink():
            raise EvidenceError('project RPC override refuses symlinked environment file')
        with path.open() as stream:
            for line in stream:
                match = re.match(r'^\s*(?:export\s+)?PHAROS_RPC_URL\s*=\s*(.*?)\s*$', line)
                if not match:
                    continue
                parts = shlex.split(match.group(1), comments=True, posix=True)
                if len(parts) != 1 or '${' in parts[0] or '$(' in parts[0] or '`' in parts[0]:
                    raise EvidenceError('PHAROS_RPC_URL must be literal; no shell expansion executed')
                if value is not None:
                    raise EvidenceError('duplicate PHAROS_RPC_URL definitions refused')
                value = parts[0]
    except (OSError, ValueError):
        raise EvidenceError('unable to extract the explicitly authorized project RPC variable') from None
    if not value:
        raise EvidenceError('requested project RPC variable is absent or empty')
    os.environ['PHAROS_RPC_URL'] = value
