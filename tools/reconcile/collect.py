"""Pinned-snapshot collection. No transactions, signing, wallets or generic RPC entry point."""
from tools.lib import abi
from tools.lib.keccak import keccak256
from .inventory import CORE, IMPL_SLOT, STAKING, ZERO, number, scopes, decode, UPGRADE
from .storage import EvidenceError, gaps, ordered
from .rpc import RpcError, BudgetExceeded, WEIGHTS
from . import incremental


def transaction_requirements(inv, logs, boundary=-1):
    """Required traces cover payment/reward movements; other transactions are optional."""
    from .external import STAKING_ABI
    names = {'WithdrawalCompleted', 'RewardClaimed', 'CoinMinted', 'EthDeposited',
             'OracleNotified', 'TransferredToRewardsVault', 'TokenAmountUpdated'}
    items = STAKING_ABI + [item for target in inv['targets'].values() for item in target.get('abi', [])]
    topics = {abi.event_topic(abi.item_signature(i)) for i in items
              if i.get('type') == 'event' and i.get('name') in names}
    # Scheduling hints are conservative even when an external ABI is unverified.
    topics.update(abi.event_topic(sig) for sig in (
        'WithdrawalCompleted(address,address,uint256)', 'EthDeposited(address,uint256)',
        'OracleNotified(address,uint256)', 'TransferredToRewardsVault(address,uint256)',
        'TokenAmountUpdated(address,uint256,uint256)'))
    grouped = {}
    for log in logs:
        grouped.setdefault(log['transactionHash'].lower(), []).append(log)
    pending = {tx for tx, rows in grouped.items() if max(number(l['blockNumber']) for l in rows) > boundary}
    required = {tx for tx in pending if tx == inv['release']['upgradeTransaction'].lower() or
                any(l['topics'] and l['topics'][0] in topics for l in grouped[tx])}
    return grouped, pending, required


def usable_trace(trace):
    return isinstance(trace, dict) and bool(trace.get('type')) and not trace.get('error')


def transaction_readiness(store, inv, logs, snapshot, boundary=-1):
    """Recompute the completion gate offline; never trust a stale coverage checkpoint."""
    grouped, pending, required = transaction_requirements(inv, logs, boundary)
    missing_receipts, missing_traces = [], []
    headers = store.get('cache/snapshots/headers.json', {})
    for tx in sorted(pending):
        rows = grouped[tx]
        receipt = store.get('cache/receipts/' + tx + '.json')
        detail = store.get('cache/receipts/' + tx + '-transaction.json')
        expected = rows[0]['blockHash']
        if headers.get(rows[0]['blockNumber'], {}).get('hash') != expected:
            raise EvidenceError('transaction event/header mismatch')
        if receipt is not None and (receipt.get('blockHash') != expected or number(receipt.get('status', 0)) != 1):
            raise EvidenceError('receipt inconsistent with pinned successful logs')
        if receipt is not None:
            for row in rows:
                matches = [r for r in receipt.get('logs', []) if number(r['logIndex']) == number(row['logIndex'])]
                if len(matches) != 1 or any(matches[0][k] != row[k] for k in ('address', 'topics', 'data', 'blockHash', 'transactionHash')):
                    raise EvidenceError('receipt/log mismatch')
        if detail is not None and detail.get('blockHash') != expected:
            raise EvidenceError('transaction details differ from pinned block')
        if receipt is None or detail is None:
            missing_receipts.append(tx)
        if tx in required and (receipt is None or not usable_trace(store.get('cache/traces/' + tx + '.json'))):
            missing_traces.append(tx)
    return {'snapshot': snapshot, 'missingReceipts': missing_receipts, 'missingRequiredTraces': missing_traces,
            'requiredEvidenceComplete': not missing_receipts and not missing_traces}


class Collector:
    def __init__(self, store, rpc, inv):
        self.store, self.rpc, self.inv = store, rpc, inv
        incremental.recover_snapshot(store)
        self.headers = store.get('cache/snapshots/headers.json', {})
        self.validated_anchors = set()
        self.boundary = None
        self.boundary_checked = False

    def header(self, block, fresh=False):
        key = hex(block) if isinstance(block, int) else block
        if not fresh and key in self.headers:
            return self.headers[key]
        h = self.rpc.call('eth_getBlockByNumber', [key, False])
        if not h or not h.get('hash'):
            raise RpcError('block_unavailable')
        if isinstance(block, int) and number(h['number']) != block:
            raise EvidenceError('block number mismatch')
        compact = {k: h[k] for k in ('number', 'hash', 'parentHash', 'timestamp')}
        old = self.headers.get(h['number'])
        if old and old['hash'] != compact['hash']:
            raise EvidenceError('REORG: cached header differs; evidence preserved, use a separate cache for new canonical history')
        self.headers[h['number']] = compact
        self.store.put('cache/snapshots/headers.json', self.headers)
        return compact

    def headers_batch(self, blocks, fresh=False):
        absent = sorted(b for b in set(blocks) if fresh or hex(b) not in self.headers)
        for offset in range(0, len(absent), 10):
            group = absent[offset:offset+10]
            requests = [('eth_getBlockByNumber', [hex(b), False]) for b in group]
            try:
                values = self.rpc.batch(requests)
            except RpcError:
                values = [self.rpc.retry(*r) for r in requests]
            for block, value in zip(group, values):
                if isinstance(value, RpcError) or not value or number(value['number']) != block:
                    raise EvidenceError('missing or inconsistent block header')
                old = self.headers.get(hex(block))
                if old and old['hash'] != value['hash']:
                    raise EvidenceError('REORG: cached block changed; cache preserved')
                self.headers[hex(block)] = {k: value[k] for k in ('number', 'hash', 'parentHash', 'timestamp')}
            self.store.put('cache/snapshots/headers.json', self.headers)

    def check_snapshot(self, snap):
        if self.header(snap['number'], fresh=True)['hash'] != snap['hash']:
            raise EvidenceError('REORG: snapshot hash mismatch; cache preserved')

    def cached_rpc(self, category, key, method, params, expected_hash=None, retries=2):
        rel = f'cache/{category}/{key}.json'
        old = self.store.get(rel)
        if old is not None:
            if expected_hash and old.get('blockHash') != expected_hash:
                raise EvidenceError('cached transaction block hash mismatch')
            return old
        result = self.rpc.call(method, params, retries=retries)
        if result is None:
            raise RpcError('evidence_unavailable')
        if expected_hash and result.get('blockHash') != expected_hash:
            raise EvidenceError('transaction block hash mismatch')
        self.store.put(rel, result)
        return result

    def call(self, address, signature, args, block):
        result = self.rpc.call('eth_call', [{'to': address, 'data': abi.calldata(signature, *args)}, hex(block)])
        if not isinstance(result, str) or len(result) <= 2 or not result.startswith('0x') or len(result[2:]) % 64:
            raise RpcError('invalid_call_data')
        return result

    def preflight(self, depth=256, chunk=5000, advance=False):
        if number(self.rpc.call('eth_chainId', [])) != 1672:
            raise EvidenceError('wrong chainId; expected 1672')
        existing = self.store.get('checkpoints/snapshot.json')
        previous_plan = self.store.get('checkpoints/plan.json', {})
        self.boundary = incremental.load_boundary(self.store)
        if advance and existing and not self.boundary:
            self.boundary = incremental.bootstrap(self.store, previous_plan)
        if self.boundary:
            incremental.validate_boundary(self.store, self.boundary)
            self.check_snapshot(self.boundary['snapshot'])
            self.boundary_checked = True
        if existing:
            self.check_snapshot(existing)
        # A pending snapshot is resumed, even when --advance is repeated after interruption.
        pending = self.boundary and existing and not incremental.same(existing, self.boundary['snapshot'])
        if existing and (not advance or pending):
            snap = existing
        else:
            head = number(self.rpc.call('eth_blockNumber', []))
            try:
                h = self.header('finalized', fresh=True)
                finality = 'provider-finalized-tag'
            except RpcError:
                if advance:
                    raise EvidenceError('incremental advance requires a finalized block tag') from None
                h = self.header(max(0, head - depth), fresh=True)
                finality = f'lagged-{depth}-blocks; finality unverified'
            snap = {'number': number(h['number']), 'hash': h['hash'], 'finality': finality, 'observedHead': head}
            if existing and snap['number'] < existing['number']:
                raise EvidenceError('snapshot cannot move backwards')
            if existing and incremental.same(snap, existing):
                snap = existing
            if existing and snap != existing:
                self.store.archive_snapshot(existing)
            self.store.put('checkpoints/snapshot.json', snap)
        if self.boundary and incremental.same(snap, self.boundary['snapshot']) and incremental.same(previous_plan.get('snapshot'), snap):
            return previous_plan
        plan = {'snapshot': snap, 'startBlock': self.inv['start'], 'scopes': scopes(self.inv),
                'capabilities': {}, 'initialChunk': chunk, 'identity': {}, 'status': 'PREFLIGHT'}
        if previous_plan.get('scopes'):
            plan['scopes'] = previous_plan['scopes']
            required = scopes(self.inv)
            existing_labels = {s['label'] for s in plan['scopes']}
            plan['scopes'] += [s for s in required if s['label'] not in existing_labels]
        if 'rangeCeiling' in previous_plan:
            plan['rangeCeiling'] = min(1000, previous_plan['rangeCeiling'])
        if self.boundary:
            # Existing capability evidence is historical, not a new-snapshot attestation.
            plan['capabilities'] = previous_plan.get('capabilities', {})
            plan['historicalBoundary'] = incremental.identity(self.boundary['snapshot'])
            plan['effectiveChunk'] = min(1000, previous_plan.get('effectiveChunk', chunk))
            plan['rangeCeiling'] = min(1000, plan.get('rangeCeiling', chunk))
            plan['estimate'] = {'baseRequests': 0, 'baseCU': 0, 'note': 'Only uncovered intervals; current state and trace costs are additional.'}
            for scope in plan['scopes']:
                chunks = self.store.chunks(scope['id'])
                for lo, hi in gaps([(m['startBlock'], m['endBlock']) for m, _ in chunks], scope['start'], snap['number']):
                    n = (hi - lo + plan['rangeCeiling']) // plan['rangeCeiling']
                    plan['estimate']['baseRequests'] += n * 2
                    plan['estimate']['baseCU'] += n * 190
            plan['status'] = 'PLANNED'
            self.store.put('checkpoints/plan.json', plan)
            return plan
        self.store.put('checkpoints/plan.json', plan)
        st = self.inv['addresses']['proxies']['StPROS'].lower()
        migration = self.inv['release']['upgradeTransaction']
        for label, method, params in [
            ('historicalCall', 'eth_call', [{'to': st, 'data': abi.calldata('totalSupply()')}, hex(self.inv['release']['upgradeBlock'] - 1)]),
            ('receipt', 'eth_getTransactionReceipt', [migration]),
            ('trace', 'debug_traceTransaction', [migration, {'tracer': 'callTracer', 'timeout': '10s'}]),
            ('stakingCode', 'eth_getCode', [STAKING, hex(snap['number'])])]:
            try:
                if label == 'trace':
                    receipt = self.store.get('cache/receipts/' + migration + '.json')
                    if not receipt or number(receipt.get('status', 0)) != 1:
                        plan['capabilities'][label] = 'UNVERIFIED_RECEIPT'
                        self.store.put('checkpoints/plan.json', plan)
                        continue
                    if self.header(number(receipt['blockNumber']))['hash'] != receipt['blockHash']:
                        raise EvidenceError('migration receipt/header mismatch')
                    if not self.can_collect_trace():
                        plan['capabilities'][label] = 'DEFERRED_BUDGET'
                        self.store.put('checkpoints/plan.json', plan)
                        continue
                result = self.rpc.call(method, params, retries=0 if label == 'trace' else 2)
                plan['capabilities'][label] = 'AVAILABLE' if result is not None and result != '0x' else 'UNAVAILABLE'
                if label in ('receipt', 'trace') and result:
                    self.store.put(f'cache/{"receipts" if label == "receipt" else "traces"}/{migration}.json', result)
            except RpcError as exc:
                plan['capabilities'][label] = exc.category
            self.store.put('checkpoints/plan.json', plan)
        # One bounded sample probes provider limits. On failure, shrink immediately.
        size = min(1000, chunk, snap['number'] - self.inv['start'] + 1)
        while size > 0:
            try:
                logs = self.rpc.call('eth_getLogs', [dict(plan['scopes'][0]['filter'],
                    fromBlock=hex(self.inv['start']), toBlock=hex(self.inv['start'] + size - 1))], retries=0)
                self.validate_logs(logs, plan['scopes'][0]['filter'], self.inv['start'], self.inv['start'] + size - 1)
                plan['capabilities']['getLogsSample'] = {'range': size, 'count': len(logs)}
                break
            except RpcError:
                if size == 1:
                    raise
                size = max(1, size // 2)
        for scope in plan['scopes']:
            scope['start'] = min(scope['start'], self.inv['start'])
        plan['effectiveChunk'] = size
        intervals = sum((snap['number'] - s['start'] + size) // size for s in plan['scopes'])
        # Logs + boundary headers. Event-block headers, state, receipts/traces are additional.
        plan['estimate'] = {'baseRequests': intervals * 2, 'baseCU': intervals * (WEIGHTS['eth_getLogs'] + WEIGHTS['eth_getBlockByNumber']),
            'additionalReserveCU': 700000, 'note': 'Conservative planning estimate, not Alchemy billing. Actual retries and evidence requests are charged separately.'}
        plan['status'] = 'PLANNED'
        self.store.put('checkpoints/plan.json', plan)
        return plan

    @staticmethod
    def validate_logs(logs, filt, start, end):
        if not isinstance(logs, list):
            raise RpcError('invalid_logs_response')
        addresses = filt['address'] if isinstance(filt['address'], list) else [filt['address']]
        for log in ordered(logs):
            if not start <= number(log['blockNumber']) <= end or log['address'].lower() not in addresses:
                raise EvidenceError('log outside requested scope')
            for i, condition in enumerate(filt.get('topics', [])):
                if condition is not None:
                    allowed = condition if isinstance(condition, list) else [condition]
                    if i >= len(log['topics']) or log['topics'][i].lower() not in allowed:
                        raise EvidenceError('log outside indexed filter')
        return logs

    def sync(self, plan):
        if number(self.rpc.call('eth_chainId', [])) != 1672:
            raise EvidenceError('wrong chainId; expected 1672')
        snap = plan['snapshot']
        self.check_snapshot(snap)
        if plan.get('historicalBoundary'):
            self.boundary = incremental.load_boundary(self.store, plan['historicalBoundary'])
            if not self.boundary:
                raise EvidenceError('missing finalized history seal')
            if not self.boundary_checked:
                incremental.validate_boundary(self.store, self.boundary)
                self.check_snapshot(self.boundary['snapshot'])
                self.boundary_checked = True
            boundary = self.boundary['snapshot']
            if snap['number'] < boundary['number']:
                raise EvidenceError('snapshot precedes trusted boundary')
            if snap['number'] > boundary['number']:
                child = self.header(boundary['number'] + 1, fresh=True)
                if child['parentHash'] != boundary['hash']:
                    raise EvidenceError('REORG: new interval does not extend finalized history')
        trusted = {s['id']: s for s in self.boundary['scopes']} if self.boundary else {}
        for scope in plan['scopes']:
            chunks = self.store.chunks(scope['id'])
            # Revalidate interval anchors, including empty intervals, before reuse.
            covered_boundary = self.boundary['snapshot']['number'] if scope['id'] in trusted else -1
            if scope['id'] in trusted:
                old_scope = trusted[scope['id']]
                if old_scope != scope or list(gaps([(m['startBlock'], m['endBlock']) for m, _ in chunks
                        if m['endBlock'] <= covered_boundary], scope['start'], covered_boundary)):
                    raise EvidenceError('sealed filter changed or historical coverage incomplete')
            anchors = {int(b) for meta, rows in chunks if meta['endBlock'] > covered_boundary
                       for b in list(meta['headers']) + [str(number(l['blockNumber'])) for l in rows]}
            self.headers_batch(anchors - self.validated_anchors, fresh=True)
            self.validated_anchors.update(anchors)
            for meta, logs in chunks:
                if meta['endBlock'] > snap['number']:
                    raise EvidenceError('chunk exceeds pinned snapshot')
                self.validate_logs(logs, scope['filter'], meta['startBlock'], meta['endBlock'])
                for log in logs:
                    if self.headers.get(log['blockNumber'], {}).get('hash') != log['blockHash']:
                        raise EvidenceError('cached log block hash mismatch')
                for block, expected in meta['headers'].items():
                    if self.headers[hex(int(block))]['hash'] != expected:
                        raise EvidenceError('REORG: interval hash mismatch')
            missing = list(gaps([(m['startBlock'], m['endBlock']) for m, _ in chunks], scope['start'], snap['number']))
            size = plan.get('rangeCeiling', plan.get('effectiveChunk', plan['initialChunk']))
            ceiling = min(1000, plan.get('rangeCeiling', 1000))
            size = min(size, ceiling)
            for lo, hi in missing:
                cursor = lo
                while cursor <= hi:
                    # Once provider range limits are learned, amortize HTTP latency with
                    # at most ten reads per batch. Each request retains its full CU charge.
                    ranges = []
                    begin = cursor
                    count = 10 if 'rangeCeiling' in plan else 1
                    for _ in range(count):
                        end = min(hi, begin + size - 1)
                        ranges.append((begin, end))
                        begin = end + 1
                        if begin > hi:
                            break
                    requests = [('eth_getLogs', [dict(scope['filter'], fromBlock=hex(a), toBlock=hex(b))])
                                for a, b in ranges]
                    try:
                        outputs = self.rpc.batch(requests) if len(requests) > 1 else [self.rpc.call(*requests[0], retries=0)]
                    except RpcError as exc:
                        if len(requests) == 1:
                            outputs = [exc]
                        else:
                            outputs = [self.rpc.retry(*r) for r in requests]
                    needed_batch = {end for _, end in ranges}
                    for output in outputs:
                        if isinstance(output, list):
                            needed_batch.update(number(l['blockNumber']) for l in output)
                    self.headers_batch(needed_batch)
                    failed = False
                    for (begin, end), output in zip(ranges, outputs):
                        if isinstance(output, RpcError) or len(output) >= 10000:
                            if size == 1:
                                raise RpcError('single_block_logs_unavailable')
                            ceiling = min(ceiling, max(1, size // 2))
                            size = ceiling
                            plan['rangeCeiling'] = ceiling
                            self.store.put('checkpoints/plan.json', plan)
                            failed = True
                            break
                        logs = ordered(self.validate_logs(output, scope['filter'], begin, end))
                        for log in logs:
                            if self.headers[log['blockNumber']]['hash'] != log['blockHash']:
                                raise EvidenceError('log block hash mismatch')
                        self.store.chunk(scope['id'], begin, end, snap, logs,
                                         {str(end): self.headers[hex(end)]['hash']})
                        cursor = end + 1
                        self.store.put('checkpoints/progress.json', {'scope': scope['id'], 'through': end, 'snapshot': snap})
                    if not failed and len(ranges) == 1 and len(outputs[0]) < 1000:
                        size = min(ceiling, size * 2)
        self.check_snapshot(snap)

    def identities(self, logs, snap):
        result = {}
        observed = {a: [] for a, t in self.inv['targets'].items() if t['known']}
        for log in logs:
            a = log['address'].lower()
            if a in observed:
                e = decode(log, [UPGRADE])
                if e:
                    observed[a].append((e['args']['implementation'].lower(), number(log['blockNumber'])))
        for addr, upgrades in observed.items():
            latest = self.rpc.call('eth_getStorageAt', [addr, IMPL_SLOT, hex(snap['number'])])
            latest = '0x' + latest[-40:]
            verified, issues = [], []
            for implementation, at in upgrades + [(latest, snap['number'])]:
                entry = self.inv['implementations'].get(implementation)
                if not entry:
                    issues.append('Unknown deployed implementation ' + implementation)
                    continue
                prior = self.boundary['context'].get('identities', {}).get('entries', {}).get(addr, {}) if self.boundary else {}
                historical = next((v for v in prior.get('verified', []) if v['implementation'] == implementation and v['at'] == at and v['runtimeKeccak'] == entry['runtimeKeccak']), None)
                if historical and at < snap['number'] and prior.get('status') == 'PASS':
                    verified.append(historical)
                    continue
                code = self.rpc.call('eth_getCode', [implementation, hex(at)])
                actual = keccak256(bytes.fromhex(code[2:]))
                if actual != entry['runtimeKeccak']:
                    issues.append('Runtime hash mismatch at ' + str(at))
                else:
                    verified.append(dict(implementation=implementation, at=at, runtimeKeccak=actual))
            if not upgrades or upgrades[-1][0] != latest:
                issues.append('Upgrade events do not establish current implementation')
            result[addr] = {'status': 'PASS' if not issues else 'BLOCKED', 'verified': verified, 'issues': issues}
            self.store.put('cache/snapshots/identities.json', {'snapshot': snap, 'entries': result})
        return result

    def states(self, snap, replay_result=None):
        # Cache each successful read immediately so budget exhaustion preserves partial evidence.
        out = self.store.get('cache/snapshots/state.json', {})
        if out.get('snapshot') != snap:
            out = {'snapshot': snap, 'custody': {}, 'queues': {}, 'holders': {}, 'errors': {}}
        proxies = {k: v.lower() for k, v in self.inv['addresses']['proxies'].items()}
        st, oracle, bridge = (proxies[n] for n in ('StPROS', 'Oracle', 'BridgeVault'))
        asset = self.inv['addresses']['asset']['WPROS'].lower()
        block = snap['number']

        def save():
            self.store.put('cache/snapshots/state.json', out)

        for key, addr, sig, args in [
            ('supply', st, 'totalSupply()', []), ('queued', st, 'queuedWithdrawal()', []),
            ('completed', st, 'completedWithdrawal()', []), ('frontier', st, 'getTotalBalance()', []),
            ('legacyReserve', st, 'totalCanWithdrawAmount()', []), ('bridgeBalance', bridge, 'getBalance(address)', [asset]),
            ('pool', oracle, 'poolInfo(address)', [asset])]:
            if key in out:
                continue
            try:
                raw = self.call(addr, sig, args, block)
                if key == 'pool':
                    out['T'], out['V'] = abi.dec_uint(raw), abi.dec_uint(raw, 1)
                    out[key] = raw
                else:
                    out[key] = abi.dec_uint(raw)
            except (RpcError, ValueError):
                out['errors'][key] = 'UNAVAILABLE'
            save()
        for addr, target in self.inv['targets'].items():
            row = out['custody'].setdefault(addr, {'name': target['name'], 'classification': 'UNCLASSIFIED custody; not verified attributable backing'})
            for key, method, params in [('native', 'eth_getBalance', [addr, hex(block)]),
                                        ('WPROS', 'eth_call', [{'to': asset, 'data': abi.calldata('balanceOf(address)', addr)}, hex(block)])]:
                if key not in row:
                    try:
                        row[key] = int(self.rpc.call(method, params), 16)
                    except (RpcError, ValueError):
                        out['errors'][addr + ':' + key] = 'UNAVAILABLE'
                    save()
        if replay_result:
            for owner in replay_result['replay']['queues']:
                if owner not in out['queues']:
                    try:
                        out['queues'][owner] = abi.dec_tuple_array(self.call(st, 'getWithdrawals(address)', [owner], block))
                    except (RpcError, ValueError):
                        out['errors']['queue:' + owner] = 'UNAVAILABLE'
                    save()
            cursors = out.setdefault('queueCursors', {})
            for owner in replay_result['replay']['queues']:
                row = cursors.setdefault(owner, {})
                for name, sig in [('head', 'withdrawalHead(address)'), ('tail', 'withdrawalTail(address)'),
                                  ('claimable', 'canWithdrawalAmount(address)')]:
                    if name in row:
                        continue
                    try:
                        raw = self.call(st, sig, [owner], block)
                        row[name] = [abi.dec_uint(raw, i) for i in range(3)] if name == 'claimable' else abi.dec_uint(raw)
                    except (RpcError, ValueError):
                        out['errors']['queue-' + name + ':' + owner] = 'UNAVAILABLE'
                    save()
            for holder in replay_result['replay']['balances']:
                if holder not in out['holders']:
                    try:
                        out['holders'][holder] = abi.dec_uint(self.call(st, 'balanceOf(address)', [holder], block))
                    except (RpcError, ValueError):
                        out['errors']['holder:' + holder] = 'UNAVAILABLE'
                    save()
        self.check_snapshot(snap)
        return out

    def historical_routes(self, plan, logs):
        """Discover and backfill previously unknown routing destinations, retaining prior scopes."""
        from .inventory import scopes as make_scopes, source_event_hints, ROOT
        from .storage import canonical, digest
        fields = {'SlpWhitelistUpdated': ['slp'], 'SlpChanged': ['oldSlp', 'newSlp'],
                  'BridgeVaultChanged': ['oldBridgeVault', 'newBridgeVault'],
                  'OracleChanged': ['oldOracle', 'newOracle'],
                  'BridgeVaultSet': ['bridgeVault'], 'SlpPharosProxySet': ['slpPharosProxy']}
        discovered = self.store.get('cache/snapshots/discovered-targets.json', {})
        candidates = {}
        for log in logs:
            target = self.inv['targets'].get(log['address'].lower())
            if not target:
                continue
            decoded = decode(log, [item for item in target['abi'] if item.get('name') in fields])
            if not decoded or decoded['name'] not in fields:
                continue
            for field in fields[decoded['name']]:
                addr = decoded['args'][field].lower()
                if addr != ZERO and addr not in self.inv['targets'] and addr not in discovered:
                    candidates[addr] = min(candidates.get(addr, 2**63), number(log['blockNumber']))
        if len(candidates) + len(discovered) > 64:
            raise EvidenceError('historical routing discovery exceeds 64-address bound')
        extra = {}
        for addr, first_seen in sorted(candidates.items()):
            code = self.rpc.call('eth_getCode', [addr, hex(first_seen)])
            if code == '0x':
                discovered[addr] = {'firstSeen': first_seen, 'status': 'NO_CODE_AT_REFERENCE',
                                    'scope': 'External routing recipient; investigate ownership and economic purpose'}
                continue
            items = []  # Unknown routing code has no authenticated business ABI.
            extra[addr] = dict(name='Historical routing contract ' + addr, address=addr, start=self.inv['start'],
                               abi=items, abiStatus='UNVERIFIED_SOURCE_HINT', known={})
            discovered[addr] = dict(firstSeen=first_seen, status='RAW_BACKFILL', runtimeKeccak=keccak256(bytes.fromhex(code[2:])))
        self.store.put('cache/snapshots/discovered-targets.json', discovered)
        if extra:
            small = dict(self.inv, targets=extra)
            additions = [s for s in make_scopes(small) if not s['label'].startswith('Staking:')]
            existing = {s['id'] for s in plan['scopes']}
            plan['scopes'] += [s for s in additions if s['id'] not in existing]
            self.inv['targets'].update(extra)
            self.store.put('cache/snapshots/inventory.json', self.inv)
            self.store.put('checkpoints/plan.json', plan)
            self.sync(dict(plan, scopes=additions))
        return bool(extra)

    def migration_states(self, snapshot):
        out = self.store.get('cache/snapshots/migration-state.json', {})
        block = self.inv['release']['upgradeBlock']
        st = self.inv['addresses']['proxies']['StPROS'].lower()
        asset = self.inv['addresses']['asset']['WPROS'].lower()
        for label, at in [('before', block - 1), ('after', block)]:
            self.header(at)
            for key, addr, sig, args in [('Supply', st, 'totalSupply()', []),
                ('Wrapped', asset, 'balanceOf(address)', [st]), ('Escrow', st, 'balanceOf(address)', [st]),
                ('Queued', st, 'queuedWithdrawal()', []), ('Completed', st, 'completedWithdrawal()', []),
                ('Reserve', st, 'totalCanWithdrawAmount()', [])]:
                if label + key not in out:
                    try:
                        out[label + key] = abi.dec_uint(self.call(addr, sig, args, at))
                    except RpcError:
                        out[label + key + 'Error'] = 'UNAVAILABLE'
                    self.store.put('cache/snapshots/migration-state.json', out)
        oracle = self.inv['addresses']['proxies']['Oracle'].lower()
        for label, at in [('before', block - 1), ('after', block)]:
            if label + 'OraclePool' not in out:
                try:
                    raw = self.call(oracle, 'poolInfo(address)', [asset], at)
                    out[label + 'OraclePool'] = [abi.dec_uint(raw, i) for i in range(2)]
                except RpcError:
                    out[label + 'OraclePoolError'] = 'UNAVAILABLE'
            for name in ('StPROS', 'BridgeVault', 'SlpDistributor'):
                key = label + name + 'Native'
                if key not in out:
                    address = self.inv['addresses']['proxies'][name].lower()
                    try:
                        out[key] = number(self.rpc.call('eth_getBalance', [address, hex(at)]))
                    except RpcError:
                        out[key + 'Error'] = 'UNAVAILABLE'
            self.store.put('cache/snapshots/migration-state.json', out)
        out['boundary'] = 'block-boundary; transaction-exact transition uses ordered logs and successful trace'
        self.store.put('cache/snapshots/migration-state.json', out)
        return out

    def transactions(self, logs, max_receipts=2000, max_traces=40, include_optional=True):
        boundary = self.boundary['snapshot']['number'] if self.boundary else -1
        grouped, pending, required = transaction_requirements(self.inv, logs, boundary)
        transactions = {tx: rows[0]['blockHash'] for tx, rows in grouped.items()}
        migration = self.inv['release']['upgradeTransaction'].lower()
        priorities = {}
        critical_topics = {abi.event_topic(s) for s in ['TokenAmountUpdated(address,uint256,uint256)',
            'EmergencyWithdraw(address,address,uint256)', 'SlpPharosProxySet(address,address)',
            'BridgeVaultSet(address,address)', 'TransferredBack(address,uint256)', 'Distributed(address,uint256)']}
        upgrade_topic = abi.event_topic('Upgraded(address)')
        completion_topic = abi.event_topic('WithdrawalCompleted(address,address,uint256)')
        st = self.inv['addresses']['proxies']['StPROS'].lower()
        for log in logs:
            tx = log['transactionHash'].lower()
            topic = log['topics'][0] if log['topics'] else None
            priority = 0 if tx == migration else 1 if topic in critical_topics else 2 if topic == upgrade_topic else 4
            if topic == completion_topic:
                priority = 1 if len(log['topics']) > 2 and abi.topic_addr(log['topics'][2]) == st else 3
            priorities[tx] = min(priorities.get(tx, 4), priority)
        ordered_txs = sorted(transactions, key=lambda t: (t not in required, priorities[t], t))
        coverage = dict(snapshot=self.store.get('checkpoints/plan.json', {}).get('snapshot'),
                        total=len(transactions), receipts=0, traces=0, newReceiptAttempts=0, newTraceAttempts=0,
                        receiptCap=max_receipts, traceCap=max_traces, missingReceipts=sorted(pending),
                        missingRequiredTraces=sorted(required), requiredEvidenceComplete=False,
                        limitation='Event-discovered transactions only; historical gaps remain unverified.')
        self.transaction_work = (ordered_txs, grouped, pending, required, set(), coverage)
        expected_scopes = self.store.get('checkpoints/plan.json', {}).get('scopes') or scopes(self.inv)
        try:
            for tx in ordered_txs:
                has_receipt = self.store.get('cache/receipts/' + tx + '.json') is not None
                has_transaction = self.store.get('cache/receipts/' + tx + '-transaction.json') is not None
                historical = self.boundary and max(number(l['blockNumber']) for l in grouped[tx]) <= self.boundary['snapshot']['number']
                if not has_receipt or not has_transaction:
                    if historical:
                        continue
                    if coverage['newReceiptAttempts'] >= max_receipts:
                        continue
                    coverage['newReceiptAttempts'] += 1
                try:
                    receipt = self.cached_rpc('receipts', tx, 'eth_getTransactionReceipt', [tx], transactions[tx])
                    if receipt['blockHash'] != transactions[tx] or number(receipt['status']) != 1:
                        raise EvidenceError('receipt inconsistent with successful logs')
                    self.cached_rpc('receipts', tx + '-transaction', 'eth_getTransactionByHash', [tx], transactions[tx])
                    # Check all known transaction logs, including missing logs inside the collected scope.
                    selected = grouped[tx]
                    for log in selected:
                        if log['blockNumber'] in self.headers and self.headers[log['blockNumber']]['hash'] != log['blockHash']:
                            raise EvidenceError('transaction event/header mismatch')
                        match = [x for x in receipt['logs'] if number(x['logIndex']) == number(log['logIndex'])]
                        if len(match) != 1 or any(match[0][k] != log[k] for k in ('address', 'topics', 'data', 'blockHash', 'transactionHash')):
                            raise EvidenceError('receipt/log mismatch')
                    selected_indices = {number(l['logIndex']) for l in selected}
                    for receipt_log in receipt['logs']:
                        relevant = False
                        for scope in expected_scopes:
                            try:
                                self.validate_logs([receipt_log], scope['filter'], scope['start'], 2**63)
                                relevant = True
                                break
                            except EvidenceError:
                                pass
                        if relevant and number(receipt_log['logIndex']) not in selected_indices:
                            raise EvidenceError('missing scoped log revealed by receipt')
                    coverage['receipts'] += 1
                    self.transaction_work[4].add(tx)
                    if tx in pending:
                        coverage['missingReceipts'].remove(tx)
                except RpcError:
                    continue
        finally:
            self.save_transaction_coverage()
        return self.collect_transaction_traces(max_traces, include_optional)

    def save_transaction_coverage(self):
        _, _, pending, required, validated, coverage = self.transaction_work
        coverage['traces'] = sum(usable_trace(self.store.get('cache/traces/' + tx + '.json')) for tx in validated)
        coverage['missingRequiredTraces'] = sorted(tx for tx in required if tx not in validated or
            not usable_trace(self.store.get('cache/traces/' + tx + '.json')))
        coverage['requiredEvidenceComplete'] = not coverage['missingReceipts'] and not coverage['missingRequiredTraces']
        self.store.put('checkpoints/transaction-coverage.json', coverage)
        return coverage

    def can_collect_trace(self):
        # Reserve reward, final-snapshot and historical-boundary checks. Rpc still charges
        # each request/retry before sending and enforces both original ceilings.
        reserve = 3 * WEIGHTS['eth_getBlockByNumber']
        cost = WEIGHTS['debug_traceTransaction'] + reserve
        return (self.rpc.ledger['estimatedCU'] + cost <= self.rpc.budget and
                self.rpc.run_usage()['estimatedCU'] + cost <= self.rpc.run_budget)

    def collect_transaction_traces(self, max_traces=40, include_optional=True):
        ordered_txs, grouped, pending, required, validated, coverage = self.transaction_work
        try:
            # No trace can consume the budget needed by a missing receipt/detail pair,
            # including when a receipt cap or a provider failure stopped that phase.
            if coverage['missingReceipts']:
                return coverage
            for tx in ordered_txs:
                if tx not in pending or tx not in validated or (not include_optional and tx not in required):
                    continue
                trace = self.store.get('cache/traces/' + tx + '.json')
                if trace is not None:  # Preserve existing evidence, including unsuccessful traces.
                    continue
                if coverage['newTraceAttempts'] >= max_traces or not self.can_collect_trace():
                    break
                row = grouped[tx][0]
                if self.headers.get(row['blockNumber'], {}).get('hash') != row['blockHash']:
                    raise EvidenceError('trace requires a verified event block header')
                coverage['newTraceAttempts'] += 1
                try:
                    self.cached_rpc('traces', tx, 'debug_traceTransaction', [tx, {'tracer': 'callTracer', 'timeout': '10s'}], retries=0)
                except RpcError as exc:
                    if exc.category == 'unsupported':
                        break
        finally:
            self.save_transaction_coverage()
        return coverage


def cached_logs(store, plan):
    logs, intervals, complete = [], [], True
    headers = store.get('cache/snapshots/headers.json', {})
    for scope in plan['scopes']:
        chunks = store.chunks(scope['id'])
        covered = []
        for meta, rows in chunks:
            if meta['endBlock'] > plan['snapshot']['number']:
                raise EvidenceError('chunk exceeds pinned snapshot')
            Collector.validate_logs(rows, scope['filter'], meta['startBlock'], meta['endBlock'])
            if headers:
                for block, expected in meta['headers'].items():
                    if headers.get(hex(int(block)), {}).get('hash') != expected:
                        raise EvidenceError('cached interval/header mismatch')
                for row in rows:
                    if headers.get(row['blockNumber'], {}).get('hash') != row['blockHash']:
                        raise EvidenceError('cached event/header mismatch')
            covered.append((meta['startBlock'], meta['endBlock']))
            logs.extend(rows)
            intervals.append(meta)
        if list(gaps(covered, scope['start'], plan['snapshot']['number'])):
            complete = False
    # Overlapping FROM/TO WPROS scopes are deliberately deduplicated by exact identity.
    return ordered(logs, allow_identical=True), intervals, complete
