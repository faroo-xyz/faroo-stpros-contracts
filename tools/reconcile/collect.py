"""Pinned-snapshot collection. No transactions, signing, wallets or generic RPC entry point."""
from tools.lib import abi
from tools.lib.keccak import keccak256
from .inventory import CORE, IMPL_SLOT, STAKING, ZERO, number, scopes, decode, UPGRADE
from .storage import EvidenceError, gaps, ordered
from .rpc import RpcError, BudgetExceeded, WEIGHTS


class Collector:
    def __init__(self, store, rpc, inv):
        self.store, self.rpc, self.inv = store, rpc, inv
        self.headers = store.get('cache/snapshots/headers.json', {})
        self.validated_anchors = set()

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

    def cached_rpc(self, category, key, method, params, expected_hash=None):
        rel = f'cache/{category}/{key}.json'
        old = self.store.get(rel)
        if old is not None:
            return old
        result = self.rpc.call(method, params)
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
        if existing:
            self.check_snapshot(existing)
        if existing and not advance:
            snap = existing
        else:
            head = number(self.rpc.call('eth_blockNumber', []))
            try:
                h = self.header('finalized', fresh=True)
                finality = 'provider-finalized-tag'
            except RpcError:
                h = self.header(max(0, head - depth), fresh=True)
                finality = f'lagged-{depth}-blocks; finality unverified'
            snap = {'number': number(h['number']), 'hash': h['hash'], 'finality': finality, 'observedHead': head}
            if existing and snap['number'] < existing['number']:
                raise EvidenceError('snapshot cannot move backwards')
            if existing and snap != existing:
                self.store.archive_snapshot(existing)
            self.store.put('checkpoints/snapshot.json', snap)
        previous_plan = self.store.get('checkpoints/plan.json', {})
        plan = {'snapshot': snap, 'startBlock': self.inv['start'], 'scopes': scopes(self.inv),
                'capabilities': {}, 'initialChunk': chunk, 'identity': {}, 'status': 'PREFLIGHT'}
        if previous_plan.get('scopes'):
            plan['scopes'] = previous_plan['scopes']
            required = scopes(self.inv)
            # The original fixed address scope remains stable after discovery.
            existing_labels = {s['label'] for s in plan['scopes']}
            plan['scopes'] += [s for s in required if s['label'] not in existing_labels]
        if 'rangeCeiling' in previous_plan:
            plan['rangeCeiling'] = previous_plan['rangeCeiling']
        self.store.put('checkpoints/plan.json', plan)
        st = self.inv['addresses']['proxies']['StPROS'].lower()
        migration = self.inv['release']['upgradeTransaction']
        for label, method, params in [
            ('historicalCall', 'eth_call', [{'to': st, 'data': abi.calldata('totalSupply()')}, hex(self.inv['release']['upgradeBlock'] - 1)]),
            ('receipt', 'eth_getTransactionReceipt', [migration]),
            ('trace', 'debug_traceTransaction', [migration, {'tracer': 'callTracer', 'timeout': '10s'}]),
            ('stakingCode', 'eth_getCode', [STAKING, hex(snap['number'])])]:
            try:
                result = self.rpc.call(method, params)
                plan['capabilities'][label] = 'AVAILABLE' if result is not None and result != '0x' else 'UNAVAILABLE'
                if label in ('receipt', 'trace') and result:
                    self.store.put(f'cache/{"receipts" if label == "receipt" else "traces"}/{migration}.json', result)
            except RpcError as exc:
                plan['capabilities'][label] = exc.category
            self.store.put('checkpoints/plan.json', plan)
        # One bounded sample probes provider limits. On failure, shrink immediately.
        size = min(chunk, snap['number'] - self.inv['start'] + 1)
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
        for scope in plan['scopes']:
            chunks = self.store.chunks(scope['id'])
            # Revalidate interval anchors, including empty intervals, before reuse.
            anchors = {int(b) for meta, _ in chunks for b in meta['headers']}
            self.headers_batch(anchors - self.validated_anchors, fresh=True)
            self.validated_anchors.update(anchors)
            for meta, logs in chunks:
                self.validate_logs(logs, scope['filter'], meta['startBlock'], meta['endBlock'])
                for block, expected in meta['headers'].items():
                    if self.headers[hex(int(block))]['hash'] != expected:
                        raise EvidenceError('REORG: interval hash mismatch')
            missing = list(gaps([(m['startBlock'], m['endBlock']) for m, _ in chunks], scope['start'], snap['number']))
            size = plan.get('rangeCeiling', plan.get('effectiveChunk', plan['initialChunk']))
            ceiling = plan.get('rangeCeiling', 50000)
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
                if addr != ZERO and addr not in self.inv['targets']:
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
            items = source_event_hints(ROOT / 'contracts/SlpPharosProxy.sol') + source_event_hints(ROOT / 'contracts/SlpPharosRewardsVault.sol')
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

    def transactions(self, logs, max_receipts=2000, max_traces=40):
        transactions = {}
        migration = self.inv['release']['upgradeTransaction'].lower()
        for log in logs:
            transactions[log['transactionHash'].lower()] = log['blockHash']
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
        ordered_txs = sorted(transactions, key=lambda t: (priorities[t], t))
        receipt_count = trace_count = new_receipts = new_traces = 0
        expected_scopes = self.store.get('checkpoints/plan.json', {}).get('scopes') or scopes(self.inv)
        for tx in ordered_txs:
            has_receipt = self.store.get('cache/receipts/' + tx + '.json') is not None
            has_transaction = self.store.get('cache/receipts/' + tx + '-transaction.json') is not None
            if not has_receipt or not has_transaction:
                if new_receipts >= max_receipts:
                    continue
                new_receipts += 1
            try:
                receipt = self.cached_rpc('receipts', tx, 'eth_getTransactionReceipt', [tx], transactions[tx])
                if receipt['blockHash'] != transactions[tx] or number(receipt['status']) != 1:
                    raise EvidenceError('receipt inconsistent with successful logs')
                self.cached_rpc('receipts', tx + '-transaction', 'eth_getTransactionByHash', [tx], transactions[tx])
                # Check all known transaction logs, including missing logs inside the collected scope.
                selected = [l for l in logs if l['transactionHash'].lower() == tx]
                for log in selected:
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
                receipt_count += 1
            except RpcError:
                continue
            cached_trace = self.store.get('cache/traces/' + tx + '.json')
            if cached_trace is not None:
                trace_count += 1
            elif new_traces < max_traces:
                new_traces += 1
                try:
                    self.cached_rpc('traces', tx, 'debug_traceTransaction', [tx, {'tracer': 'callTracer', 'timeout': '10s'}])
                    trace_count += 1
                except RpcError as exc:
                    if exc.category == 'unsupported':
                        max_traces = 0
        self.store.put('checkpoints/transaction-coverage.json', dict(total=len(transactions), receipts=receipt_count,
                       traces=trace_count, newReceiptAttempts=new_receipts, newTraceAttempts=new_traces,
                       receiptCap=max_receipts, traceCap=max_traces,
                       limitation='Only transactions discovered through protocol events; native-only unsolicited transfers remain unverified.'))


def cached_logs(store, plan):
    logs, intervals, complete = [], [], True
    for scope in plan['scopes']:
        chunks = store.chunks(scope['id'])
        covered = []
        for meta, rows in chunks:
            if meta['endBlock'] > plan['snapshot']['number']:
                raise EvidenceError('chunk exceeds pinned snapshot')
            Collector.validate_logs(rows, scope['filter'], meta['startBlock'], meta['endBlock'])
            covered.append((meta['startBlock'], meta['endBlock']))
            logs.extend(rows)
            intervals.append(meta)
        if list(gaps(covered, scope['start'], plan['snapshot']['number'])):
            complete = False
    # Overlapping FROM/TO WPROS scopes are deliberately deduplicated by exact identity.
    return ordered(logs, allow_identical=True), intervals, complete
