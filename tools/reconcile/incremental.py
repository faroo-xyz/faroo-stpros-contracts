"""Finalized history seals and immutable snapshot reports; shared raw evidence stays in cache."""
import os
from fractions import Fraction
from .storage import EvidenceError, digest, read, atomic, safe

REPORTS = ('RECONCILIATION-REPORT.md', 'RECONCILIATION-RESULTS.json', 'DATA-MANIFEST.json',
           'RPC-USAGE.json', 'ANOMALIES.csv', 'INCREMENTAL-SUMMARY.json')
CONTEXT = ('state', 'identities', 'fixed-code-identities', 'reward-evidence', 'external-state',
           'incremental-external', 'migration-state')


def identity(snapshot):
    return str(int(snapshot['number'])) + '-' + snapshot['hash'].removeprefix('0x')


def same(a, b):
    return bool(a and b and all(a.get(k) == b.get(k) for k in ('number', 'hash')))


def report_dir(store, snapshot):
    return store.root / 'output/reconciliation/snapshots' / identity(snapshot)


def load_boundary(store, key=None):
    key = key or store.get('checkpoints/completed-snapshot.json', {}).get('key')
    return store.get('checkpoints/finalized/' + key + '.json') if key else None


def validate_boundary(store, boundary):
    """Local checksum verification is never replaced by the canonical RPC boundary check."""
    headers = store.get('cache/snapshots/headers.json', {})
    for key, header in boundary['headers'].items():
        if headers.get(key) != header:
            raise EvidenceError('sealed historical header changed')
    for rel, checksum in boundary['files'].items():
        path = safe(store.root / rel)
        if not path.is_file() or digest(path.read_bytes()) != checksum:
            raise EvidenceError('sealed historical evidence changed or missing')
    for rel, checksum in boundary['reports'].items():
        if digest(safe(store.root / rel).read_bytes()) != checksum:
            raise EvidenceError('immutable snapshot report changed')


def seal(store, plan, result, folder, source=None):
    from .collect import cached_logs
    logs, chunks, complete = cached_logs(store, plan)
    snap = plan['snapshot']
    if not complete or not result.get('collectionComplete') or result.get('executionBlocker'):
        raise EvidenceError('cannot finalize incomplete collection')
    if snap.get('finality') != 'provider-finalized-tag' or not same(result.get('snapshot'), snap):
        raise EvidenceError('trusted history requires provider-finalized snapshot')
    headers = store.get('cache/snapshots/headers.json', {})
    for m in chunks:
        for block, value in m['headers'].items():
            if headers.get(hex(int(block)), {}).get('hash') != value:
                raise EvidenceError('historical interval anchor differs from cached header')
    for log in logs:
        if headers.get(log['blockNumber'], {}).get('hash') != log['blockHash']:
            raise EvidenceError('historical event hash differs from cached header')
    files = {}
    for category in ('manifests', 'receipts', 'traces'):
        for p in sorted((store.root / 'cache' / category).glob('*.json')):
            store.get(str(p.relative_to(store.root)))
            files[str(p.relative_to(store.root))] = digest(p.read_bytes())
    for m in chunks:
        files[m['file']] = m['sha256']
    # The closeout source proof is immutable and independent of future snapshot state.
    p = store.root / 'cache/snapshots/p0-closeout-attestation.json'
    if p.exists():
        store.get(str(p.relative_to(store.root)))
        files[str(p.relative_to(store.root))] = digest(p.read_bytes())
    report_source = source or folder
    reports = {str((folder / p.name).relative_to(store.root)): digest(p.read_bytes())
               for p in report_source.iterdir() if p.is_file() and p.name in REPORTS}
    closeout = store.root / 'output/reconciliation/p0-closeout/RECONCILIATION-FOLLOWUP-RESULTS.json'
    if closeout.exists():
        reports[str(closeout.relative_to(store.root))] = digest(closeout.read_bytes())
    published_proof = closeout.parent / 'SOURCE-ATTESTATION.json'
    if published_proof.exists():
        reports[str(published_proof.relative_to(store.root))] = digest(published_proof.read_bytes())
    boundary = {'snapshot': snap, 'scopes': plan['scopes'], 'files': files, 'headers': headers,
                'reports': reports, 'result': str((folder / 'RECONCILIATION-RESULTS.json').relative_to(store.root)),
                'stagingReport': str(report_source.relative_to(store.root)),
                'context': {name: store.get('cache/snapshots/' + name + '.json', {}) for name in CONTEXT}}
    key = identity(snap)
    existing = load_boundary(store, key)
    if existing:
        validate_boundary(store, existing)
        return existing
    store.put('checkpoints/finalized/' + key + '.json', boundary)
    if source is None:
        store.put('checkpoints/completed-snapshot.json', {'key': key})
    return boundary


def bootstrap(store, plan):
    """Import the existing completed audit, checking its published manifest before trusting it."""
    boundary = load_boundary(store)
    if boundary:
        validate_boundary(store, boundary)
        return boundary
    out = store.root / 'output/reconciliation'
    result = read(out / 'RECONCILIATION-RESULTS.json')
    manifest = read(out / 'DATA-MANIFEST.json')
    anchor = store.get('cache/snapshots/final-anchor-verification.json', {})
    if (anchor.get('status') != 'MATCH' or anchor.get('chainId') != 1672 or
            not same(anchor.get('snapshot'), plan['snapshot']) or
            not same(manifest.get('snapshot'), plan['snapshot'])):
        raise EvidenceError('previous finalized audit anchor is missing')
    for rel, checksum in manifest['evidenceFiles'].items():
        if digest(safe(store.root / rel).read_bytes()) != checksum:
            raise EvidenceError('previous audit manifest checksum mismatch')
    if (not result.get('collectionComplete') or result.get('executionBlocker') or
            not same(result.get('snapshot'), plan['snapshot']) or plan['snapshot'].get('finality') != 'provider-finalized-tag'):
        raise EvidenceError('previous audit is not a completed finalized snapshot')
    from .collect import cached_logs
    _, chunks, complete = cached_logs(store, plan)
    if not complete or chunks != manifest['chunks']:
        raise EvidenceError('previous audit event manifest mismatch or incomplete')
    published_proof = out / 'p0-closeout/SOURCE-ATTESTATION.json'
    if published_proof.exists():
        proof = store.get('cache/snapshots/p0-closeout-attestation.json')
        closeout = read(out / 'p0-closeout/RECONCILIATION-FOLLOWUP-RESULTS.json')
        if (proof != read(published_proof) or proof != closeout.get('sourceAttestation') or
                proof.get('status') != 'PASS' or not same(proof.get('snapshot'), plan['snapshot'])):
            raise EvidenceError('authenticated source proof differs from published fixed-snapshot closeout')
    # Preserve first-round and P0 outputs exactly; publish an independent immutable copy.
    commit_snapshot(store, plan, result, out)
    return load_boundary(store)


def recover_snapshot(store):
    """Finish a previously prepared publication without recomputing or overwriting evidence."""
    snap = store.get('checkpoints/snapshot.json')
    if not snap:
        return
    key = identity(snap)
    boundary = load_boundary(store, key)
    if not boundary or store.get('checkpoints/completed-snapshot.json', {}).get('key') == key:
        return
    folder = report_dir(store, snap)
    if not folder.exists():
        source = safe(store.root / boundary['stagingReport'])
        for name in REPORTS:
            rel = str((folder / name).relative_to(store.root))
            if rel in boundary['reports'] and digest(safe(source / name).read_bytes()) != boundary['reports'][rel]:
                raise EvidenceError('prepared snapshot report changed')
        publish(store, source, folder)
    validate_boundary(store, boundary)
    store.put('checkpoints/completed-snapshot.json', {'key': key})


def commit_snapshot(store, plan, result, source):
    folder = report_dir(store, plan['snapshot'])
    # Prepare the sealed evidence and report checksums BEFORE the directory commit point.
    # Recovery can finish either side of the rename; it never regenerates a sealed report.
    seal(store, plan, result, folder, source=source)
    publish(store, source, folder)
    recover_snapshot(store)
    return folder


def publish(store, source, destination):
    """Directory rename is the commit point. Interrupted staging cannot replace a completed report."""
    if destination.exists():
        return
    staging = destination.parent / ('.pending-' + destination.name)
    staging.mkdir(parents=True, exist_ok=True)
    for name in REPORTS:
        path = source / name
        if path.exists():
            atomic(staging / name, path.read_bytes())
    os.rename(staging, destination)
    fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def decimal(value, places=18):
    if value is None:
        return None
    sign = '-' if value < 0 else ''
    value = abs(value)
    return f'{sign}{value // 10**places}.{value % 10**places:0{places}d}'


def amount(before, after, unit='PROS'):
    return {'previous': before, 'current': after,
            'change': after - before if before is not None and after is not None else None,
            'unit': unit, 'currentDecimal': decimal(after),
            'changeDecimal': decimal(after - before) if before is not None and after is not None else None}


def summary(store, plan, result, logs, states):
    boundary = load_boundary(store, plan.get('historicalBoundary')) if plan.get('historicalBoundary') else None
    if not boundary:
        return None
    old = read(store.root / boundary['result'])
    previous = boundary['snapshot']
    old_states = boundary['context']['state']
    new_logs = [l for l in logs if int(l['blockNumber'], 16) > previous['number']]
    txs = sorted({l['transactionHash'].lower() for l in new_logs})
    a, b = old.get('replay', {}), result['replay']
    accounting = {k: amount(a.get(k), b.get(k), 'stPROS' if k in ('supply', 'V') else 'PROS')
                  for k in ('supply', 'T', 'V', 'liability', 'queued', 'completed')}
    rates = [Fraction(r['T'], r['V']) if r.get('V') else None for r in (a, b)]
    def ratio(f):
        return {'numerator': f.numerator, 'denominator': f.denominator,
                'decimal': decimal(f.numerator * 10**18 // f.denominator)} if f is not None else None
    accounting['PROSPerStPROS'] = {'previous': ratio(rates[0]), 'current': ratio(rates[1]),
        'change': ratio(rates[1] - rates[0]) if all(x is not None for x in rates) else None}
    movements = {kind: {'assets': sum(c['assets'] for c in result['conversions'] if c['kind'] == kind and c['transaction'] in txs),
                       'shares': sum(c['shares'] for c in result['conversions'] if c['kind'] == kind and c['transaction'] in txs)}
                 for kind in ('Deposit', 'Withdraw')}
    for row in movements.values():
        row.update(assetsDecimal=decimal(row['assets']), sharesDecimal=decimal(row['shares']))
    old_checks = old['checks']
    closeout_rel = 'output/reconciliation/p0-closeout/RECONCILIATION-FOLLOWUP-RESULTS.json'
    if closeout_rel in boundary['reports']:
        closeout = read(store.root / closeout_rel)
        if same(closeout.get('snapshot'), previous):
            old_checks = closeout['checks'] + closeout.get('newFindings', [])
    before = {c['id']: c['status'] for c in old_checks}
    after = {c['id']: c['status'] for c in result['checks']}
    changes = [{'id': k, 'previous': before.get(k), 'current': after.get(k)}
               for k in sorted(before.keys() | after.keys()) if before.get(k) != after.get(k)]
    custody = {}
    for addr, row in states.get('custody', {}).items():
        if any(n in row.get('name', '') for n in ('BridgeVault', 'SlpDistributor', 'RewardsVault')):
            custody[addr] = {'name': row['name'], **{k: amount(old_states.get('custody', {}).get(addr, {}).get(k), row.get(k)) for k in ('native', 'WPROS')}}
    staking = {}
    for addr, positions in result.get('externalState', {}).get('positions', {}).items():
        keys = ('activeStakeIncludingCompoundedRewards', 'pendingStakeIncludingCompoundedRewards', 'pendingWithdrawStake', 'unclaimedRewards')
        staking[addr] = {}
        for key in keys:
            values = [p.get(key) for p in positions.get('pools', [])]
            total = sum(values) if values and all(v is not None for v in values) else None
            staking[addr][key] = {'raw': total, 'decimal': decimal(total)}
        staking[addr]['classification'] = positions.get('classification', 'PROVISIONAL')
    relevant_events = [e for e in result.get('routing', []) + result.get('governance', [])
                       if int(e['raw']['blockNumber'],16) > previous['number']]
    return {'previousSnapshot': previous, 'currentSnapshot': plan['snapshot'],
            'newBlockRange': [previous['number'] + 1, plan['snapshot']['number']],
            'newEvents': len(new_logs), 'relevantTransactions': txs, 'upgradeRoutingRoleEvents': relevant_events, 'accounting': accounting,
            'movements': movements, 'custodyObservations': custody,
            'snapshotObservations': {k: amount(old_states.get(k), states.get(k), 'stPROS' if k in ('supply', 'V') else 'PROS')
                                     for k in ('supply', 'T', 'V', 'queued', 'completed')},
            'bridgeBalance': amount(old_states.get('bridgeBalance'), states.get('bridgeBalance')),
            'stakingObservations': staking,
            'reportedRewards': amount(old.get('rewardProvenance', {}).get('vaultReportedTotal'), result.get('rewardProvenance', {}).get('vaultReportedTotal')),
            'acceptedOracleRewards': amount(old.get('rewardProvenance', {}).get('acceptedOracleReward'), result.get('rewardProvenance', {}).get('acceptedOracleReward')),
            'findingChanges': changes, 'resolvedFindings': [c for c in changes if c['previous'] in ('FAIL', 'UNVERIFIED', 'BLOCKED') and c['current'] == 'PASS'],
            'introducedFindings': [c for c in changes if c['previous'] is None],
            'findingsByStatus': {s: [c['id'] for c in result['checks'] if c['status'] == s] for s in ('PASS', 'FAIL', 'UNVERIFIED', 'BLOCKED')},
            'rpcRun': store.get('checkpoints/rpc-run.json', {}), 'rpcCumulative': result['rpcUsage'],
            'provisionalAssets': 'All underlying active/pending/unbonding/claimable stake and reward origin remain UNVERIFIED. Custody is observed cash, not attributable backing. No economic solvency conclusion.',
            'units': 'Raw amounts are integers at 18 decimals. stPROS shares are never subtracted from PROS assets.'}
