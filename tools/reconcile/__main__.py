"""python3 -m tools.reconcile {plan,sync,reconcile,all,status,closeout}."""
import argparse
import csv
import fcntl
import io
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from .storage import Store, EvidenceError, canonical, digest, atomic, write, safe
from .inventory import discover, abi_hashes, source_binding, number, ROOT
from .rpc import Rpc, BudgetExceeded, load_project_rpc_override
from .collect import Collector, cached_logs, transaction_readiness, usable_trace
from .replay import replay, finding
from .assets import assess, payment_evidence, provisional_coverage
from .external import observe, observe_proxy_slots, verify_fixed_contracts, discover_proxy_admin_history
from . import governance, conversion, rewards, incremental
from .external import authenticate_incremental, bound_inventory

DEFAULT_ROOT = '/Volumes/project/tmp/stpros-mainnet-reconciliation'


def cached_objects(store, category):
    result = {}
    for p in sorted((store.root / 'cache' / category).glob('0x*.json')):
        if '-transaction' not in p.stem:
            result[p.stem] = store.get(str(p.relative_to(store.root)))
    return result


def _offline(store, inv, blocker=None):
    incremental.recover_snapshot(store)
    plan = store.get('checkpoints/plan.json')
    snap = plan['snapshot'] if plan else None
    if plan and plan.get('historicalBoundary'):
        boundary = incremental.load_boundary(store, plan['historicalBoundary'])
        if not boundary:
            raise EvidenceError('missing historical boundary')
        incremental.validate_boundary(store, boundary)
    logs, intervals, complete = cached_logs(store, plan) if plan else ([], [], False)
    external_proof = store.get('cache/snapshots/p0-closeout-attestation.json')
    current_external = store.get('cache/snapshots/incremental-external.json', {})
    if plan and plan.get('historicalBoundary'):
        if not complete:
            current_external = {}
        inv = bound_inventory(inv, external_proof, current_external, snap)
    states = store.get('cache/snapshots/state.json', {})
    if states.get('snapshot') != snap:
        states = {}
    identity_data = store.get('cache/snapshots/identities.json', {})
    identities = identity_data.get('entries', {}) if identity_data.get('snapshot') == snap else {}
    bindings = source_binding(inv)
    if any(b['status'] != 'PASS' for b in bindings.values()) or not all(x['metadataMatch'] for x in inv['legacySources'].values()):
        identities = {}
    receipts, traces = cached_objects(store, 'receipts'), cached_objects(store, 'traces')
    headers = store.get('cache/snapshots/headers.json', {})
    # Orphan/error traces cannot prove a successful payment, conversion or asset movement.
    receipts = {tx: r for tx, r in receipts.items() if number(r.get('status', 0)) == 1 and
                headers.get(r.get('blockNumber'), {}).get('hash') == r.get('blockHash') and r.get('blockHash')}
    traces = {tx: t for tx, t in traces.items() if tx in receipts and usable_trace(t)}
    transaction_evidence = None
    if plan and plan.get('historicalBoundary'):
        transaction_evidence = transaction_readiness(store, inv, logs, snap, boundary['snapshot']['number'])
    result = replay(inv, logs, snap, states, complete,
        identities, receipts, traces, store.get('cache/snapshots/headers.json', {}))
    result.update(chainId=1672, sourceCommit=inv['sourceCommit'], abiHashes=abi_hashes(inv),
                  deploymentIdentity=identities, sourceBinding=bindings,
                  replayToolHashes={p.name: digest(p.read_bytes()) for p in Path(__file__).parent.glob('*.py')},
                  collectionComplete=complete, discoveryStatus='UNVERIFIED beyond event-discoverable routes', collectedLogs=len(logs),
                  coveredIntervals=[{k: m[k] for k in ('scope', 'startBlock', 'endBlock', 'count', 'sha256')} for m in intervals],
                  limitations=inv['limitations'], executionBlocker=blocker,
                  transactionCoverage=store.get('checkpoints/transaction-coverage.json', {}),
                  transactionEvidence=transaction_evidence)
    if transaction_evidence:
        result['checks'].append(finding('incremental-transaction-evidence',
            'PASS' if transaction_evidence['requiredEvidenceComplete'] else 'UNVERIFIED',
            'New transactions require successful block-bound receipts/details and payment/reward traces before snapshot completion.', actual=transaction_evidence))
    provenance_path = ROOT / 'snapshots/deployed/provenance.json'
    provenance_bytes = provenance_path.read_bytes()
    if digest(provenance_bytes) != inv['files']['snapshots/deployed/provenance.json']:
        raise EvidenceError('pinned provenance file changed')
    provenance = json.loads(provenance_bytes)['records']
    result['legacySourceEvidence'] = {k: {p: entry for p, entry in value.get('sources', {}).items()
        if p.startswith('project/contracts/')} for k, value in provenance.items()
        if k in {v['record'] for v in inv['implementations'].values() if v['version'] == 'V1'}}
    wrapped_code = store.get('cache/snapshots/fixed-code-identities.json', {})
    wrapped_verified = wrapped_code.get('snapshot') == snap and wrapped_code.get('entries', {}).get('WPROS', {}).get('status') == 'PASS'
    result['assets'] = assess(inv, traces, receipts, result,
                              store.get('cache/snapshots/migration-state.json'), logs, wrapped_verified, headers=headers)
    result['checks'] += result['assets']['checks']
    result['paymentEvidence'] = payment_evidence(inv, result, receipts, traces)
    for check in result['checks']:
        if check['id'] == 'redemption-payments' and result['paymentEvidence']['status'] == 'PASS':
            check.update(status='PASS', confidence='PROVEN', reason='Each observed completion has a distinct matching successful receipt transfer or native trace payout.', recommendedInvestigation='')
    external_state = store.get('cache/snapshots/external-state.json', {})
    result['externalState'] = external_state if external_state.get('snapshot') == snap else {}
    fixed_code = store.get('cache/snapshots/fixed-code-identities.json', {})
    result['fixedCodeIdentities'] = fixed_code if fixed_code.get('snapshot') == snap else {}
    for name, row in result['fixedCodeIdentities'].get('entries', {}).items():
        result['checks'].append(finding('fixed-runtime-' + name, row['status'], 'Pinned runtime compared with recorded bytecode/fixture.',
            row['expected'], row['actual'], [row['record']], 'P1', row['address']))
    reward_evidence = store.get('cache/snapshots/reward-evidence.json', {})
    result['rewardEvidence'] = reward_evidence if reward_evidence.get('snapshot') == snap else {}
    if transaction_evidence:
        result['requiredEvidenceComplete'] = transaction_evidence['requiredEvidenceComplete'] and result['rewardEvidence'].get('requiredEvidenceComplete', False)
        result['checks'].append(finding('incremental-reward-evidence',
            'PASS' if result['rewardEvidence'].get('requiredEvidenceComplete') else 'UNVERIFIED',
            'Required new reward receipt/trace/balance cases must be complete; this does not certify reward origin.'))
    result['rewardProvenance'] = rewards.provenance(inv, result['externalAccountingEvents'], result['replay']['rewards'], result.get('cashObservations', {}))
    timelock = inv['addresses']['governance']['timelock'].lower()
    for address, roles in result['externalState'].get('roles', {}).items():
        outsiders = [a for a in roles.get('0x' + '00' * 32, []) if a.lower() != timelock]
        if outsiders:
            result['checks'].append(finding('external-admin-inventory-' + address, 'UNVERIFIED',
                'Current external DEFAULT_ADMIN_ROLE holders include accounts outside the core Timelock. Confirm the approved authority inventory and routing/upgrade powers; this is not by itself an unauthorized-action finding.',
                expected='owner-approved external authority inventory', actual=outsiders,
                evidence=['cache/snapshots/external-state.json'], priority='P1', contract=address))
    for address, slot in result['externalState'].get('proxySlots', {}).items():
        admin = slot.get('admin')
        if admin and int(admin, 16) and admin != timelock and slot.get('adminOwner') != timelock:
            result['checks'].append(finding('external-upgrade-authority-' + address, 'UNVERIFIED',
                'An external EIP-1967 admin is not established as Timelock-controlled. Verify the independently approved upgrade authority; DEFAULT_ADMIN_ROLE membership alone does not resolve proxy upgrade control.',
                expected='Timelock control or separately approved upgrade-authority inventory',
                actual={'admin': admin, 'adminOwner': slot.get('adminOwner')},
                evidence=['cache/snapshots/external-state.json'], priority='P1', contract=address))
    for address, ledger in result['rewardProvenance']['vaults'].items():
        if ledger['unclassifiedInflow']:
            result['checks'].append(finding('reward-source-unclassified-' + address, 'UNVERIFIED',
                'RewardsVault inflows lack matching SLP and underlying Staking reward events. Investigate donations, principal routing, unsupported ABI or missing provenance before calling these funds yield.',
                actual=ledger['unclassifiedInflow'], evidence=ledger['entries'], contract=address))

    result['economicCoverage'] = provisional_coverage(result.get('cashObservations', {}), result['externalState'], states)
    governance_state = store.get('cache/snapshots/governance-state.json', {})
    result['governanceState'] = governance_state if governance_state.get('snapshot') == snap else {}
    verified_governance = {a for a, r in identities.items() if r['status'] == 'PASS'}
    verified_governance.update(r['address'] for r in result['fixedCodeIdentities'].get('entries', {}).values() if r['status'] == 'PASS')
    verified_governance.update(a for a, r in result['externalState'].get('adminCodeIdentity', {}).items() if r['matchesRecordedProxyAdminRuntime'])
    verified_external = {a for a, t in inv['targets'].items() if t['abiStatus'] == 'AUTHENTICATED_RECORDED_RUNTIME'} if complete else set()
    verified_governance.update(verified_external)
    if plan and plan.get('historicalBoundary'):
        result['externalIdentity'] = current_external if incremental.same(current_external.get('snapshot'), snap) else {}
        for address, target in inv['targets'].items():
            if target['name'].startswith('SlpPharosProxy') or 'RewardsVault' in target['name']:
                row = result['externalIdentity'].get('entries', {}).get(address, {})
                result['checks'].append(finding('external-runtime-' + address, row.get('status', 'UNVERIFIED'),
                    row.get('reason', 'Current snapshot implementation evidence missing'), actual=row, contract=address))
        for address in result.get('externalIdentity', {}).get('entries', {}):
            roles = result['externalState'].get('roles', {}).get(address, {})
            from tools.lib.keccak import keccak256
            role_ids = ('0x' + '00' * 32, keccak256(b'WHITELIST_ROLE'))
            expected_roles = {role: set() for role in role_ids}
            for event in result['governance']:
                if event['address'] == address and event['name'] in ('RoleGranted', 'RoleRevoked'):
                    role = event['args']['role']
                    if role in expected_roles:
                        account = event['args']['account'].lower()
                        if event['name'] == 'RoleGranted':
                            expected_roles[role].add(account)
                        else:
                            expected_roles[role].discard(account)
            errors = result['externalState'].get('errors', {})
            available = address in verified_external and all(role in roles for role in role_ids) and not any(
                key.startswith(address + ':getRole') for key in errors)
            expected = {k: sorted(v) for k,v in expected_roles.items()}
            actual = {k: sorted(a.lower() for a in roles.get(k, [])) for k in role_ids}
            status = ('PASS' if actual == expected else 'FAIL') if available else 'UNVERIFIED'
            result['checks'].append(finding('external-role-state-' + address, status,
                'Current DEFAULT_ADMIN_ROLE/WHITELIST_ROLE enumeration compared with ordered role events; approved authority remains a separate judgment.',
                expected, actual if available else None, contract=address))
        if verified_external and complete and result['externalState']:
            from .closeout import external_events, routing_evidence
            scoped_proof = dict(external_proof, proxies={a: external_proof['proxies'][a] for a in verified_external})
            result['externalRouting'] = routing_evidence(scoped_proof, result['externalState'], external_events(logs, scoped_proof))
            for address in verified_external:
                proxy = external_proof['proxies'][address]
                if 'RewardsVault' in proxy['name']:
                    for view, index in [('getWPROS', 2), ('getVTokenOracle', 3)]:
                        from tools.lib import abi
                        raw = result['externalState']['reads'].get(address + ':' + view + '():')
                        actual = abi.dec_addr(raw) if raw else None
                        expected = proxy['initialization']['args'][index].lower()
                        result['externalRouting'].append({'address': address, 'view': view, 'expected': expected, 'actual': actual,
                            'status': 'UNVERIFIED' if actual is None else 'PASS' if actual == expected else 'FAIL'})
            for row in result['externalRouting']:
                result['checks'].append(finding('external-routing-' + row['address'] + '-' + row['view'], row['status'],
                    'Authenticated initialization/configuration events compared with snapshot routing state.', row['expected'], row['actual'], contract=row['address']))
    result['checks'] += governance.checks(result['governanceState'], complete, verified_governance)
    result['checks'] += governance.historical_admin_findings(inv, result['governance'], result['externalState'], snap)
    transactions = {p.stem.removesuffix('-transaction'): store.get(str(p.relative_to(store.root)))
                    for p in (store.root / 'cache/receipts').glob('0x*-transaction.json')}
    transactions = {tx: t for tx, t in transactions.items() if tx in receipts and t.get('blockHash') == receipts[tx]['blockHash']}
    # This selector was observed on mainnet's SLP forwarding transactions. It matches
    # a candidate signature absent from the supplied SLP source; do not decode it as verified ABI.
    from tools.lib.keccak import selector
    source_gap_transactions = [tx for tx, tr in transactions.items()
        if (tr.get('to') or '').lower() in inv['targets']
        and inv['targets'][tr['to'].lower()]['name'].startswith('SlpPharosProxy')
        and tr.get('input', '').startswith(selector('transferToRewardsVault(uint256)'))]
    if source_gap_transactions:
        result['checks'].append(finding('external-call-source-mapping', 'PASS' if all((transactions[t].get('to') or '').lower() in verified_external for t in source_gap_transactions) else 'BLOCKED',
            'Successful SLP forwarding calls use selector 0x30bc5d2c, which matches the candidate transferToRewardsVault(uint256) signature but has no verified deployed ABI/source mapping in this repository. Claims and forwarding occur in separate transactions; do not substitute the current combined claim-and-forward source.',
            actual='0x30bc5d2c', evidence=sorted(source_gap_transactions), contract=inv['addresses']['slpDestinations'][0]))
    for check in result['checks']:
        if check['id'] == 'slp-routing' and verified_external:
            rows = result.get('externalRouting', [])
            all_external = {a for a,t in inv['targets'].items() if t['name'].startswith('SlpPharosProxy') or 'RewardsVault' in t['name']}
            status = 'FAIL' if any(r['status'] == 'FAIL' for r in rows) else 'PASS' if rows and all_external == verified_external and all(r['status'] == 'PASS' for r in rows) else 'UNVERIFIED'
            check.update(status=status, reason='Current authenticated SLP/RV routing compared with initialization and ordered events; role/upgrade authority and economic provenance are separate checks.', confidence='SOURCE AND STATE VERIFIED' if status == 'PASS' else 'INCOMPLETE')
        if check['id'] == 'external-call-source-mapping' and check['status'] == 'PASS':
            check.update(reason='Current runtime and complete upgrade history match the previously authenticated ABI/source closure.', confidence='RECORDED BUILD VERIFIED', recommendedInvestigation='')
    result['conversionEvidence'] = conversion.check(inv, result['conversions'], transactions, traces,
        complete and any(c['id'] == 'oracle-T' and c['status'] == 'PASS' for c in result['checks']))
    result['checks'] += result['conversionEvidence']['failures']
    result['checks'].append(finding('conversion-rounding', result['conversionEvidence']['status'],
        'Call-path-specific floor/ceil arithmetic; unresolved call attribution remains UNVERIFIED.',
        evidence=result['conversionEvidence']['missingTransactions']))
    if blocker:
        result['checks'].append(finding('execution-prerequisite', 'BLOCKED', blocker))
    usage = store.get('checkpoints/rpc-usage.json', {'requests': 0, 'estimatedCU': 0, 'methods': {}, 'errors': {}, 'retries': 0})
    result['rpcUsage'] = usage
    result['incrementalSummary'] = incremental.summary(store, plan, result, logs, states) if plan else None
    out = output_dir(store, plan)
    write(out / 'RECONCILIATION-RESULTS.json', result)
    write(out / 'RPC-USAGE.json', usage)
    write(store.root / 'output/rpc-usage/RPC-USAGE.json', usage)
    evidence_files = []
    for category in ('receipts', 'traces'):
        evidence_files.extend(sorted((store.root / 'cache' / category).glob('0x*.json')))
    for name in ('headers.json', 'inventory.json', 'state.json', 'identities.json', 'external-state.json',
                 'governance-state.json', 'migration-state.json', 'fixed-code-identities.json',
                 'reward-evidence.json', 'external-implementations.json', 'final-anchor-verification.json',
                 'source-binding.json', 'cast-crosscheck.json', 'Pharos-IStaking.sol', 'Pharos-Staking.sol',
                 'p0-closeout-attestation.json', 'incremental-external.json'):
        path = store.root / 'cache/snapshots' / name
        if path.exists():
            evidence_files.append(path)
    for name in ('collection-finished.json', 'rpc-run.json'):
        path = store.root / 'checkpoints' / name
        if path.exists():
            evidence_files.append(path)
    evidence_hashes = {str(p.relative_to(store.root)): digest(safe(p).read_bytes()) for p in evidence_files}
    write(out / 'DATA-MANIFEST.json', {'chainId': 1672, 'snapshot': snap, 'chunks': intervals,
                                     'inventorySha256': digest(canonical(inv)), 'sourceFiles': inv['files'],
                                     'abiHashes': abi_hashes(inv), 'evidenceFiles': evidence_hashes})
    buf = io.StringIO()
    fields = ['id', 'severity', 'check', 'status', 'affectedContract', 'expected', 'actual', 'rawDifference',
              'supportingEvidence', 'confidence', 'reason', 'reproduction', 'recommendedInvestigation']
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for row in result['checks']:
        if row['status'] != 'PASS':
            writer.writerow({k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for k, v in row.items()})
    atomic(out / 'ANOMALIES.csv', buf.getvalue().encode())
    atomic(store.root / 'output/anomalies/ANOMALIES.csv', buf.getvalue().encode())
    counts = {s: sum(c['status'] == s for c in result['checks']) for s in ('PASS', 'FAIL', 'UNVERIFIED', 'BLOCKED')}
    rows = '\n'.join(f"| {c['id']} | {c['severity']} | {c['status']} | {c['reason']} |" for c in result['checks'] if not c['id'].startswith(('holder-', 'owner-')))
    block_time = store.get('cache/snapshots/headers.json', {}).get(hex(snap['number']), {}).get('timestamp') if snap else None
    report_date = datetime.fromtimestamp(number(block_time), ZoneInfo('Asia/Shanghai')).date().isoformat() if block_time is not None else 'UNPINNED'
    text = f'''# stPROS Mainnet Reconciliation — {report_date}

Status: **INCOMPLETE**. This report does not establish protocol solvency.

- Chain: 1672 (expected; independently checked only when preflight succeeds).
- Snapshot: {json.dumps(snap, sort_keys=True)}.
- Production source commit: `{inv['sourceCommit']}` (working-tree source hashes in DATA-MANIFEST).
- Historical source records include the initial V1 implementation, the V1 replacement at block 12838543,
  and the V4 migration transaction `{inv['release']['upgradeTransaction']}` at block {inv['release']['upgradeBlock']}.
  These are recorded deployment facts; live identity results are in RECONCILIATION-RESULTS.
- Required start block: {inv['start']}. Collected logs: {len(logs)}. Complete scopes: {complete}.
- Covered intervals: {len(intervals)}; exact ranges and checksums are in DATA-MANIFEST.
- RPC requests: {usage['requests']}; estimated CU: {usage['estimatedCU']}; retries: {usage['retries']}.
  Estimates are conservative internal weights, not provider billing guarantees. The ledger persists across runs.
- Receipt/trace cached counts: {result['receiptCount']} / {result['traceCount']}.
- Execution blocker: {blocker or 'None recorded; see evidence gaps and collection status.'}

| Check | Priority | Result | Evidence / limitation |
|---|---|---|---|
{rows}

Totals: {json.dumps(counts, sort_keys=True)}.

## Accounting and economic evidence

Replay totals (only meaningful with complete qualified evidence):
`{json.dumps({k: result['replay'][k] for k in ('supply','minted','burned','commissionMinted','T','V','queued','completed','liability')}, sort_keys=True)}`.
Zero values with no input logs are empty replay accumulators, **not mainnet balances**.
Holder and owner-queue comparisons, reward calculations and migration transitions are in the JSON results.
Custody observations are unclassified. No active/pending/unbonding/claimable stake total is certified;
no cash or historical `totalBonded` counter is substituted for attributable economic backing.
Accepted Oracle rewards are separate from economic provenance. RewardsVault's source reports its entire
balance, so unrelated principal or donations cannot be classified as yield without movement evidence.
Governance events and fund-routing events are preserved; absence from documentation is not revocation.

Provisional economic coverage (raw integer units; **UNVERIFIED**, not a solvency assertion):
`{json.dumps(result['economicCoverage'], sort_keys=True)}`.

## Limitations and validation

{chr(10).join('- ' + x for x in inv['limitations'] + result['evidenceGaps'])}

- Coverage completeness applies to the declared filters. Historical routing destinations are discovered from configuration
  events and backfilled; addresses invisible to those events remain an evidence gap. External source/ABI identity is qualified only by snapshot-specific runtime correspondence.
  Unknown core events/implementations remain visible and block qualified replay.
- Reward call-path rounding, full commission mint attribution, role enumeration, native provenance and migration
  internal transfers require additional transaction/state verification. Their aggregate checks remain UNVERIFIED.
- Targeted receipt/trace sampling is bounded (defaults: 2000 receipts, 40 traces); no full-chain native transfer scan.
- Chain reorganization invalidates reuse and stops execution without deleting evidence.
- Full Foundry CI, fork suites, deployment/broadcast, source recompilation and live cast RPC cross-check: NOT RUN. Offline cast calldata decoding of the real migration trace was executed when cache/snapshots/cast-crosscheck.json is present.
- Focused Python test execution is documented separately in VALIDATION.md when executed.

## Reproduction

Run from the production repository. Set PHAROS_RPC_URL in the process environment without logging its value.
The CLI defaults to inherited environment only. The owner explicitly authorized the optional\n`--rpc-from-project-env` flag to extract only this project variable without shell execution.\nBoth per-run and cumulative budgets apply; neither is automatically increased.

```sh
python3 -m tools.reconcile plan
python3 -m tools.reconcile all
python3 -m tools.reconcile all --advance --rpc-from-project-env
python3 -m tools.reconcile reconcile
python3 -m tools.reconcile status
```

`all --advance` pins/resumes a finalized snapshot, checks sealed history locally and downloads uncovered intervals only.
`reconcile` is strictly offline. Detailed evidence resides in the data directory under `cache/`;
all large data stays outside Git. Completed snapshot reports are immutable; incomplete attempts are separate.
'''
    if result['incrementalSummary']:
        delta = result['incrementalSummary']
        write(out / 'INCREMENTAL-SUMMARY.json', delta)
        text += '\n## Incremental snapshot summary\n\n' + summary_markdown(delta)
    atomic(out / 'RECONCILIATION-REPORT.md', text.encode())
    return result


def output_dir(store, plan):
    return store.root / 'output/reconciliation/attempts' / (incremental.identity(plan['snapshot']) if plan else 'unplanned')


def summary_markdown(delta):
    a, b = delta['previousSnapshot'], delta['currentSnapshot']
    text = f"Previous: {a['number']} `{a['hash']}`. Current: {b['number']} `{b['hash']}`.\n\n"
    text += f"New scan: {delta['newBlockRange']}; events: {delta['newEvents']}; transactions: {len(delta['relevantTransactions'])}.\n\n"
    text += '| Accounting | Current | Change | Unit |\n|---|---|---|---|\n'
    for key, row in delta['accounting'].items():
        if key != 'PROSPerStPROS':
            text += f"| {key} | {row['currentDecimal']} | {row['changeDecimal']} | {row['unit']} |\n"
    text += '\nExchange rate (PROS/stPROS): ' + json.dumps(delta['accounting']['PROSPerStPROS'], sort_keys=True) + '\n\n'
    for label, key in [('Deposits/withdrawals', 'movements'), ('Custody', 'custodyObservations'),
                       ('Snapshot state observations', 'snapshotObservations'), ('BridgeVault balance', 'bridgeBalance'), ('Staking observations (provisional)', 'stakingObservations'),
                       ('Vault reported rewards', 'reportedRewards'), ('Accepted Oracle rewards', 'acceptedOracleRewards'),
                       ('Finding changes', 'findingChanges'), ('Current run RPC', 'rpcRun')]:
        text += label + ': `' + json.dumps(delta[key], sort_keys=True) + '`\n\n'
    return text + delta['provisionalAssets'] + '\n' + delta['units'] + '\n'


def offline(store, inv, blocker=None):
    try:
        return _offline(store, inv, blocker)
    except (EvidenceError, ValueError, KeyError, TypeError, AttributeError, OSError):
        # No corrupted evidence may contribute to a successful audit conclusion.
        result = {'chainId': 1672, 'snapshot': None, 'collectionComplete': False, 'collectedLogs': 0,
                  'checks': [finding('cache-integrity', 'BLOCKED', 'Evidence integrity/schema validation failed; preserve cache and investigate.')],
                  'rpcUsage': {'requests': None, 'estimatedCU': None}, 'executionBlocker': 'cache integrity/schema failure'}
        out = store.root / 'output/reconciliation/attempts/blocked'
        write(out / 'RECONCILIATION-RESULTS.json', result)
        atomic(out / 'RECONCILIATION-REPORT.md', b'# Reconciliation BLOCKED\n\nEvidence integrity/schema failure. Cache preserved. No accounting conclusion.\n')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'sync', 'reconcile', 'all', 'status', 'closeout'))
    parser.add_argument('--data-dir', default=DEFAULT_ROOT)
    parser.add_argument('--slp-repo', default='/Volumes/project/workspace/vETH-3.0-contract', help='Read-only source artifact input for fixed-snapshot closeout')
    parser.add_argument('--budget-cu', type=int, default=5_000_000)
    parser.add_argument('--run-budget-cu', type=int, default=100_000)
    parser.add_argument('--chunk', type=int, default=5000)
    parser.add_argument('--fallback-depth', type=int, default=256)
    parser.add_argument('--request-interval', type=float, default=0.5)
    parser.add_argument('--max-receipts', type=int, default=2000)
    parser.add_argument('--max-traces', type=int, default=40)
    parser.add_argument('--advance', action='store_true')
    parser.add_argument('--rpc-from-project-env', action='store_true', help='Explicitly authorized extraction of only PHAROS_RPC_URL from this project .env; never sourced')
    args = parser.parse_args(argv)
    if not 1 <= args.budget_cu <= 5_000_000 or args.run_budget_cu < 1 or not 1 <= args.chunk <= 5000 or args.fallback_depth < 1 or args.request_interval < 0.25 or min(args.max_receipts, args.max_traces) < 0:
        parser.error('unsafe limits; budget cannot exceed 5,000,000 CU')
    data_root = safe(args.data_dir).resolve()
    if data_root == ROOT or ROOT in data_root.parents:
        parser.error('raw data must remain outside the production repository')
    store = Store(data_root)
    with open(safe(store.root / 'checkpoints/execution.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('BLOCKED: another reconciliation process owns this cache')
            return 2
        if args.command == 'closeout':
            if args.advance or args.rpc_from_project_env:
                parser.error('closeout is offline and cannot advance or load environment files')
            from .closeout import run
            result = run(store, args.slp_repo)
            print(json.dumps({'output':str(store.root / 'output/reconciliation/p0-closeout'), 'statusCounts':result['statusCounts'], 'rpc':{k:v for k,v in result['rpc'].items() if k not in ('justifications','cumulative')}}, sort_keys=True))
            return 0
        if args.command == 'status':
            print(json.dumps({'plan': store.get('checkpoints/plan.json'),
                              'progress': store.get('checkpoints/progress.json'),
                              'completedSnapshot': store.get('checkpoints/completed-snapshot.json'),
                              'currentRun': store.get('checkpoints/rpc-run.json'),
                              'usage': store.get('checkpoints/rpc-usage.json')}, indent=2))
            return 0
        inv = store.get('cache/snapshots/inventory.json')
        if inv is None:
            inv = discover()
            store.put('cache/snapshots/inventory.json', inv)
        actual_start = min(t['start'] for t in inv['targets'].values())
        if actual_start < inv['start']:
            inv['start'] = actual_start
            store.put('cache/snapshots/inventory.json', inv)
        blocker = None
        try:
            if args.command in ('plan', 'sync', 'all'):
                if args.rpc_from_project_env:
                    load_project_rpc_override(ROOT)
                rpc = Rpc(store, args.budget_cu, args.request_interval, run_budget=args.run_budget_cu)
                rpc.save()
                collector = Collector(store, rpc, inv)
                plan = store.get('checkpoints/plan.json')
                if args.command in ('plan', 'all') or plan is None:
                    plan = collector.preflight(args.fallback_depth, args.chunk, args.advance)
                    print(json.dumps({'snapshot': plan['snapshot'], 'estimate': plan['estimate'], 'capabilities': plan['capabilities']}, sort_keys=True), flush=True)
                if args.command == 'all':
                    if collector.boundary and incremental.same(plan['snapshot'], collector.boundary['snapshot']):
                        print(json.dumps({'status': 'No newer finalized snapshot', 'report': collector.boundary['result'], 'runUsage': rpc.run_usage()}, sort_keys=True))
                        return 0
                    collector.states(plan['snapshot'])
                    collector.migration_states(plan['snapshot'])
                if args.command in ('sync', 'all'):
                    collector.sync(plan)
                if args.command == 'all':
                    logs, _, complete = cached_logs(store, plan)
                    if plan.get('historicalBoundary'):
                        if not complete:
                            raise EvidenceError('incremental intervals are incomplete')
                        observe_proxy_slots(collector, plan['snapshot'])
                        current = authenticate_incremental(collector, plan['snapshot'], logs)
                        collector.inv = bound_inventory(inv, store.get('cache/snapshots/p0-closeout-attestation.json'), current, plan['snapshot'])
                    for _ in range(3):
                        if not collector.historical_routes(plan, logs):
                            break
                        logs, _, complete = cached_logs(store, plan)
                    identities = collector.identities(logs, plan['snapshot'])
                    result = replay(collector.inv, logs, plan['snapshot'], complete=complete, identities=identities, headers=collector.headers)
                    collector.states(plan['snapshot'], result)
                    observe_proxy_slots(collector, plan['snapshot'])
                    observe(collector, plan['snapshot'], logs)
                    verify_fixed_contracts(collector, plan['snapshot'])
                    if discover_proxy_admin_history(collector, plan):
                        logs, _, complete = cached_logs(store, plan)
                        result = replay(collector.inv, logs, plan['snapshot'], complete=complete, identities=identities, headers=collector.headers)
                    governance.observe(collector, plan['snapshot'], result)
                    collector.migration_states(plan['snapshot'])
                    coverage = collector.transactions(logs, args.max_receipts, args.max_traces, include_optional=False)
                    if coverage['missingReceipts']:
                        raise EvidenceError('Required transaction receipts/details incomplete; resume pending snapshot')
                    reward_evidence = rewards.collect_evidence(collector, plan['snapshot'], result['externalAccountingEvents'], max_new_traces=0)
                    if not coverage['requiredEvidenceComplete'] or not reward_evidence['requiredEvidenceComplete']:
                        raise EvidenceError('Required payment/reward evidence incomplete; resume pending snapshot')
                    collector.collect_transaction_traces(args.max_traces)
                    collector.check_snapshot(plan['snapshot'])
                    if collector.boundary:
                        collector.check_snapshot(collector.boundary['snapshot'])
                    inv = collector.inv
                    store.put('checkpoints/collection-finished.json', {'snapshot': plan['snapshot'], 'complete': complete})
            if args.command == 'reconcile':
                # Preserve prior execution blocker for deterministic offline reproduction.
                blocker = store.get('checkpoints/execution.json', {}).get('blocker')
        except KeyboardInterrupt:
            blocker = 'Interrupted safely; completed cache chunks preserved'
        except EvidenceError as exc:
            blocker = str(exc)  # Only locally curated, credential-free error classes.
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            blocker = 'Local evidence/schema error; inspect cache and source, no provider diagnostic exposed'
        store.put('checkpoints/execution.json', {'blocker': blocker})
        inv = store.get('cache/snapshots/inventory.json', inv)
        result = offline(store, inv, blocker)
        plan = store.get('checkpoints/plan.json')
        out = store.root / 'output/reconciliation/attempts/blocked' if result.get('executionBlocker') == 'cache integrity/schema failure' else output_dir(store, plan)
        finished = store.get('checkpoints/collection-finished.json', {})
        if (not blocker and not result.get('executionBlocker') and result.get('collectionComplete')
                and result.get('requiredEvidenceComplete', not plan.get('historicalBoundary'))
                and finished.get('complete') and incremental.same(finished.get('snapshot'), result.get('snapshot'))
                and result['snapshot'].get('finality') == 'provider-finalized-tag'):
            try:
                out = incremental.commit_snapshot(store, plan, result, out)
            except (EvidenceError, OSError, ValueError, KeyError, TypeError):
                blocker = 'Snapshot archive validation failed; evidence preserved'
                store.put('checkpoints/execution.json', {'blocker': blocker})
        print(json.dumps({'report': str(out / 'RECONCILIATION-REPORT.md'),
                          'blocker': blocker, 'logs': result['collectedLogs'], 'usage': result['rpcUsage']}, sort_keys=True))
        return 2 if blocker or result.get('executionBlocker') or result.get('requiredEvidenceComplete') is False or (args.command == 'all' and not result.get('collectionComplete')) else 0


if __name__ == '__main__':
    sys.exit(main())
