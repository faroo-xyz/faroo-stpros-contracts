"""Separate accepted yield from observed reward sources; unidentified inflows stay unknown."""
from collections import defaultdict
from .inventory import STAKING


def provenance(inv, events, reported, custody):
    vaults = {a for a, t in inv['targets'].items() if 'RewardsVault' in t['name']}
    slps = {a for a, t in inv['targets'].items() if t['name'].startswith('SlpPharosProxy')}
    ledgers = {a: {'observedInflow': 0, 'stakingCorrelatedInflow': 0, 'unclassifiedInflow': 0,
                   'forwardedAfterNotify': 0, 'observedBalance': custody.get(a, {}).get('native'),
                   'entries': []} for a in vaults}
    by_tx, credits = {}, defaultdict(int)
    for e in events:
        by_tx.setdefault(e['raw']['transactionHash'].lower(), []).append(e)
    for tx, group in by_tx.items():
        claims, underlying, minted = defaultdict(int), defaultdict(int), defaultdict(int)
        for e in group:
            a = e['args']
            if e['address'] in slps and e['name'] == 'RewardClaimed':
                claims[e['address']] += a['amount']
            elif e['address'] == STAKING and e['name'] == 'RewardClaimed':
                underlying[a['delegator'].lower()] += a['amount']
            elif e['address'] == STAKING and e['name'] == 'CoinMinted' and a['reason'] == 1:
                minted[a['beneficiary'].lower()] += a['amount']
        for sender, amount in claims.items():
            if underlying[sender] == amount and minted[sender] == amount:
                credits[sender] += amount
        for e in group:
            if e['address'] not in vaults:
                continue
            row, a = ledgers[e['address']], e['args']
            if e['name'] == 'EthDeposited':
                amount = a['amount']; sender = a['sender'].lower()
                correlated = min(credits[sender], amount) if sender in slps else 0
                credits[sender] -= correlated
                row['observedInflow'] += amount
                row['stakingCorrelatedInflow'] += correlated
                row['unclassifiedInflow'] += amount - correlated
                row['entries'].append(dict(transaction=tx, amount=amount, sender=sender,
                    eventCreditUsed=correlated, unclassifiedWei=amount-correlated,
                    classification='Earlier/same-transaction Staking RewardClaimed + CoinMinted and SLP event credit; not native provenance proof'))
            elif e['name'] == 'OracleNotified':
                row['forwardedAfterNotify'] += a['amount']
    for row in ledgers.values():
        row['eventDerivedBalance'] = row['observedInflow'] - row['forwardedAfterNotify']
        row['balanceDifference'] = row['observedBalance'] - row['eventDerivedBalance'] if row['observedBalance'] is not None else None
    return {'status': 'UNVERIFIED', 'vaults': ledgers, 'unforwardedEventCredits': dict(credits),
            'acceptedOracleReward': sum(r['reward'] for r in reported),
            'vaultReportedTotal': sum(row['forwardedAfterNotify'] for row in ledgers.values()),
            'limitation': 'Cross-transaction event credits are attribution evidence, not proof that intervening native funds were unspent. Initial balances, unlogged flows, external implementation identity and client reward minting remain prerequisites.'}


def collect_evidence(collector, snapshot, events, max_cases=32, max_new_traces=32):
    """Targeted traces and real native balance changes for reward claims/forwarding/reports."""
    from .rpc import RpcError
    from .inventory import number
    inv, store = collector.inv, collector.store
    slps = {a for a, t in inv['targets'].items() if t['name'].startswith('SlpPharosProxy')}
    vaults = {a for a, t in inv['targets'].items() if 'RewardsVault' in t['name']}
    grouped = {}
    for e in events:
        if (e['address'] == STAKING and e['name'] == 'CoinMinted' and e['args']['beneficiary'].lower() in slps) or (e['address'] in vaults and e['name'] in ('EthDeposited', 'OracleNotified')):
            grouped.setdefault(e['raw']['transactionHash'].lower(), []).append(e)
    out = store.get('cache/snapshots/reward-evidence.json', {})
    if out.get('snapshot') != snapshot:
        previous = collector.boundary['context'].get('reward-evidence', {}) if collector.boundary else {}
        out = {'snapshot': snapshot, 'cases': dict(previous.get('cases', {})), 'balanceReads': dict(previous.get('balanceReads', {})), 'scope': 'Block-boundary native deltas and targeted traces; external source identity not certified'}
    new_traces = 0
    candidates = [(tx, rows) for tx, rows in grouped.items()
                  if (not collector.boundary or number(rows[0]['raw']['blockNumber']) > collector.boundary['snapshot']['number'])
                  and not (out['cases'].get(tx, {}).get('complete') and out['cases'][tx].get('trace') == 'AVAILABLE')]
    for tx, rows in candidates[:max_cases]:
        if tx in out['cases'] and out['cases'][tx].get('complete') and out['cases'][tx].get('trace') == 'AVAILABLE':
            continue
        block = number(rows[0]['raw']['blockNumber'])
        participants = set()
        for e in rows:
            if e['name'] == 'CoinMinted':
                participants.add(e['args']['beneficiary'].lower())
            else:
                participants.add(e['address'])
                if e['name'] == 'OracleNotified':
                    participants.update(slps)
                if e['name'] == 'EthDeposited':
                    participants.add(e['args']['sender'].lower())
        before = collector.header(block - 1)
        after = collector.header(block)
        case = {'block': block, 'beforeBlockHash': before['hash'], 'blockHash': after['hash'],
                'events': rows, 'balances': {}, 'complete': False}
        for address in sorted(participants):
            values = []
            for at in (block - 1, block):
                key = address + ':' + str(at)
                if key not in out['balanceReads']:
                    out['balanceReads'][key] = number(collector.rpc.call('eth_getBalance', [address, hex(at)]))
                    store.put('cache/snapshots/reward-evidence.json', out)
                values.append(out['balanceReads'][key])
            case['balances'][address] = {'before': values[0], 'after': values[1], 'difference': values[1] - values[0]}
        if store.get('cache/traces/' + tx + '.json') is not None:
            case['trace'] = 'AVAILABLE'
        elif new_traces < max_new_traces:
            new_traces += 1
            try:
                collector.cached_rpc('traces', tx, 'debug_traceTransaction', [tx, {'tracer': 'callTracer', 'timeout': '10s'}])
                case['trace'] = 'AVAILABLE'
            except RpcError:
                case['trace'] = 'UNAVAILABLE'
        else:
            case['trace'] = 'UNVERIFIED_TRACE_CAP'
        case['complete'] = True
        out['cases'][tx] = case
        store.put('cache/snapshots/reward-evidence.json', out)
    out['totalCandidateTransactions'] = len(grouped)
    out['caseLimit'] = max_cases
    store.put('cache/snapshots/reward-evidence.json', out)
    collector.check_snapshot(snapshot)
    return out
