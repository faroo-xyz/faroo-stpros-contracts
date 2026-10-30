"""Integer-only replay of recorded V1/V4 models. Unknown versions fail closed."""
from collections import defaultdict
from itertools import groupby
from tools.lib import abi
from .inventory import ZERO, UPGRADE, WPROS_ABI, STAKING, PROXY_ABI, decode, number
from .external import STAKING_ABI
from .storage import EvidenceError, ordered


def commission(t, v, reward, rate):
    if min(t, v, reward, rate) < 0 or rate > 1_000_000 or not v:
        raise EvidenceError('invalid commission prestate')
    fee = reward * rate // 1_000_000
    shares = fee * v // (t + reward - fee) if fee else 0
    return fee, shares


class Queue:
    def __init__(self):
        self.records = defaultdict(list)
        self.head = defaultdict(int)
        self.queued = self.completed = 0

    def enqueue(self, owner, receiver, amount, timestamp, period, version):
        rows = self.records[owner]
        rows.append(dict(owner=owner, receiver=receiver if version == 'V4' and receiver != owner else ZERO,
                         index=len(rows), initial=amount, pending=amount, paid=0,
                         queued=self.queued, createdAt=timestamp, unbondingPeriod=period))
        self.queued += amount

    def pay(self, owner, receiver, amount, timestamp, version):
        left, touched = amount, 0
        while left:
            i = self.head[owner]
            if i >= len(self.records[owner]):
                raise EvidenceError('completion exceeds known queue')
            row = self.records[owner][i]
            if row['createdAt'] is not None and timestamp is not None and timestamp < row['createdAt'] + row['unbondingPeriod']:
                raise EvidenceError('payment before maturity')
            if version == 'V4' and row['receiver'] != ZERO and row['receiver'] != receiver:
                raise EvidenceError('bound receiver mismatch')
            paid = min(left, row['pending'])
            row['pending'] -= paid
            row['paid'] += paid
            row['queued'] += paid
            left -= paid
            self.completed += paid
            touched += 1
            if row['pending'] == 0:
                self.head[owner] += 1
            if version == 'V4' and left:
                raise EvidenceError('V4 completion spans multiple records')
        return touched

    def remaining(self):
        return sum(r['pending'] for rows in self.records.values() for r in rows)


def finding(check, state, reason, expected=None, actual=None, evidence=None, priority='P0', contract=None):
    difference = actual - expected if isinstance(actual, int) and isinstance(expected, int) else None
    return dict(id=check, severity=priority, check=check, status=state, affectedContract=contract,
                expected=expected, actual=actual, rawDifference=difference,
                supportingEvidence=evidence or [], confidence='PROVEN' if state in ('PASS', 'FAIL') else 'INCOMPLETE',
                reason=reason, reproduction='python3 -m tools.reconcile reconcile',
                recommendedInvestigation='' if state == 'PASS' else reason)


def replay(inv, logs, snapshot=None, states=None, complete=False, identities=None, receipts=None, traces=None, headers=None):
    states, identities = states or {}, identities or {}
    receipts, traces, headers = receipts or {}, traces or {}, headers or {}
    logs = ordered(logs)
    targets = inv['targets']
    byname = {v['name']: a for a, v in targets.items()}
    st, oracle, bridge = (byname[n] for n in ('StPROS', 'Oracle', 'BridgeVault'))
    wpros = inv['addresses']['asset']['WPROS'].lower()
    versions, events, unknown = {}, [], []
    for log in logs:
        addr = log['address'].lower()
        target = targets.get(addr)
        upgrade = decode(log, [UPGRADE])
        if upgrade and target and target['known']:
            implementation = upgrade['args']['implementation'].lower()
            versions[addr] = implementation
            if implementation not in target['known']:
                unknown.append({'reason': 'unknown implementation', 'implementation': implementation, 'log': log})
        imp = versions.get(addr)
        if target and target['known']:
            entry = inv['implementations'].get(imp)
            items = entry['abi'] + PROXY_ABI if entry else PROXY_ABI
            version = entry['version'] if entry else 'UNKNOWN'
        else:
            items = WPROS_ABI if addr == wpros else STAKING_ABI if addr == STAKING else (target or {}).get('abi', [])
            version = 'EXTERNAL'
        try:
            decoded = decode(log, items)
        except (ValueError, KeyError, IndexError):
            decoded = None
        if not decoded:
            unknown.append({'reason': 'unmapped event or version', 'log': log})
            continue
        stamp = headers.get(log['blockNumber'], {}).get('timestamp')
        events.append(dict(decoded, address=addr, version=version,
                           timestamp=number(stamp) if stamp is not None else None, raw=log))

    queue = Queue()
    supply = minted = burned = commission_minted = 0
    deposit_shares = escrow_remints = request_burns = completion_burns = migration_burns = 0
    balances = defaultdict(int)
    t = v = 0
    period = 7 * 86400
    rate = apr = cap = interval = last_update = None
    violations, gaps, rewards, migrations, governance, routing, conversions = [], [], [], [], [], [], []
    unknown_core = [x for x in unknown if x['log']['address'].lower() in (st, oracle, bridge)]
    migration_tx = inv['release']['upgradeTransaction'].lower()
    migration_seen = False
    pool_checks = 0

    def violation(name, expected, actual, e):
        row = finding(name, 'FAIL' if complete and not unknown_core and all(identities.get(x, {}).get('status') == 'PASS' for x in (st, oracle)) else 'UNVERIFIED',
                      'Replay expectation differs from event/state.', expected, actual,
                      [e['raw']], contract=e['address'])
        row['id'] = name + '-' + e['raw']['transactionHash'] + '-' + e['raw']['logIndex']
        violations.append(row)

    def compare(name, expected, actual, e):
        if expected != actual:
            violation(name, expected, actual, e)

    for tx, group in groupby(events, lambda e: e['raw']['transactionHash'].lower()):
        group = list(group)
        pre_supply, pre_escrow, pre_liability = supply, balances[st], queue.remaining()
        migration_burn = 0
        pending_pool = []
        reward_events = [e for e in group if e['address'] == oracle and e['name'] == 'TokenAmountUpdated']
        fees = [e for e in group if e['address'] == oracle and e['name'] == 'CommissionMinted' and e['args']['token'].lower() == wpros]
        used_fees = set()
        for idx, e in enumerate(group):
            a, n, addr, ver = e['args'], e['name'], e['address'], e['version']
            if isinstance(a, dict):
                a = {k: value.lower() if isinstance(value, str) and value.startswith('0x') else value for k, value in a.items()}
                e['args'] = a
            if n in ('GuardianChanged', 'Paused', 'Unpaused', 'MinDelayChange', 'CallScheduled', 'CallExecuted', 'Cancelled', 'UnbondingPeriodChanged', 'MaxWithdrawCountChanged', 'CommissionVTokenSet', 'VTokenSet', 'Upgraded', 'OwnershipTransferred', 'OwnershipTransferStarted', 'RoleGranted', 'RoleRevoked',
                     'RoleAdminChanged', 'SlpChanged', 'BridgeVaultChanged', 'OracleChanged', 'SlpWhitelistUpdated',
                     'KeeperChanged', 'BridgeVaultSet', 'SlpPharosProxySet', 'CommissionConfigChanged',
                     'UpdateLimitsChanged', 'MaxAprPpmChanged', 'EmergencyWithdraw'):
                governance.append(e)
            if addr == st and n == 'OracleChanged' and a['newOracle'] != oracle:
                gaps.append('Historical Oracle routing differs from canonical pool; conversion history needs separate mapping')
            if n in ('Distributed', 'TransferredBack', 'OracleNotified', 'EmergencyWithdraw', 'BridgeVaultSet', 'SlpPharosProxySet'):
                routing.append(e)
            if addr == st:
                if n == 'Transfer':
                    amount, sender, receiver = a['value'], a['from'], a['to']
                    if sender == ZERO:
                        minted += amount
                        supply += amount
                    else:
                        balances[sender] -= amount
                        if balances[sender] < 0:
                            violation('negative-holder-balance', 0, balances[sender], e)
                    if receiver == ZERO:
                        burned += amount
                        supply -= amount
                        if tx == migration_tx and sender == st:
                            migration_burn += amount
                            migration_burns += amount
                    else:
                        balances[receiver] += amount
                elif n == 'UnbondingPeriodChanged':
                    compare('unbonding-prevalue', period, a['oldUnbondingPeriod'], e)
                    period = a['newUnbondingPeriod']
                elif n in ('Deposit', 'Withdraw'):
                    conversions.append(dict(transaction=tx, logIndex=e['raw']['logIndex'], kind=n, version=ver, preT=t, preV=v, assets=a['assets'], shares=a['shares']))
                    if n == 'Deposit':
                        deposit_shares += a['shares']
                    else:
                        request_burns += a['shares']
                        if ver == 'V1':
                            escrow_remints += a['shares']
                    if ver == 'V1':
                        compare('legacy-par-conversion', a['assets'], a['shares'], e)
                    if n == 'Withdraw':
                        if ver == 'V1' and a['assets'] == 0:
                            gaps.append('Legacy zero-amount record makes aggregate completion/head advancement ambiguous; historical queue state required')
                        queue.enqueue(a['owner'], a['receiver'], a['assets'], e['timestamp'], period, ver)
                    if ver == 'V4':
                        if not pending_pool:
                            gaps.append('Deposit/withdraw missing its preceding Oracle PoolUpdated')
                        else:
                            p, before_t, before_v = pending_pool.pop(0)
                            assets, shares = a['assets'], a['shares']
                            # Path rounding is checked from receipt/transaction call evidence separately.
                            nt = before_t + assets if n == 'Deposit' else before_t - assets
                            nv = before_v + shares if n == 'Deposit' else before_v - shares
                            if nv == 0:
                                nt = 0
                            compare('pool-T-event', nt, p['args']['tokenAmount'], p)
                            compare('pool-V-event', nv, p['args']['vTokenAmount'], p)
                            compare('pool-direction', n == 'Deposit', p['args']['isMint'], p)
                            t, v = nt, nv
                            pool_checks += 1
                elif n == 'WithdrawalCompleted':
                    if ver == 'V1':
                        completion_burns += a['tokenAmount']
                    try:
                        queue.pay(a['caller'], a['receiver'], a['tokenAmount'], e['timestamp'], ver)
                    except EvidenceError as exc:
                        violation('queue-payment', None, str(exc), e)
                    if ver == 'V4':
                        payment = next((p for p in reversed(group[:idx]) if p['address'] == bridge and p['name'] == 'TokenWithdrawn'), None)
                        if payment is None:
                            gaps.append('Missing BridgeVault TokenWithdrawn for V4 payment')
                        else:
                            compare('payment-amount', a['tokenAmount'], payment['args']['amount'], e)
                            compare('payment-receiver', a['receiver'], payment['args']['to'].lower(), e)
                elif n == 'CommissionMinted':
                    commission_minted += a['shares']
                    previous = next((x for x in reversed(group[:idx]) if x['address'] == st and x['name'] == 'Transfer'), None)
                    if previous:
                        compare('commission-transfer-origin', ZERO, previous['args']['from'], e)
                        compare('commission-transfer-recipient', a['account'], previous['args']['to'], e)
                        compare('commission-transfer-shares', a['shares'], previous['args']['value'], e)
                    else:
                        gaps.append('Commission mint missing Transfer evidence')
            if addr == oracle:
                if n in ('PoolUpdated', 'TokenAmountUpdated', 'CommissionMinted', 'SetTokenAmount') and isinstance(a, dict) and a.get('token', wpros) != wpros:
                    continue
                if n == 'CommissionConfigChanged':
                    rate = a['ratePpm']
                elif n == 'MaxAprPpmChanged':
                    apr = a['maxAprPpm']
                elif n == 'UpdateLimitsChanged':
                    cap, interval = a['maxUpdateAmount'], a['updateInterval']
                elif n == 'SetTokenAmount':
                    if ver == 'V1':
                        # V1's setter is internal and not exposed by the recorded implementation.
                        gaps.append('Unexpected legacy Oracle SetTokenAmount requires source/transaction investigation')
                    elif tx == migration_tx:
                        compare('migration-pool-T', supply, a['tokenAmount'], e)
                        compare('migration-pool-V', supply, a['vTokenAmount'], e)
                        t, v, last_update = supply, supply, e['timestamp']
                        migration_seen = True
                    else:
                        compare('correction-preserves-V', v, a['vTokenAmount'], e)
                        t = a['tokenAmount']
                        gaps.append('Oracle administrative correction requires calldata/Timelock economic justification')
                elif n == 'PoolUpdated':
                    pending_pool.append((e, t, v))
                    if v == 0 and a['isMint']:
                        last_update = e['timestamp']
                elif n == 'TokenAmountUpdated':
                    reward = a['addedAmount']
                    if None in (rate, cap, interval, apr, last_update, e['timestamp']):
                        gaps.append('Incomplete historical reward limits/timestamps')
                        expected_fee = expected_shares = None
                    else:
                        try:
                            expected_fee, expected_shares = commission(t, v, reward, rate)
                            elapsed = e['timestamp'] - last_update
                            allowance = t * apr * min(max(elapsed, 0), 7 * 86400) // (365 * 86400 * 1_000_000)
                            if reward <= 0 or reward > cap or reward > allowance or elapsed < interval:
                                violation('reward-limits', {'cap': cap, 'aprAllowance': allowance, 'minInterval': interval},
                                          {'reward': reward, 'elapsed': elapsed}, e)
                        except EvidenceError:
                            expected_fee = expected_shares = None
                            violation('reward-zero-supply', 'positive supply', v, e)
                    preceding = [f for f in fees if number(f['raw']['logIndex']) < number(e['raw']['logIndex']) and id(f) not in used_fees]
                    fee_event = preceding[-1] if preceding else None
                    actual_shares = fee_event['args']['vTokenAmount'] if fee_event else 0
                    if fee_event:
                        used_fees.add(id(fee_event))
                    if expected_shares is not None:
                        compare('commission-shares', expected_shares, actual_shares, e)
                        if expected_shares:
                            compare('commission-assets', expected_fee, fee_event['args']['tokenAmount'] if fee_event else None, e)
                    compare('reward-post-T', t + reward, a['newTokenAmount'], e)
                    rewards.append(dict(tx=tx, preT=t, preV=v, reward=reward, fee=expected_fee,
                                        expectedShares=expected_shares, mintedShares=actual_shares,
                                        provenance='UNVERIFIED'))
                    t += reward
                    v += actual_shares
                    last_update = e['timestamp']
        st_fees = sum(e['args']['shares'] for e in group if e['address'] == st and e['name'] == 'CommissionMinted')
        oracle_fees = sum(e['args']['vTokenAmount'] for e in fees)
        if st_fees != oracle_fees:
            violation('commission-cross-contract', oracle_fees, st_fees, group[-1])
        if pending_pool:
            gaps.append('Unpaired Oracle PoolUpdated event')
        if tx == migration_tx:
            compare('migration-escrow-burn', pre_escrow, migration_burn, group[-1])
            compare('migration-supply-transition', pre_supply - pre_escrow, supply, group[-1])
            compare('migration-liability-preserved', pre_liability, queue.remaining(), group[-1])
            migrations.append(dict(transaction=tx, supplyBefore=pre_supply, supplyAfter=supply,
                                   escrowBurn=migration_burn, outstandingLiability=pre_liability,
                                   assetTransition='UNVERIFIED pending historical states and native trace'))

    if not complete:
        gaps.append('Historical scope coverage incomplete')
    if unknown_core:
        gaps.append('Unknown core ABI/implementation/event')
    if not migration_seen:
        gaps.append('Migration initialization not replayed')
    if any(e['timestamp'] is None for e in events if e['name'] in ('Withdraw', 'WithdrawalCompleted')):
        gaps.append('Missing queue block timestamps')
    core_verified = all(identities.get(a, {}).get('status') == 'PASS' for a in (st, oracle, bridge))
    qualified = complete and not gaps and core_verified
    if not core_verified:
        gaps.append('Deployment identities not live-verified')

    def final_check(name, expected, actual, extra=True):
        status = 'BLOCKED' if snapshot is None else 'UNVERIFIED'
        if qualified and extra and actual is not None:
            status = 'PASS' if expected == actual else 'FAIL'
        return finding(name, status, 'Exact integer comparison; evidence qualification required.',
                       expected if snapshot else None, actual, ['cache/snapshots/state.json'],
                       contract=oracle if name.startswith('oracle-') else st)

    checks = [final_check('issuance-paths', deposit_shares + escrow_remints + commission_minted, minted),
              final_check('destruction-paths', request_burns + completion_burns + migration_burns, burned),
              final_check('share-supply', supply, states.get('supply')),
              final_check('oracle-T', t, states.get('T')),
              final_check('oracle-V', v, states.get('V')),
              final_check('oracle-V-vs-supply', states.get('supply'), states.get('V')),
              final_check('queue-cumulative', queue.queued, states.get('queued')),
              final_check('queue-completed', queue.completed, states.get('completed')),
              final_check('queue-liability', queue.remaining(),
                          states['queued'] - states['completed'] if 'queued' in states and 'completed' in states else None)]
    if states.get('frontier') is not None and states.get('bridgeBalance') is not None and states.get('completed') is not None:
        checks.append(final_check('funding-frontier', states['bridgeBalance'] + states['completed'], states['frontier']))
    else:
        checks.append(finding('funding-frontier', 'UNVERIFIED' if snapshot else 'BLOCKED', 'Missing BridgeVault/frontier snapshot evidence.'))
    checks += violations
    for check, reason, priority in [
        ('reward-commission', 'Reward update bounds, integer dilution and commission Transfer attribution are checked separately from reward provenance.', 'P0'),
        ('redemption-payments', 'Event correlations do not prove every native payout; inspect transaction traces and legacy WPROS payments.', 'P0'),
        ('asset-backing', 'Verified Staking interface/source and disjoint pending/active/unbonding/claimable positions unavailable.', 'P0'),
        ('reward-provenance', 'RewardsVault reports its entire balance; source classification requires native movement history, including donations/principal.', 'P0'),
        ('unexpected-value-movements', 'Targeted traces cannot detect every unsolicited transfer in transactions without protocol events.', 'P0'),
        ('governance-authority', 'Recorded role changes require complete historical discovery, current role enumeration and authorization review.', 'P1'),
        ('slp-routing', 'SLP/RewardsVault deployment-source identity and complete historical administrator/routing attestations unavailable.', 'P1')]:
        state = 'UNVERIFIED' if snapshot else 'BLOCKED'
        if check == 'reward-commission' and qualified:
            state = 'FAIL' if any(x['id'].startswith(('reward-', 'commission-')) for x in violations) else 'PASS'
        checks.append(finding(check, state, reason, priority=priority))
    for e in routing:
        if e['name'] == 'EmergencyWithdraw':
            checks.append(finding('emergency-asset-movement', 'UNVERIFIED', 'Emergency withdrawal observed; establish authorized purpose and economic effect.',
                                  actual=e['args'], evidence=[e['raw']], contract=e['address']))
    for owner, rows in queue.records.items():
        observed = states.get('queues', {}).get(owner)
        expected = [[r['queued'], r['pending'], r['createdAt'], r['unbondingPeriod'], int(r['receiver'], 16)]
                    for r in rows[queue.head[owner]:]]
        checks.append(final_check('owner-queue-' + owner, expected, observed))
        cursors = states.get('queueCursors', {}).get(owner, {})
        checks.append(final_check('owner-head-' + owner, queue.head[owner], cursors.get('head')))
        checks.append(final_check('owner-tail-' + owner, len(rows), cursors.get('tail')))
        now = headers.get(hex(snapshot['number']), {}).get('timestamp') if snapshot else None
        if now is not None and states.get('frontier') is not None and all(r['createdAt'] is not None for r in rows):
            amount = full = partial = 0
            for row in rows[queue.head[owner]:]:
                if number(now) < row['createdAt'] + row['unbondingPeriod'] or states['frontier'] <= row['queued']:
                    break
                available = min(row['pending'], states['frontier'] - row['queued'])
                amount += available
                if available < row['pending']:
                    partial = available
                    break
                full += 1
            checks.append(final_check('owner-claimability-' + owner, [amount, full, partial], cursors.get('claimable')))
    for owner, balance in sorted(balances.items()):
        checks.append(final_check('holder-' + owner, balance, states.get('holders', {}).get(owner)))
    return {'checks': checks, 'replay': dict(supply=supply, minted=minted, burned=burned,
            commissionMinted=commission_minted, depositShares=deposit_shares, legacyEscrowRemints=escrow_remints,
            requestBurns=request_burns, legacyCompletionBurns=completion_burns, migrationBurns=migration_burns, balances=dict(balances), T=t, V=v,
            queued=queue.queued, completed=queue.completed, liability=queue.remaining(),
            queues=dict(queue.records), queueHeads=dict(queue.head), rewards=rewards,
            migrations=migrations, poolComparisons=pool_checks),
            'externalAccountingEvents': [e for e in events if e['address'] not in (st, oracle) and e['name'] in ('EthDeposited', 'OracleNotified', 'RewardClaimed', 'CoinMinted', 'DelegatorWithdrawClaimed', 'StakeClaimed', 'Bonded', 'Unbonded', 'Claimed', 'TransferredBack', 'Distributed')],
            'conversions': conversions,
            'payments': [e for e in events if e['address'] == st and e['name'] == 'WithdrawalCompleted'],
            'unknownEvents': unknown, 'governance': governance, 'routing': routing,
            'evidenceGaps': sorted(set(gaps)), 'snapshot': snapshot,
            'coverageConclusion': 'INCOMPLETE; no solvency conclusion',
            'cashObservations': states.get('custody', {}),
            'receiptCount': len(receipts), 'traceCount': len(traces)}
