"""Native trace evidence, separate from event accounting and solvency conclusions."""
from .inventory import STAKING, number
from .replay import finding


def native_movements(trace):
    """Ignore delegated value metadata and every reverted subtree (no double counting)."""
    result = []
    def visit(node, path=()):
        if node.get('error'):
            return
        kind = node.get('type', '').upper()
        value = number(node.get('value', '0x0'))
        if value and kind in ('CALL', 'CREATE', 'CREATE2', 'SELFDESTRUCT'):
            result.append({'from': node.get('from', '').lower(), 'to': node.get('to', '').lower(),
                           'value': value, 'type': kind, 'tracePath': list(path)})
        for i, child in enumerate(node.get('calls', [])):
            visit(child, path + (i,))
    visit(trace)
    return result


def wpros_unwrap_evidence(inv, tx, trace, receipt, moves, headers, wrapped_identity):
    """One Withdrawal consumes one successful withdraw-call payout, within a pinned receipt.

    Forwarding is only an observed later action in the external router's same CALL
    frame. Equal amounts do not prove exclusive funding, ownership or authorization.
    """
    from tools.lib import abi
    from .inventory import WPROS_ABI, decode
    wpros = inv['addresses']['asset']['WPROS'].lower()
    annotations, issues, events = {}, [], {}
    duplicate = False
    for row in receipt.get('logs', []):
        if row.get('address', '').lower() != wpros:
            continue
        event = decode(row, WPROS_ABI)
        if not event or event['name'] != 'Withdrawal':
            continue
        index = number(row['logIndex'])
        duplicate |= index in events
        events[index] = (row, event['args'])
    if not events:
        return annotations, issues
    bound = (wrapped_identity and not duplicate and number(receipt.get('status', 0)) == 1 and
             receipt.get('transactionHash', '').lower() == tx.lower() and receipt.get('blockHash') and
             headers.get(hex(number(receipt['blockNumber'])), {}).get('hash') == receipt['blockHash'] and
             bool(receipt.get('from')) and bool(receipt.get('to')) and
             trace.get('from', '').lower() == receipt['from'].lower() and
             trace.get('to', '').lower() == receipt['to'].lower() and not trace.get('error'))

    def node(path):
        value = trace
        for index in path:
            value = value['calls'][index]
        return value

    for index, (row, args) in sorted(events.items()):
        src, amount = args['src'].lower(), args['wad']
        event_bound = (bound and not row.get('removed') and
                       row.get('transactionHash', '').lower() == tx.lower() and
                       row.get('blockHash') == receipt['blockHash'] and
                       number(row['blockNumber']) == number(receipt['blockNumber']))
        match = None
        for move in moves if event_bound else []:
            path = tuple(move['tracePath'])
            if path in annotations or not path or move['type'] != 'CALL' or (move['from'], move['to'], move['value']) != (wpros, src, amount):
                continue
            parent = node(path[:-1])
            if (parent.get('type', '').upper() == 'CALL' and parent.get('from', '').lower() == src and
                    parent.get('to', '').lower() == wpros and
                    parent.get('input', '').lower() == abi.calldata('withdraw(uint256)', amount)):
                match = move
                break
        if match is None:
            issues.append(finding('wpros-unwrap-unverified-' + tx + '-' + str(index), 'UNVERIFIED',
                'WPROS Withdrawal lacks a unique successful matching payout bound to the receipt, block header and withdraw call.',
                actual={'src': src, 'wei': amount}, evidence=[tx, row], contract=wpros))
            continue
        path = tuple(match['tracePath'])
        annotations[path] = {'classification': 'Verified WPROS unwrap movement',
                             'withdrawalLogIndex': index, 'unwrapStatus': 'PASS',
                             'economicAuthorization': 'UNVERIFIED'}
        # Do not reclassify protocol outflows as router forwarding or conceal payment gaps.
        if src in inv['targets'] or src in (wpros, STAKING) or len(path) < 2:
            continue
        frame_path = path[:-2]
        frame = node(frame_path)
        if frame.get('type', '').upper() != 'CALL' or frame.get('to', '').lower() != src:
            continue
        for forwarded in moves:
            forward_path = tuple(forwarded['tracePath'])
            if (forward_path and forward_path not in annotations and forward_path[:-1] == frame_path and
                    forward_path[-1] > path[-2] and forwarded['type'] == 'CALL' and
                    (forwarded['from'], forwarded['to'], forwarded['value']) == (src, receipt['from'].lower(), amount)):
                annotations[forward_path] = {
                    'classification': 'Observed external routing transfer to transaction sender after WPROS unwrap',
                    'relatedUnwrapTracePath': list(path), 'withdrawalLogIndex': index,
                    'fundingProvenance': 'UNVERIFIED; equal amounts do not prove exclusive funding',
                    'economicAuthorization': 'UNVERIFIED'}
                break
    return annotations, issues


def assess(inv, traces, receipts, replay_result, migration_states=None, logs=None, wrapped_identity=False, headers=None):
    known = set(inv['targets']) | {inv['addresses']['asset']['WPROS'].lower(), STAKING}
    movements, findings = [], []
    st = inv['addresses']['proxies']['StPROS'].lower()
    payouts = {(e['raw']['transactionHash'].lower(), (e['args']['caller'].lower() if e['version'] == 'V1' and e['args']['receiver'].lower() == st else e['args']['receiver'].lower()), e['args']['tokenAmount'])
               for e in replay_result.get('payments', [])}
    for tx, trace in sorted(traces.items()):
        if tx not in receipts:
            findings.append(finding('trace-without-receipt-' + tx, 'UNVERIFIED',
                                    'Trace cannot be bound to a successful pinned receipt.', evidence=[tx]))
            continue
        native = native_movements(trace)
        unwraps, unwrap_issues = wpros_unwrap_evidence(inv, tx, trace, receipts[tx], native, headers or {}, wrapped_identity)
        findings.extend(unwrap_issues)
        for move in native:
            attribution = unwraps.get(tuple(move['tracePath']))
            if move['from'] not in known and move['to'] not in known and not attribution:
                continue
            move = dict(move, transaction=tx, block=receipts[tx]['blockNumber'],
                        classification='UNKNOWN economic purpose')
            if attribution:
                move.update(attribution)
            elif move['from'] in known and move['to'] in known:
                move['classification'] = 'Observed protocol/external-system transfer; economic classification pending'
            elif move['to'] in known:
                move['classification'] = 'Observed external inflow; deposit/donation/principal/reward unresolved'
            else:
                if (tx, move['to'], move['value']) in payouts:
                    move['classification'] = 'Observed redemption payout correlated with completion event'
                else:
                    move['classification'] = 'Observed external outflow; payout/authorized routing unresolved'
                    findings.append(finding('native-unclassified-' + tx + '-' + '.'.join(map(str, move['tracePath'])),
                        'UNVERIFIED', 'Protocol native outflow lacks an identified redemption counterpart; verify recipient authority and economic purpose.',
                        actual={'recipient': move['to'], 'wei': move['value']}, evidence=[tx, move], contract=move['from']))
            movements.append(move)
    migration_tx = inv['release']['upgradeTransaction'].lower()
    migration = next((m for m in replay_result['replay']['migrations'] if m['transaction'] == migration_tx), None)
    if migration:
        st = inv['addresses']['proxies']['StPROS'].lower()
        bridge = inv['addresses']['proxies']['BridgeVault'].lower()
        distributor = inv['addresses']['proxies']['SlpDistributor'].lower()
        sent = {(st, bridge): 0, (st, distributor): 0}
        for move in movements:
            pair = (move['from'], move['to'])
            if move['transaction'] == migration_tx and pair in sent:
                sent[pair] += move['value']
        actual = sent[(st, bridge)]
        expected = migration['outstandingLiability']
        supported = migration_tx in traces and migration_tx in receipts and replay_result.get('collectionComplete') and all(replay_result.get('deploymentIdentity', {}).get(a, {}).get('status') == 'PASS' for a in (st, bridge))
        findings.append(finding('migration-native-reserve', ('PASS' if actual == expected else 'FAIL') if supported else 'UNVERIFIED',
                                'Migration native reserve from successful call trace compared with reconstructed unpaid claims.',
                                expected, actual, [migration_tx], contract=bridge))
        if migration_states and 'beforeWrapped' in migration_states and 'afterWrapped' in migration_states:
            expected = migration_states['beforeWrapped'] - migration['outstandingLiability']
            actual = sent[(st, distributor)]
            from .inventory import decode, WPROS_ABI
            wpros = inv['addresses']['asset']['WPROS'].lower()
            block = receipts.get(migration_tx, {}).get('blockNumber')
            wrapped_logs = [l for l in (logs or []) if l['address'].lower() == wpros and l['blockNumber'] == block]
            migration_wrapped = [decode(l, WPROS_ABI) for l in wrapped_logs if l['transactionHash'].lower() == migration_tx]
            isolated = bool(migration_wrapped) and all(l['transactionHash'].lower() == migration_tx for l in wrapped_logs)
            isolated = isolated and all(e and e['name'] == 'Withdrawal' and e['args']['src'].lower() == st for e in migration_wrapped)
            isolated = isolated and sum(e['args']['wad'] for e in migration_wrapped) == migration_states['beforeWrapped']
            isolated = isolated and not any(c['transaction'] == migration_tx for c in replay_result.get('conversions', []))
            status = 'UNVERIFIED'
            if supported and wrapped_identity and isolated:
                status = 'PASS' if actual == expected and migration_states['afterWrapped'] == 0 else 'FAIL'
            findings.append(finding('migration-active-assets', status,
                                    'Pinned wrapped balances and native trace; qualification requires verified WPROS, isolated migration-block withdrawals and no ordinary conversions in the migration transaction.',
                                    expected, actual, [migration_tx], contract=distributor))
    cash = replay_result.get('cashObservations', {})
    # Cash alone is neither attributable backing nor full asset coverage.
    custody_total = sum(row.get('native', 0) + row.get('WPROS', 0) for row in cash.values()
                        if 'ProxyAdmin' not in row.get('name', '') and row.get('name') != 'TimelockController')
    return {'movements': movements, 'checks': findings, 'observedCustodyTotal': custody_total if cash else None,
            'verifiedAttributableAssets': None, 'stakingPrincipal': None, 'economicCoverageDifference': None,
            'status': 'UNVERIFIED', 'reason': 'Unknown provenance and unverified external staking positions prevent coverage certification.'}


def payment_evidence(inv, result, receipts, traces):
    """Consume individual payout movements once; an event is never its own payment proof."""
    from tools.lib import abi
    from .inventory import TRANSFER, decode
    st = inv['addresses']['proxies']['StPROS'].lower()
    bridge = inv['addresses']['proxies']['BridgeVault'].lower()
    wpros = inv['addresses']['asset']['WPROS'].lower()
    available = {}
    for tx, receipt in receipts.items():
        rows = []
        for log in receipt.get('logs', []):
            if log['address'].lower() == wpros:
                e = decode(log, [TRANSFER])
                if e:
                    a = e['args']
                    rows.append((a['from'].lower(), a['to'].lower(), a['value'], 'wrapped'))
        for move in native_movements(traces.get(tx, {})):
            rows.append((move['from'], move['to'], move['value'], 'native'))
        available[tx] = rows
    verified, missing = [], []
    for event in result['payments']:
        tx = event['raw']['transactionHash'].lower()
        a = event['args']
        payer = st if event['version'] == 'V1' else bridge
        receiver = a['receiver'].lower()
        if event['version'] == 'V1' and receiver == st:
            receiver = a['caller'].lower()
            modes = ('native',)
        else:
            modes = ('wrapped', 'native') if event['version'] == 'V4' else ('wrapped',)
        wanted = [(payer, receiver, a['tokenAmount'], mode) for mode in modes]
        match = next((x for x in wanted if x in available.get(tx, [])), None)
        if match:
            available[tx].remove(match)
            verified.append({'transaction': tx, 'amount': a['tokenAmount'], 'receiver': receiver, 'mode': match[-1]})
        else:
            missing.append({'transaction': tx, 'logIndex': event['raw']['logIndex'], 'amount': a['tokenAmount']})
    return {'verified': verified, 'missing': missing,
            'status': 'PASS' if result.get('collectionComplete') and not missing and result.get('deploymentIdentity', {}).get(st, {}).get('status') == 'PASS' and not any(e['log']['address'].lower() == st for e in result.get('unknownEvents', [])) else 'UNVERIFIED'}


def provisional_coverage(custody, external, state):
    """Expose assumptions and distinct buckets; never promote provisional source ABI to solvency proof."""
    cash = sum(r.get('native', 0) + r.get('WPROS', 0) for r in custody.values()
               if 'ProxyAdmin' not in r.get('name', '') and r.get('name') != 'TimelockController')
    active = pending = exiting = principal = unclaimed = 0
    for position in external.get('positions', {}).values():
        for row in position.get('pools', []):
            active += row['activeStakeIncludingCompoundedRewards']
            pending += row['pendingStakeIncludingCompoundedRewards']
            principal += row['principalStake']
            unclaimed += row.get('unclaimedRewards') or 0
        for row in position.get('pendingUndelegations', []):
            if not row['processed']:
                exiting += row['amount']
                principal += row['principalAmount']
    liability = state.get('T', 0) + state.get('queued', 0) - state.get('completed', 0)
    observed = bool(external.get('positions')) and bool(custody) and not external.get('errors')
    return {'status': 'UNVERIFIED', 'cash': cash if custody else None,
            'activeStakeIncludingCompoundedRewards': active if observed else None,
            'pendingStakeIncludingCompoundedRewards': pending if observed else None,
            'unbondingOrClaimableIncludingRewards': exiting if observed else None,
            'principalMemoOnlyNotAddedAgain': principal if observed else None,
            'unclaimedRewardsMemoOnlyNotAdded': unclaimed if observed else None,
            'oracleAssetsPlusUnpaidLiability': liability if 'T' in state else None,
            'provisionalGrossAssets': cash + active + pending + exiting if observed else None,
            'provisionalDifference': cash + active + pending + exiting - liability if observed and 'T' in state else None,
            'assumptions': ['External code/source mapping UNVERIFIED; returned ABI shape alone is insufficient.',
                            'Cash sources are unclassified; RewardsVault balance may include unrelated principal or donations.',
                            'Active/pending includes compounded rewards; unbonding amount includes its own reward component.',
                            'principalStake and pendingWithdrawStake are not added as separate assets.',
                            'Unclaimed rewards are a memo item because realization and reporting provenance are not established.']}
