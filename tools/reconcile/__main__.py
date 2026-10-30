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
from .collect import Collector, cached_logs
from .replay import replay, finding
from .assets import assess, payment_evidence, provisional_coverage
from .external import observe, observe_proxy_slots, verify_fixed_contracts, discover_proxy_admin_history
from . import governance, conversion, rewards

DEFAULT_ROOT = '/Volumes/project/tmp/stpros-mainnet-reconciliation'


def cached_objects(store, category):
    result = {}
    for p in sorted((store.root / 'cache' / category).glob('0x*.json')):
        if '-transaction' not in p.stem:
            result[p.stem] = store.get(str(p.relative_to(store.root)))
    return result


def _offline(store, inv, blocker=None):
    plan = store.get('checkpoints/plan.json')
    snap = plan['snapshot'] if plan else None
    logs, intervals, complete = cached_logs(store, plan) if plan else ([], [], False)
    states = store.get('cache/snapshots/state.json', {})
    if states.get('snapshot') != snap:
        states = {}
    identity_data = store.get('cache/snapshots/identities.json', {})
    identities = identity_data.get('entries', {}) if identity_data.get('snapshot') == snap else {}
    bindings = source_binding(inv)
    if any(b['status'] != 'PASS' for b in bindings.values()) or not all(x['metadataMatch'] for x in inv['legacySources'].values()):
        identities = {}
    result = replay(inv, logs, snap, states, complete,
        identities, cached_objects(store, 'receipts'),
        cached_objects(store, 'traces'), store.get('cache/snapshots/headers.json', {}))
    result.update(chainId=1672, sourceCommit=inv['sourceCommit'], abiHashes=abi_hashes(inv),
                  deploymentIdentity=identities, sourceBinding=bindings,
                  replayToolHashes={p.name: digest(p.read_bytes()) for p in Path(__file__).parent.glob('*.py')},
                  collectionComplete=complete, discoveryStatus='UNVERIFIED beyond event-discoverable routes', collectedLogs=len(logs),
                  coveredIntervals=[{k: m[k] for k in ('scope', 'startBlock', 'endBlock', 'count', 'sha256')} for m in intervals],
                  limitations=inv['limitations'], executionBlocker=blocker,
                  transactionCoverage=store.get('checkpoints/transaction-coverage.json', {}))
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
    result['assets'] = assess(inv, cached_objects(store, 'traces'), cached_objects(store, 'receipts'), result,
                              store.get('cache/snapshots/migration-state.json'), logs, wrapped_verified)
    result['checks'] += result['assets']['checks']
    result['paymentEvidence'] = payment_evidence(inv, result, cached_objects(store, 'receipts'), cached_objects(store, 'traces'))
    for check in result['checks']:
        if check['id'] == 'redemption-payments' and result['paymentEvidence']['status'] == 'PASS':
            check.update(status='PASS', confidence='PROVEN', reason='Each observed completion has a distinct matching successful receipt transfer or native trace payout.', recommendedInvestigation='')
    result['externalState'] = store.get('cache/snapshots/external-state.json', {})
    fixed_code = store.get('cache/snapshots/fixed-code-identities.json', {})
    result['fixedCodeIdentities'] = fixed_code if fixed_code.get('snapshot') == snap else {}
    for name, row in result['fixedCodeIdentities'].get('entries', {}).items():
        result['checks'].append(finding('fixed-runtime-' + name, row['status'], 'Pinned runtime compared with recorded bytecode/fixture.',
            row['expected'], row['actual'], [row['record']], 'P1', row['address']))
    result['rewardEvidence'] = store.get('cache/snapshots/reward-evidence.json', {})
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
    result['governanceState'] = store.get('cache/snapshots/governance-state.json', {})
    verified_governance = {a for a, r in identities.items() if r['status'] == 'PASS'}
    verified_governance.update(r['address'] for r in result['fixedCodeIdentities'].get('entries', {}).values() if r['status'] == 'PASS')
    verified_governance.update(a for a, r in result['externalState'].get('adminCodeIdentity', {}).items() if r['matchesRecordedProxyAdminRuntime'])
    result['checks'] += governance.checks(result['governanceState'], complete, verified_governance)
    result['checks'] += governance.historical_admin_findings(inv, result['governance'], result['externalState'], snap)
    transactions = {p.stem.removesuffix('-transaction'): store.get(str(p.relative_to(store.root)))
                    for p in (store.root / 'cache/receipts').glob('0x*-transaction.json')}
    # This selector was observed on mainnet's SLP forwarding transactions. It matches
    # a candidate signature absent from the supplied SLP source; do not decode it as verified ABI.
    from tools.lib.keccak import selector
    source_gap_transactions = [tx for tx, tr in transactions.items()
        if (tr.get('to') or '').lower() in inv['targets']
        and inv['targets'][tr['to'].lower()]['name'].startswith('SlpPharosProxy')
        and tr.get('input', '').startswith(selector('transferToRewardsVault(uint256)'))]
    if source_gap_transactions:
        result['checks'].append(finding('external-call-source-mapping', 'BLOCKED',
            'Successful SLP forwarding calls use selector 0x30bc5d2c, which matches the candidate transferToRewardsVault(uint256) signature but has no verified deployed ABI/source mapping in this repository. Claims and forwarding occur in separate transactions; do not substitute the current combined claim-and-forward source.',
            actual='0x30bc5d2c', evidence=sorted(source_gap_transactions), contract=inv['addresses']['slpDestinations'][0]))
    result['conversionEvidence'] = conversion.check(inv, result['conversions'], transactions, cached_objects(store, 'traces'),
        complete and any(c['id'] == 'oracle-T' and c['status'] == 'PASS' for c in result['checks']))
    result['checks'] += result['conversionEvidence']['failures']
    result['checks'].append(finding('conversion-rounding', result['conversionEvidence']['status'],
        'Call-path-specific floor/ceil arithmetic; unresolved call attribution remains UNVERIFIED.',
        evidence=result['conversionEvidence']['missingTransactions']))
    if blocker:
        result['checks'].append(finding('execution-prerequisite', 'BLOCKED', blocker))
    usage = store.get('checkpoints/rpc-usage.json', {'requests': 0, 'estimatedCU': 0, 'methods': {}, 'errors': {}, 'retries': 0})
    result['rpcUsage'] = usage
    out = store.root / 'output/reconciliation'
    write(out / 'RECONCILIATION-RESULTS.json', result)
    write(out / 'RPC-USAGE.json', usage)
    write(store.root / 'output/rpc-usage/RPC-USAGE.json', usage)
    evidence_files = []
    for category in ('receipts', 'traces'):
        evidence_files.extend(sorted((store.root / 'cache' / category).glob('0x*.json')))
    for name in ('headers.json', 'inventory.json', 'state.json', 'identities.json', 'external-state.json',
                 'governance-state.json', 'migration-state.json', 'fixed-code-identities.json',
                 'reward-evidence.json', 'external-implementations.json', 'final-anchor-verification.json',
                 'source-binding.json', 'cast-crosscheck.json', 'Pharos-IStaking.sol', 'Pharos-Staking.sol'):
        path = store.root / 'cache/snapshots' / name
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
  events and backfilled; addresses invisible to those events remain an evidence gap. External source/ABI identity is unverified.
  Unknown core events/implementations remain visible and block qualified replay.
- Reward call-path rounding, full commission mint attribution, role enumeration, native provenance and migration
  internal transfers require additional transaction/state verification. Their aggregate checks remain UNVERIFIED.
- Targeted receipt/trace sampling is bounded (defaults: 2000 receipts, 40 traces); no full-chain native transfer scan.
- Chain reorganization invalidates reuse and stops execution without deleting evidence.
- Full Foundry CI, fork suites, deployment/broadcast, source recompilation and live cast RPC cross-check: NOT RUN. Offline cast calldata decoding of the real migration trace was executed when cache/snapshots/cast-crosscheck.json is present.
- Focused Python test execution is documented separately in VALIDATION.md when executed.

## Reproduction

Run from the production repository. Set PHAROS_RPC_URL in the process environment without logging its value.
The CLI defaults to inherited environment only. The owner explicitly authorized the optional\n`--rpc-from-project-env` flag to extract only this project variable without shell execution.\nBudget applies to the cumulative cache ledger.

```sh
python3 -m tools.reconcile plan
python3 -m tools.reconcile all
python3 -m tools.reconcile all --advance --rpc-from-project-env
python3 -m tools.reconcile reconcile
python3 -m tools.reconcile status
```

`all --advance` pins a new finalized snapshot, verifies previous anchors and downloads missing intervals only.
`reconcile` is strictly offline. Detailed evidence resides under `../../cache/` relative to this report;
all large data stays outside Git. Reports and source are uncommitted; no contracts or deployment records changed.
'''
    atomic(out / 'RECONCILIATION-REPORT.md', text.encode())
    return result


def offline(store, inv, blocker=None):
    try:
        return _offline(store, inv, blocker)
    except (EvidenceError, ValueError, KeyError, TypeError, AttributeError, OSError):
        # No corrupted evidence may contribute to a successful audit conclusion.
        result = {'chainId': 1672, 'snapshot': None, 'collectionComplete': False, 'collectedLogs': 0,
                  'checks': [finding('cache-integrity', 'BLOCKED', 'Evidence integrity/schema validation failed; preserve cache and investigate.')],
                  'rpcUsage': {'requests': None, 'estimatedCU': None}, 'executionBlocker': 'cache integrity/schema failure'}
        out = store.root / 'output/reconciliation'
        write(out / 'RECONCILIATION-RESULTS.json', result)
        atomic(out / 'RECONCILIATION-REPORT.md', b'# Reconciliation BLOCKED\n\nEvidence integrity/schema failure. Cache preserved. No accounting conclusion.\n')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'sync', 'reconcile', 'all', 'status', 'closeout'))
    parser.add_argument('--data-dir', default=DEFAULT_ROOT)
    parser.add_argument('--slp-repo', default='/Volumes/project/workspace/vETH-3.0-contract', help='Read-only source artifact input for fixed-snapshot closeout')
    parser.add_argument('--budget-cu', type=int, default=5_000_000)
    parser.add_argument('--chunk', type=int, default=5000)
    parser.add_argument('--fallback-depth', type=int, default=256)
    parser.add_argument('--request-interval', type=float, default=0.5)
    parser.add_argument('--max-receipts', type=int, default=2000)
    parser.add_argument('--max-traces', type=int, default=40)
    parser.add_argument('--advance', action='store_true')
    parser.add_argument('--rpc-from-project-env', action='store_true', help='Explicitly authorized extraction of only PHAROS_RPC_URL from this project .env; never sourced')
    args = parser.parse_args(argv)
    if not 1 <= args.budget_cu <= 5_000_000 or not 1 <= args.chunk <= 5000 or args.fallback_depth < 1 or args.request_interval < 0.25 or min(args.max_receipts, args.max_traces) < 0:
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
                rpc = Rpc(store, args.budget_cu, args.request_interval)
                collector = Collector(store, rpc, inv)
                plan = store.get('checkpoints/plan.json')
                if args.command in ('plan', 'all') or plan is None:
                    plan = collector.preflight(args.fallback_depth, args.chunk, args.advance)
                    print(json.dumps({'snapshot': plan['snapshot'], 'estimate': plan['estimate'], 'capabilities': plan['capabilities']}, sort_keys=True), flush=True)
                if args.command == 'all':
                    collector.states(plan['snapshot'])
                    collector.migration_states(plan['snapshot'])
                if args.command in ('sync', 'all'):
                    collector.sync(plan)
                if args.command == 'all':
                    logs, _, complete = cached_logs(store, plan)
                    for _ in range(3):
                        if not collector.historical_routes(plan, logs):
                            break
                        logs, _, complete = cached_logs(store, plan)
                    identities = collector.identities(logs, plan['snapshot'])
                    result = replay(inv, logs, plan['snapshot'], complete=complete, identities=identities, headers=collector.headers)
                    collector.states(plan['snapshot'], result)
                    observe(collector, plan['snapshot'], logs)
                    observe_proxy_slots(collector, plan['snapshot'])
                    verify_fixed_contracts(collector, plan['snapshot'])
                    if discover_proxy_admin_history(collector, plan):
                        logs, _, complete = cached_logs(store, plan)
                        result = replay(inv, logs, plan['snapshot'], complete=complete, identities=identities, headers=collector.headers)
                    governance.observe(collector, plan['snapshot'], result)
                    collector.migration_states(plan['snapshot'])
                    trace_before = rpc.ledger['methods'].get('debug_traceTransaction', {}).get('requests', 0)
                    rewards.collect_evidence(collector, plan['snapshot'], result['externalAccountingEvents'], max_new_traces=args.max_traces)
                    trace_used = rpc.ledger['methods'].get('debug_traceTransaction', {}).get('requests', 0) - trace_before
                    collector.transactions(logs, args.max_receipts, max(0, args.max_traces - trace_used))
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
        result = offline(store, inv, blocker)
        print(json.dumps({'report': str(store.root / 'output/reconciliation/RECONCILIATION-REPORT.md'),
                          'blocker': blocker, 'logs': result['collectedLogs'], 'usage': result['rpcUsage']}, sort_keys=True))
        return 2 if blocker or result.get('executionBlocker') else 0


if __name__ == '__main__':
    sys.exit(main())
