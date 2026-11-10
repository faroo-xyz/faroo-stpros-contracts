"""Pinned, cache-first P0 closeout using authenticated external deployment records.

This extends the existing replay, preserves its original outputs, and never invokes RPC,
Foundry, shell startup files or a deployment framework. New targeted reads are separate,
explicitly justified cache evidence; absence of them remains UNVERIFIED.
"""
import copy
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from tools.lib import abi
from tools.lib.keccak import keccak256, selector
from .attestation import authenticate, SNAPSHOT
from .assets import native_movements, provisional_coverage
from .collect import cached_logs
from .external import STAKING_ABI
from .governance import checks as governance_checks
from .inventory import decode, STAKING, number, source_binding
from .replay import replay, finding
from .storage import EvidenceError, canonical, digest, read, safe, write

WHITELIST = keccak256(b'WHITELIST_ROLE')


def lower(value):
    if isinstance(value, str) and value.startswith('0x'):
        return value.lower()
    if isinstance(value, list):
        return [lower(v) for v in value]
    if isinstance(value, dict):
        return {k: lower(v) for k, v in value.items()}
    return value


def consume_credit(credits, amount):
    """Consume each observed native reward credit once; excess is not silently relabelled."""
    if amount < 0 or amount > sum(lot['remaining'] for lot in credits):
        raise EvidenceError('forward exceeds unspent observed reward credits')
    links = []
    for lot in credits:
        used = min(amount, lot['remaining'])
        if used:
            links.append({'claimTransaction': lot['transaction'], 'amount': used})
            lot['remaining'] -= used
            amount -= used
        if not amount:
            break
    return links


def walk_successful(node):
    if node.get('error'):
        return
    yield node
    for child in node.get('calls', []):
        yield from walk_successful(child)


def external_events(logs, attestation):
    events = []
    for log in logs:
        address = log['address'].lower()
        if address in attestation['proxies']:
            items = attestation['proxies'][address]['abi']
        elif address == STAKING:
            items = STAKING_ABI  # Observational schema; NOT a staking runtime attestation.
        else:
            continue
        event = decode(log, items)
        if event:
            events.append(dict(event, args=lower(event['args']), address=address, raw=log))
    return events


def receipt_and_case(store, tx, case, participants):
    receipt = store.get(f'cache/receipts/{tx}.json')
    transaction = store.get(f'cache/receipts/{tx}-transaction.json')
    trace = store.get(f'cache/traces/{tx}.json')
    if not receipt or not transaction or not trace or not case:
        raise EvidenceError('required transaction/balance/trace evidence missing')
    if number(receipt['status']) != 1 or trace.get('error'):
        raise EvidenceError('transaction/trace not successful')
    if receipt.get('transactionHash', '').lower() != tx or transaction.get('hash', '').lower() != tx:
        raise EvidenceError('receipt/transaction hash mismatch')
    header = store.get('cache/snapshots/headers.json', {}).get(hex(case['block']))
    if not header or header['hash'] != case['blockHash'] or header['parentHash'] != case.get('beforeBlockHash'):
        raise EvidenceError('native balance boundaries are not consecutive canonical blocks')
    if receipt['blockHash'] != case['blockHash'] or transaction['blockHash'] != case['blockHash']:
        raise EvidenceError('reward case not bound to its transaction block')
    if number(receipt['blockNumber']) != case['block']:
        raise EvidenceError('reward case block number mismatch')
    if lower(trace.get('to')) != lower(transaction.get('to')) or trace.get('input', '').lower() != transaction['input'].lower():
        raise EvidenceError('trace root differs from transaction')
    for address in participants:
        if address not in case['balances']:
            raise EvidenceError('participant boundary balance missing')
    return transaction, trace


def reward_ledger(store, inv, attestation, events, logs):
    """Verify real credits, native forwarding, whole-balance reporting and reward restaking.

    PASS for a cash-flow step does not certify the underlying reward-generation formula.
    """
    snap = store.get('checkpoints/snapshot.json')
    evidence = store.get('cache/snapshots/reward-evidence.json', {})
    if evidence.get('snapshot') != snap:
        raise EvidenceError('reward evidence snapshot mismatch')
    slps = {a for a, p in attestation['proxies'].items() if p['name'].startswith('SlpPharosProxy')}
    vaults = {a for a, p in attestation['proxies'].items() if 'RewardsVault' in p['name']}
    by_tx = defaultdict(list)
    block_txs = defaultdict(set)
    for event in events:
        by_tx[event['raw']['transactionHash'].lower()].append(event)
        block_txs[number(event['raw']['blockNumber'])].add(event['raw']['transactionHash'].lower())
    roles, credits, cash = defaultdict(set), defaultdict(list), defaultdict(int)
    claimed, transferred, bonded = defaultdict(int), defaultdict(int), defaultdict(int)
    rows, issues, notifications = [], [], []
    vault_liabilities = defaultdict(int)
    for tx, group in by_tx.items():
        # All relevant reward entrypoints in this fixed history are direct operator calls.
        claims = [e for e in group if e['address'] in slps and e['name'] == 'RewardClaimed']
        forwards = [e for e in group if e['address'] in slps and e['name'] == 'TransferredToRewardsVault']
        notifies = [e for e in group if e['address'] in vaults and e['name'] == 'OracleNotified']
        block = number(group[0]['raw']['blockNumber'])
        case = evidence.get('cases', {}).get(tx)
        try:
            if claims:
                actors = {e['address'] for e in claims}
                if len(actors) != 1:
                    raise EvidenceError('multi-SLP claim needs explicit attribution')
                slp = next(iter(actors)); amount = sum(e['args']['amount'] for e in claims)
                transaction, trace = receipt_and_case(store, tx, case, [slp])
                if lower(transaction['to']) != slp or lower(transaction['from']) not in roles[(slp, WHITELIST)]:
                    raise EvidenceError('historical claim caller is not the reconstructed operator')
                decoded = abi.decode_function(attestation['proxies'][slp]['abi'], transaction['input'])
                if decoded['functionName'] not in ('claimReward', 'batchClaimReward'):
                    raise EvidenceError('claim calldata does not use authenticated reward entrypoint')
                underlying = [e for e in group if e['address'] == STAKING and e['name'] == 'RewardClaimed' and e['args']['delegator'] == slp]
                expected = sorted((e['args']['poolId'], e['args']['amount']) for e in claims)
                if sorted((e['args']['poolId'], e['args']['amount']) for e in underlying) != expected:
                    raise EvidenceError('per-pool reward event correspondence mismatch')
                minted = sum(e['args']['amount'] for e in group if e['address'] == STAKING and e['name'] == 'CoinMinted' and e['args']['beneficiary'] == slp and e['args']['reason'] == 1)
                if minted != amount:
                    raise EvidenceError('chain-mint event amount mismatch')
                calls = [n for n in walk_successful(trace) if n.get('type') == 'CALL' and lower(n.get('to')) == STAKING and n.get('input', '').startswith(selector('claimReward(bytes32)'))]
                returned = sorted(('0x' + n['input'][10:74].lower(), abi.dec_uint(n['output'])) for n in calls)
                if returned != expected:
                    raise EvidenceError('actual staking call outputs differ from SLP rewards')
                balance = case['balances'][slp]
                moves = native_movements(trace)
                evm_delta = sum(m['value'] for m in moves if m['to'] == slp) - sum(m['value'] for m in moves if m['from'] == slp)
                if block_txs[block] != {tx}:
                    raise EvidenceError('block-level balance evidence contains multiple relevant transactions')
                if balance['after'] - balance['before'] != amount or evm_delta != 0:
                    raise EvidenceError('post-transaction native credit is not isolated from EVM transfers')
                if cash[slp] != balance['before']:
                    raise EvidenceError('SLP cash replay does not reach claim pre-balance')
                credits[slp].append({'transaction': tx, 'amount': amount, 'remaining': amount})
                cash[slp] += amount; claimed[slp] += amount
                rows.append({'kind': 'claim-native-credit', 'transaction': tx, 'block': block, 'slp': slp,
                             'amount': amount, 'pools': len(claims), 'poolRewards': [{'poolId':p,'amount':v} for p,v in expected],
                             'caller':lower(transaction['from']), 'stakingContract':STAKING, 'balances': balance, 'status': 'PASS',
                             'earnedRewardSemantics': 'UNVERIFIED: staking implementation/client formula not authenticated'})
            for event in forwards:
                slp = event['address']; amount = event['args']['amount']
                destination = lower(attestation['proxies'][slp]['initialization']['args'][3])
                transaction, trace = receipt_and_case(store, tx, case, [slp, destination])
                decoded = abi.decode_function(attestation['proxies'][slp]['abi'], transaction['input'])
                if lower(transaction['to']) != slp or decoded['functionName'] != 'transferToRewardsVault' or decoded['args'] != [amount]:
                    raise EvidenceError('forward calldata/amount mismatch')
                if lower(transaction['from']) not in roles[(slp, WHITELIST)]:
                    raise EvidenceError('forward caller not authorized by historical roles')
                incoming = [e for e in group if e['address'] == destination and e['name'] == 'EthDeposited']
                if len(incoming) != 1 or incoming[0]['args'] != {'sender': slp, 'amount': amount}:
                    raise EvidenceError('RewardsVault deposit mismatch')
                moves = [m for m in native_movements(trace) if m['from'] == slp and m['to'] == destination]
                if sum(m['value'] for m in moves) != amount:
                    raise EvidenceError('native forwarding trace mismatch')
                b, v = case['balances'][slp], case['balances'][destination]
                if block_txs[block] != {tx} or b['before'] - b['after'] != amount or v['after'] - v['before'] != amount:
                    raise EvidenceError('forwarding balance boundaries are incomplete or inconsistent')
                if cash[slp] != b['before'] or cash[destination] != v['before']:
                    raise EvidenceError('cash ledger does not reach forwarding pre-balances')
                links = consume_credit(credits[slp], amount)
                cash[slp] -= amount; transferred[slp] += amount
                rows.append({'kind': 'forward', 'transaction': tx, 'block': block, 'slp': slp,
                             'destination': destination, 'caller':lower(transaction['from']), 'amount': amount, 'sourceLots': links,
                             'sourceBalanceAfter': b['after'], 'status': 'PASS'})
            for event in notifies:
                vault = event['address']; amount = event['args']['amount']
                transaction, trace = receipt_and_case(store, tx, case, [vault])
                decoded = abi.decode_function(attestation['proxies'][vault]['abi'], transaction['input'])
                if lower(transaction['to']) != vault or decoded['functionName'] != 'notifyOracle' or lower(transaction['from']) not in roles[(vault, WHITELIST)]:
                    raise EvidenceError('notification calldata/caller mismatch')
                init = lower(attestation['proxies'][vault]['initialization']['args'])
                oracle_calls = [n for n in walk_successful(trace) if n.get('type') == 'CALL' and lower(n.get('from')) == vault and lower(n.get('to')) == init[3] and n.get('input', '').lower() == abi.calldata('update(address,uint256)', init[2], amount)]
                if len(oracle_calls) != 1:
                    raise EvidenceError('notification Oracle update target/asset/amount mismatch')
                b = case['balances'][vault]
                if cash[vault] != b['before'] or amount != b['before'] or b['after'] != 0:
                    raise EvidenceError('notification did not consume the expected entire vault balance')
                recipients = [m for m in native_movements(trace) if m['from'] == vault and m['value']]
                if sum(m['value'] for m in recipients) != amount or len(recipients) != 1:
                    raise EvidenceError('notification forwarding trace mismatch')
                target = recipients[0]['to']
                if target not in slps or case['balances'].get(target, {}).get('difference') != amount:
                    raise EvidenceError('reported funds not received for restaking')
                cash[vault] -= amount
                notifications.append({'transaction': tx, 'amount': amount, 'returnedTo': target, 'block': block})
                rows.append(dict(notifications[-1], kind='notify-and-return', status='PASS'))
        except (EvidenceError, KeyError, ValueError) as exc:
            issues.append({'transaction': tx, 'status': 'UNVERIFIED', 'reason': str(exc)})
        # Native receive events are counted once; claim CoinMinted credits bypass receive().
        for event in group:
            address, name, args = event['address'], event['name'], event['args']
            if name == 'EthDeposited':
                cash[address] += args['amount']
                if address in vaults:
                    vault_liabilities[address] += args['amount']
                    if args['sender'] not in slps:
                        issues.append({'transaction': tx, 'status': 'UNVERIFIED', 'reason': 'Non-SLP donation/unrelated vault inflow'})
            elif name == 'Bonded' and address in slps:
                cash[address] -= args['amount']; bonded[address] += args['amount']
            elif name == 'TransferredBack' and address in slps:
                cash[address] -= args['amount']
            elif name == 'RoleGranted':
                roles[(address, args['role'])].add(args['account'])
            elif name == 'RoleRevoked':
                roles[(address, args['role'])].discard(args['account'])
    custody = store.get('cache/snapshots/state.json')['custody']
    final_cash = {a: {'replayed': cash[a], 'observed': custody[a]['native'],
                      'difference': custody[a]['native'] - cash[a]} for a in sorted(slps | vaults)}
    if any(r['difference'] for r in final_cash.values()):
        issues.append({'status': 'UNVERIFIED', 'reason': 'Cash replay residue requires provenance investigation'})
    counter_evidence = store.get('cache/snapshots/p0-closeout-counter-reads.json', {})
    counters = []
    for address in sorted(slps):
        for sig, expected in [('totalRewardClaimed()', claimed[address]), ('totalRewardTransferred()', transferred[address])]:
            observed = counter_evidence.get('reads', {}).get(address + ':' + sig, {}).get('value') if counter_evidence.get('snapshot') == snap else None
            counters.append({'address': address, 'getter': sig, 'expected': expected, 'actual': observed,
                             'status': 'UNVERIFIED' if observed is None else 'PASS' if expected == observed else 'FAIL'})
    # Match the known reported-yield return to the subsequent mixed cash restaking transaction.
    restaking = store.get('cache/snapshots/p0-closeout-restaking.json')
    restake_result = {'status': 'UNVERIFIED', 'reason': 'Targeted restaking boundary evidence missing'}
    if restaking and restaking.get('snapshot') == snap:
        tx = restaking['transaction']; group = by_tx.get(tx, [])
        amount = sum(e['args']['amount'] for e in group if e['name'] == 'Bonded')
        trace = store.get(f'cache/traces/{tx}.json')
        if trace:
            case = {'block': restaking['block'], 'blockHash': restaking['blockHash'], 'beforeBlockHash':restaking['beforeBlockHash'],
                    'balances': {restaking['account']: {'before': restaking['before'], 'after': restaking['after']}}}
            transaction, trace = receipt_and_case(store, tx, case, [restaking['account']])
            decoded = abi.decode_function(attestation['proxies'][restaking['account']]['abi'], transaction['input'])
            if lower(transaction['to']) != restaking['account'] or decoded['functionName'] != 'batchBond' or sum(decoded['args'][1]) != amount:
                raise EvidenceError('restaking calldata does not match observed delegation amounts')
            native = sum(m['value'] for m in native_movements(trace) if m['from'] == restaking['account'] and m['to'] == STAKING)
            delegated = sum(e['args']['amount'] for e in group if e['address'] == STAKING and e['name'] == 'StakeDelegated')
            reported = sum(n['amount'] for n in notifications if n['block'] < restaking['block'])
            restake_result = {'transaction': tx, 'status': 'PASS' if native == amount == delegated == restaking['before'] and restaking['after'] == 0 and amount >= reported else 'FAIL',
                'nativeDelegated': native, 'reportedRewardComponent': reported, 'otherPrincipalComponent': amount-reported,
                'note': 'Reported reward component is included in delegated stake, never added a second time to assets.'}
    return {'cashFlowStatus': 'PASS' if not issues and rows else 'UNVERIFIED',
            'generationStatus': 'UNVERIFIED', 'rows': rows, 'issues': issues, 'counters': counters,
            'finalNativeCash': final_cash, 'claimed': dict(claimed), 'forwarded': dict(transferred),
            'bonded': dict(bonded), 'notified': notifications, 'restaking': restake_result,
            'remainingSourceCredits': {a:sum(l['remaining'] for l in values) for a,values in credits.items()},
            'principalGuard': {'enforced': False, 'reason': 'Authenticated transferToRewardsVault checks role and available balance, not amount <= claimed - transferred. Principal/donations can be forwarded by an operator.'}}


def routing_evidence(attestation, external, events):
    results = []
    for address, proxy in attestation['proxies'].items():
        init = lower(proxy['initialization']['args'])
        if proxy['name'].startswith('SlpPharosProxy'):
            expected = init[2]
            for e in events:
                if e['address'] == address and e['name'] == 'BridgeVaultSet':
                    expected = e['args']['bridgeVault']
            raw = external['reads'].get(address + ':getBridgeVault():')
            actual = abi.dec_addr(raw) if raw else None
            results.append({'address': address, 'view': 'getBridgeVault', 'expected': expected, 'actual': actual})
            raw = external['reads'].get(address + ':getRewardsVault():')
            results.append({'address': address, 'view': 'getRewardsVault', 'expected': init[3], 'actual': abi.dec_addr(raw) if raw else None})
        else:
            expected = '0x' + '00'*20
            for e in events:
                if e['address'] == address and e['name'] == 'SlpPharosProxySet':
                    expected = e['args']['slpPharosProxy']
            raw = external['reads'].get(address + ':getSlpPharosProxy():')
            results.append({'address': address, 'view': 'getSlpPharosProxy', 'expected': expected, 'actual': abi.dec_addr(raw) if raw else None})
    for row in results:
        row['status'] = 'UNVERIFIED' if row['actual'] is None else 'PASS' if row['actual'] == row['expected'] else 'FAIL'
    return results


def decode_safe_batch(calldata):
    """Read cached Safe/MultiSend calldata, never create or execute an operation."""
    signature = 'execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)'
    if calldata[:10] != selector(signature):
        raise EvidenceError('not a Safe execTransaction input')
    types = ['address','uint256','bytes','uint8','uint256','uint256','uint256','address','address','bytes']
    args = abi.decode_parameters([{'type':t} for t in types], '0x'+calldata[10:])
    if args[1] != 0 or args[3] != 1 or args[2][:10] != selector('multiSend(bytes)'):
        raise EvidenceError('unexpected Safe batch envelope')
    packed = bytes.fromhex(abi.decode_parameters([{'type':'bytes'}], '0x'+args[2][10:])[0][2:])
    calls = []; offset = 0
    while offset < len(packed):
        if len(packed)-offset < 85:
            raise EvidenceError('truncated MultiSend entry')
        operation = packed[offset]; target = '0x'+packed[offset+1:offset+21].hex()
        value = int.from_bytes(packed[offset+21:offset+53], 'big')
        length = int.from_bytes(packed[offset+53:offset+85], 'big'); offset += 85
        if operation != 0 or offset+length > len(packed):
            raise EvidenceError('unsupported or truncated MultiSend entry')
        calls.append({'to':target,'value':value,'data':'0x'+packed[offset:offset+length].hex()})
        offset += length
    return {'target':args[0].lower(),'operation':1,'calls':calls}


def stale_checker_finding(source_repo, external, attestation):
    """Report a concrete off-chain checker mismatch without changing the source repo."""
    root = safe(source_repo)
    if attestation.get('provider') == 'local-origin-manifest':
        evidence_path = Path(__file__).with_name('historical-checker.json')
        raw = safe(evidence_path).read_bytes()
        if digest(raw) != '4022ccdf1635fffa2cdafe9a32daf4e46a35063e9465cf937f8f3ed549799170':
            raise EvidenceError('historical checker evidence drift')
        frozen = json.loads(raw)
        match = re.search(r"'(0x[0-9a-fA-F]{40})'", frozen['files']['constants/constants.ts']['excerpt'])
        expected = match.group(1).lower(); observed = {}
        references = [str(evidence_path)]
    else:
        root = safe(source_repo)
        config_path = root/'constants/constants.ts'
        text = safe(config_path).read_text().split('export const MULTISIG_ADDRESS', 1)[1]
        match = re.search(r"\[CHAIN_ID\.PHAROS_PACIFIC_MAINNET\]:\s*'(0x[0-9a-fA-F]{40})'", text)
        if not match:
            return None
        expected = match.group(1).lower(); observed = {}
        references = [str(config_path)]
        for name in ('SlpPharosProxy', 'SlpPharosRewardsVault'):
            path = root/'tasks'/f'{name}.ts'
            if 'expect(await' not in safe(path).read_text() or 'hasRole(DEFAULT_ADMIN_ROLE, MULTISIG_ADDRESS[chainId])' not in path.read_text():
                return None
            references.append(str(path))
    for address in attestation['proxies']:
        members = external['roles'][address]['0x'+'00'*32]
        observed[address] = {'enumeratedAdmins':members,'checkerExpectedAdminPresent':expected in members}
    if all(r['checkerExpectedAdminPresent'] for r in observed.values()):
        return None
    return finding('slp-repository-admin-check-stale','FAIL',
        'Static operational-checker discrepancy, not a financial/contract failure: the read-only source repository checker still requires GOV as administrator, while the approved later migration and pinned state use Timelock. Hardhat was NOT executed; source assertions were compared to authenticated cached role reads. Correct the checker in a separately authorized change.',
        expected={'admin':expected,'hasRole':True},actual=observed,evidence=references,
        priority='P1',contract=list(observed))


def run(store, source_repo):
    """Generate a followup result without rewriting any first-round result or ledger."""
    baseline_dir = store.root / 'output/reconciliation/baseline-before-p0-closeout'
    preservation = read(baseline_dir / 'PRESERVATION-MANIFEST.json')
    for filename, checksum in preservation.items():
        if digest(safe(baseline_dir / filename).read_bytes()) != checksum:
            raise EvidenceError('preserved baseline checksum changed')
        if digest(safe(store.root / 'output/reconciliation' / filename).read_bytes()) != checksum:
            raise EvidenceError('original output changed; refuse silent baseline replacement')
    baseline = read(baseline_dir / 'RECONCILIATION-RESULTS.json')
    plan = store.get('checkpoints/plan.json')
    if any(plan['snapshot'][k] != v for k,v in SNAPSHOT.items()):
        raise EvidenceError('closeout requires the original fixed snapshot')
    logs, _, complete = cached_logs(store, plan)
    if not complete:
        raise EvidenceError('required original event coverage incomplete; no rescan is performed')
    attestation = authenticate(Path(source_repo), store, logs)
    store.put('cache/snapshots/p0-closeout-attestation.json', attestation)
    inv = copy.deepcopy(store.get('cache/snapshots/inventory.json'))
    for address, proof in attestation['proxies'].items():
        inv['targets'][address]['abi'] = proof['abi']
        inv['targets'][address]['abiStatus'] = 'AUTHENTICATED_RECORDED_RUNTIME'
    if any(x['status'] != 'PASS' for x in source_binding(inv).values()):
        raise EvidenceError('core source binding changed')
    identities = store.get('cache/snapshots/identities.json')['entries']
    fresh = replay(inv, logs, plan['snapshot'], store.get('cache/snapshots/state.json'), complete,
                   identities, headers=store.get('cache/snapshots/headers.json'))
    if canonical(fresh['replay']) != canonical(baseline['replay']):
        raise EvidenceError('core replay differs from preserved baseline; explicit investigation required')
    events = external_events(logs, attestation)
    flow = reward_ledger(store, inv, attestation, events, logs)
    reported = {n['transaction']:n['amount'] for n in flow['notified']}
    accepted = {n['tx']:n['reward'] for n in baseline['replay']['rewards']}
    flow['acceptedOracleReportsMatch'] = reported == accepted
    if reported != accepted:
        flow['issues'].append({'status':'UNVERIFIED','reason':'Vault notifications differ from previously verified accepted Oracle reports'})
        flow['cashFlowStatus'] = 'UNVERIFIED'
    external = store.get('cache/snapshots/external-state.json')
    routing = routing_evidence(attestation, external, events)
    checks = copy.deepcopy(baseline['checks']); changes = []
    updated_config = {r['id']:r for r in governance_checks(store.get('cache/snapshots/governance-state.json'), complete, set(attestation['proxies']))}
    for c in checks:
        before = copy.deepcopy(c)
        if c['id'] == 'external-call-source-mapping':
            c.update(status='PASS', confidence='RECORDED BUILD VERIFIED', reason='Full runtime, metadata CID, ABI, literal sources and pinned Git deployment artifacts match all three cached mainnet implementations.', recommendedInvestigation='')
        elif c['id'] in updated_config and c['affectedContract'] in attestation['proxies']:
            c.update(updated_config[c['id']])
        elif c['id'] == 'slp-routing' and all(r['status']=='PASS' for r in routing):
            c.update(status='PASS', confidence='SOURCE AND STATE VERIFIED', reason='Authenticated initialization and routing events match current destination getters; no post-deployment implementation upgrades observed. Historical approval judgments remain a separate finding.', recommendedInvestigation='')
        elif c['id'] == 'asset-backing':
            c['reason'] = 'SLP/RV identities resolved; underlying Staking implementation 0x9c6791f6dc5ba11a3919d72b496ed059e4f81cf4 still lacks authenticated executable source/build. IStaking interface and returned tuple shape are insufficient.'
        elif c['id'] == 'reward-provenance':
            c['reason'] = 'Native claim credits, forwarding and notification have transaction/balance evidence. Underlying reward generation and client minting semantics remain unauthenticated; cash-flow PASS is not earned-yield certification.'
        if before['status'] != c['status']:
            changes.append({'id':c['id'],'before':before['status'],'after':c['status'],'reason':c['reason']})
    checker_issue = stale_checker_finding(Path(source_repo), external, attestation)
    new_findings = [checker_issue] if checker_issue else []
    current_usage = store.get('checkpoints/rpc-usage.json')
    original_usage = baseline['rpcUsage']
    result = {'schema':'stpros-p0-closeout-v1','snapshot':plan['snapshot'],'chainId':1672,
              'baselineSha256':digest((baseline_dir/'RECONCILIATION-RESULTS.json').read_bytes()),
              'sourceAttestation':attestation,'checks':checks,'newFindings':new_findings,'statusChanges':changes,'coreReplayUnchanged':True,
              'governanceDocumentEvidence':store.get('cache/snapshots/p0-closeout-governance-document-evidence.json'),
              'newFinancialDiscrepancies':[c for c in flow['counters'] if c['status']=='FAIL'] + ([flow['restaking']] if flow['restaking']['status']=='FAIL' else []), 'newEvidenceIssues':flow['issues'],'rewardLedger':flow,'routing':routing,
              'assetReassessment':provisional_coverage(baseline['cashObservations'],external,store.get('cache/snapshots/state.json')),
              'stakingEvidence':{'status':'UNVERIFIED','implementation':abi.dec_addr(external['stakingImplementation']),
                'proxyIdentity':store.get('cache/snapshots/p0-closeout-staking-identity.json'),
                'reason':'The new repository contains only IStaking for this system contract, not its executable implementation/build or consensus minting implementation.'},
              'rpc':{'additionalRequests':current_usage['requests']-original_usage['requests'],
                     'additionalEstimatedCU':current_usage['estimatedCU']-original_usage['estimatedCU'],
                     'cumulative':current_usage,'justifications':store.get('cache/snapshots/p0-closeout-rpc-journal.json',[])},
              'limits':{'additionalBudget':500000,'cumulativeCeiling':5000000},
              'toolHashes':{p.name:digest(p.read_bytes()) for p in Path(__file__).parent.glob('*.py')},
              'conclusion':'INCOMPLETE economic certification; core numeric accounting unchanged'}
    if result['rpc']['additionalEstimatedCU'] > 500000 or current_usage['estimatedCU'] > 5000000:
        raise EvidenceError('closeout RPC budget exceeded')
    result['assetReassessment']['verifiedLiquidCustodyObservation'] = result['assetReassessment']['cash']
    result['assetReassessment']['verifiedEarnedYield'] = None
    result['assetReassessment']['classificationNotes'] = ['Cash is an observed native/WPROS custody balance, not certification of earned-yield origin.', 'Reported reward already restaked is included in stake observations; remaining vault cash is included in liquid custody, never added again.', 'Unverified stake is not assumed to be zero; full economic coverage remains INCOMPLETE.']
    result['newDiscrepancies'] = result['newFinancialDiscrepancies'] + new_findings
    result['baselineFindingStatusCounts'] = dict(Counter(c['status'] for c in checks))
    result['statusCounts'] = dict(Counter(c['status'] for c in checks + new_findings))
    out = store.root/'output/reconciliation/p0-closeout';out.mkdir(exist_ok=True)
    write(out/'RECONCILIATION-FOLLOWUP-RESULTS.json',result)
    write(out/'SOURCE-ATTESTATION.json',attestation)
    write(out/'REWARD-LEDGER.json',flow)
    write(out/'RPC-ADDITIONAL.json',result['rpc'])
    return result
