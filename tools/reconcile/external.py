"""Provisional external observations; successful ABI calls are NOT code/source attestation.

Schema source: PharosNetwork/contracts 003d086e4746eec1d7f5ea9b9663b0c151cefc37,
IStaking.sol Git blob e01382dc7459b79cb22c983ce7fb2c8892351391.
"""
from tools.lib import abi
from tools.lib.keccak import keccak256
from .inventory import STAKING, ZERO, event, number
from .rpc import RpcError
from .storage import EvidenceError

STAKING_ABI = [event(n, [('delegator', 'address', True), ('poolId', 'bytes32', True),
                          ('amount', 'uint256', False)] + extra) for n, extra in [
    ('StakeDelegated', [('effectiveEpoch', 'uint256', False)]),
    ('StakeUndelegated', [('unlockEpoch', 'uint256', False)]),
    ('StakeCancelled', [('epochNumber', 'uint256', True)]),
    ('RewardClaimed', []), ('RewardsCompounded', [])]]
STAKING_ABI.append(event('DelegatorWithdrawClaimed', [('delegator', 'address', True), ('poolId', 'bytes32', True), ('amount', 'uint256', False), ('epochNumber', 'uint256', True)]))
STAKING_ABI.append(event('StakeClaimed', [('user', 'address', True), ('amount', 'uint256', False)]))
STAKING_ABI.append(event('CoinMinted', [('beneficiary', 'address', True), ('reason', 'uint8', True), ('amount', 'uint256', False)]))
SOURCE = 'https://github.com/PharosNetwork/contracts/blob/003d086e4746eec1d7f5ea9b9663b0c151cefc37/src/staking/interfaces/IStaking.sol'


def slps(inv):
    return [addr for addr, t in inv['targets'].items() if t['name'].startswith('SlpPharosProxy')]


def observe(collector, snap, logs):
    store, inv = collector.store, collector.inv
    out = store.get('cache/snapshots/external-state.json', {})
    if out.get('snapshot') != snap:
        out = {'snapshot': snap, 'source': SOURCE, 'status': 'UNVERIFIED', 'reads': {}, 'positions': {}, 'roles': {}, 'errors': {}}
    block = snap['number']

    def call(addr, sig, args=()):
        key = addr + ':' + sig + ':' + ','.join(str(x) for x in args)
        if key in out['reads']:
            return out['reads'][key]
        try:
            value = collector.call(addr, sig, args, block)
            out['reads'][key] = value
            store.put('cache/snapshots/external-state.json', out)
            return value
        except RpcError:
            out['errors'][key] = 'UNAVAILABLE'
            store.put('cache/snapshots/external-state.json', out)
            return None

    def array(raw, kind='bytes32'):
        return abi.decode_parameters([{'type': kind + '[]'}], raw)[0] if raw else []

    # Pool list is discovery only; historical pools are added from SLP/Staking logs.
    all_pools = array(call(STAKING, 'getAllValidators()'))
    if len(all_pools) > 512:
        raise EvidenceError('staking pool discovery exceeds bounded 512 pools')
    current_epoch_raw = call(STAKING, 'getBlockchainInfo()')
    epoch = abi.dec_uint(current_epoch_raw) if current_epoch_raw else None
    out['stakingImplementation'] = call(STAKING, 'getImplAddress()')
    for addr in slps(inv):
        if inv['targets'][addr]['abiStatus'] == 'UNVERIFIED_IMPLEMENTATION':
            continue
        pools = set(all_pools) | set(array(call(addr, 'registeredPools()')))
        for log in logs:
            if log['address'].lower() == addr and len(log['topics']) > 2:
                pools.add(log['topics'][2])
        # Only recognized pool events should introduce pool IDs; role addresses are excluded below.
        historical = set()
        for log in logs:
            if log['address'].lower() == addr:
                for e in inv['targets'][addr]['abi']:
                    if e.get('type') == 'event' and any(p['name'] == 'poolId' for p in e['inputs']) and log['topics'] and abi.event_topic(abi.item_signature(e)) == log['topics'][0]:
                        historical.add(abi.decode_event(e, log['topics'], log['data'])['args']['poolId'])
        pools = set(all_pools) | set(array(call(addr, 'registeredPools()'))) | historical
        rows = []
        for pool in sorted(pools):
            raw = call(STAKING, 'getDelegator(bytes32,address)', [pool, addr])
            if not raw:
                continue
            words = abi.words(raw)
            if len(words) != 9:
                out['errors'][addr + ':' + pool] = 'ABI_SHAPE_MISMATCH'
                continue
            values = [int(x, 16) for x in words]
            if not any(values):
                continue
            keys = ['principalStake', 'activeStakeIncludingCompoundedRewards', 'accumulatedRewardPerShare',
                    'settledRewards', 'pendingStakeIncludingCompoundedRewards', 'pendingWithdrawStake',
                    'pendingWithdrawWindow', 'totalRewardsClaimed', 'isPendingUndelegate']
            row = dict(zip(keys, values), poolId=pool)
            reward = call(STAKING, 'getDelegatorReward(bytes32,address)', [pool, addr])
            row['unclaimedRewards'] = abi.dec_uint(reward) if reward else None
            rows.append(row)
        raw_pending = call(STAKING, 'getUserPendingUndelegations(address)', [addr])
        pending = abi.dec_tuple_array(raw_pending) if raw_pending else None
        pending_rows = []
        for row in pending or []:
            if len(row) != 8:
                out['errors'][addr + ':pending'] = 'ABI_SHAPE_MISMATCH'
                continue
            d = dict(zip(['delegator', 'poolId', 'amount', 'unlockEpoch', 'pendingWithdrawWindow', 'processed', 'principalAmount', 'rewardAmount'], row))
            d['maturedAtSnapshotEpoch'] = epoch >= d['unlockEpoch'] if epoch is not None else None
            pending_rows.append(d)
        out['positions'][addr] = {'pools': rows, 'pendingUndelegations': pending_rows,
            'principalNotUnbonding': sum(r['principalStake'] for r in rows),
            'principalUnbondingOrClaimable': sum(r['principalAmount'] for r in pending_rows if not r['processed']),
            'classification': 'PROVISIONAL; principalStake already includes pending principal. Do not add it to active/pending stake totals. pendingWithdrawStake duplicates pendingUndelegations.'}
        call(addr, 'totalBonded()')
        call(addr, 'totalUnbonded()')
        call(addr, 'getBridgeVault()')
        call(addr, 'getRewardsVault()')
    # Enumerate both roles even if they are absent from current documentation.
    for addr, target in inv['targets'].items():
        if target['abiStatus'] not in ('UNVERIFIED_SOURCE_HINT', 'RECORDED_LOCAL_ORIGIN_MANIFEST', 'AUTHENTICATED_RECORDED_RUNTIME'):
            continue
        roles = {}
        for role in ('0x' + '00' * 32, keccak256(b'WHITELIST_ROLE')):
            raw = call(addr, 'getRoleMemberCount(bytes32)', [role])
            if not raw:
                continue
            count = abi.dec_uint(raw)
            if count > 128:
                out['errors'][addr + ':' + role] = 'ROLE_COUNT_BOUND_EXCEEDED'
                continue
            members = []
            for i in range(count):
                member = call(addr, 'getRoleMember(bytes32,uint256)', [role, i])
                if member:
                    members.append(abi.dec_addr(member))
            roles[role] = members
        out['roles'][addr] = roles
        if 'RewardsVault' in target['name']:
            call(addr, 'getSlpPharosProxy()')
            call(addr, 'getVTokenOracle()')
            call(addr, 'getWPROS()')
    store.put('cache/snapshots/external-state.json', out)
    return out


def observe_proxy_slots(collector, snap):
    """Record external proxy implementation/admin slots without assuming proxy shape."""
    from .inventory import IMPL_SLOT
    out = collector.store.get('cache/snapshots/external-state.json', {})
    if out.get('snapshot') != snap:
        out = {'snapshot': snap, 'source': SOURCE, 'status': 'UNVERIFIED', 'reads': {}, 'positions': {}, 'roles': {}, 'errors': {}}
    slots = out.setdefault('proxySlots', {})
    admin_slot = hex(int(keccak256(b'eip1967.proxy.admin'), 16) - 1)
    for addr, target in collector.inv['targets'].items():
        if target['abiStatus'] not in ('UNVERIFIED_SOURCE_HINT', 'RECORDED_LOCAL_ORIGIN_MANIFEST', 'AUTHENTICATED_RECORDED_RUNTIME', 'UNVERIFIED_IMPLEMENTATION') or addr in slots:
            continue
        row = {'interpretation': 'Raw EIP-1967 observations; proxy/source identity remains unverified'}
        for label, slot in [('implementation', IMPL_SLOT), ('admin', admin_slot)]:
            try:
                raw = collector.rpc.call('eth_getStorageAt', [addr, slot, hex(snap['number'])])
                row[label] = abi.dec_addr(raw)
            except (RpcError, ValueError):
                row[label] = None
        code = collector.rpc.call('eth_getCode', [addr, hex(snap['number'])])
        row['runtimeKeccak'] = keccak256(bytes.fromhex(code[2:]))
        if row.get('admin') and row['admin'] != ZERO:
            try:
                row['adminOwner'] = abi.dec_addr(collector.call(row['admin'], 'owner()', [], snap['number']))
            except (RpcError, ValueError):
                row['adminOwner'] = None
        slots[addr] = row
        collector.store.put('cache/snapshots/external-state.json', out)
    return slots


def verify_fixed_contracts(collector, snap):
    """Check recorded non-proxy governance contracts and the legacy WPROS runtime."""
    import json
    from .inventory import ROOT
    from .storage import digest
    cases = [('ProxyAdmin', 'deployments/mainnet/DefaultProxyAdmin.json'),
             ('TimelockController', 'deployments/mainnet/v4/TimelockController.json'),
             ('WPROS', 'tests/fixtures/legacy/mainnet/WPROS.json')]
    out = collector.store.get('cache/snapshots/fixed-code-identities.json', {})
    if out.get('snapshot') != snap:
        out = {'snapshot': snap, 'entries': {}}
    for name, path in cases:
        if name in out['entries']:
            continue
        data = (ROOT / path).read_bytes()
        record = json.loads(data)
        address = record['address'].lower()
        expected = record.get('runtimeKeccak')
        if not expected:
            runtime = record['deployedBytecode']
            runtime = runtime.get('object') if isinstance(runtime, dict) else runtime
            expected = keccak256(bytes.fromhex(runtime.removeprefix('0x')))
        code = collector.rpc.call('eth_getCode', [address, hex(snap['number'])])
        actual = keccak256(bytes.fromhex(code.removeprefix('0x')))
        out['entries'][name] = {'address': address, 'expected': expected, 'actual': actual,
                               'status': 'PASS' if actual == expected else 'FAIL', 'record': path,
                               'recordSha256': digest(data)}
        if name == 'WPROS':
            prior = collector.boundary['context'].get('fixed-code-identities', {}).get('entries', {}).get(name, {}) if collector.boundary else {}
            if prior.get('status') == 'PASS' and prior.get('recordSha256') == digest(data):
                out['entries'][name]['atAuditStart'] = prior['atAuditStart']
            else:
                first = collector.rpc.call('eth_getCode', [address, hex(collector.inv['start'])])
                out['entries'][name]['atAuditStart'] = keccak256(bytes.fromhex(first.removeprefix('0x')))
            if out['entries'][name]['atAuditStart'] != expected:
                out['entries'][name]['status'] = 'FAIL'
        collector.store.put('cache/snapshots/fixed-code-identities.json', out)
    return out


def discover_proxy_admin_history(collector, plan):
    """Backfill the independently deployed admin discovered in external proxy slots."""
    import json
    from .inventory import ROOT
    from .storage import canonical, digest
    out = collector.store.get('cache/snapshots/external-state.json', {})
    template_path = 'deployments/mainnet/DefaultProxyAdmin.json'
    template = json.loads((ROOT / template_path).read_text())
    runtime = template['deployedBytecode']
    runtime = runtime.get('object') if isinstance(runtime, dict) else runtime
    expected = keccak256(bytes.fromhex(runtime.removeprefix('0x')))
    added = []
    for admin in sorted({r['admin'] for r in out.get('proxySlots', {}).values() if r.get('admin') and r['admin'] != ZERO}):
        code = collector.rpc.call('eth_getCode', [admin, hex(plan['snapshot']['number'])])
        actual = keccak256(bytes.fromhex(code.removeprefix('0x')))
        out.setdefault('adminCodeIdentity', {})[admin] = {'runtimeKeccak': actual,
            'matchesRecordedProxyAdminRuntime': actual == expected, 'reference': template_path,
            'snapshot': plan['snapshot']}
        if admin in collector.inv['targets']:
            continue
        collector.inv['targets'][admin] = dict(name='External ProxyAdmin', address=admin,
            start=collector.inv['start'], abi=template['abi'],
            abiStatus='RECORDED_RUNTIME_MATCH' if actual == expected else 'UNVERIFIED_SOURCE_HINT', known={})
        scope = {'filter': {'address': admin}, 'start': collector.inv['start'], 'label': 'External ProxyAdmin history ' + admin}
        scope['id'] = digest(canonical(scope['filter']))[:24]
        if scope['id'] not in {s['id'] for s in plan['scopes']}:
            plan['scopes'].append(scope)
            added.append(scope)
    collector.store.put('cache/snapshots/external-state.json', out)
    collector.store.put('cache/snapshots/inventory.json', collector.inv)
    collector.store.put('checkpoints/plan.json', plan)
    if added:
        collector.sync(dict(plan, scopes=added))
    return bool(added)


def authenticate_incremental(collector, snap, logs):
    """Carry immutable recorded source identities only after current runtime/slot checks.

    No source checkout, compiler or fixed-snapshot closeout is rerun. Changed versions
    require an explicitly reviewed historical ABI timeline before decoding their events.
    """
    from .inventory import PROXY_ABI, decode
    from .storage import canonical, digest
    proof = collector.store.get('cache/snapshots/p0-closeout-attestation.json')
    out = {'snapshot': snap, 'sourceProofSha256': digest(canonical(proof)), 'entries': {}, 'scope': 'Current runtime correspondence only; no reward/backing certification'}
    from .incremental import same
    external_state = collector.store.get('cache/snapshots/external-state.json', {})
    slots = external_state.get('proxySlots', {}) if same(external_state.get('snapshot'), snap) else {}
    for address, target in collector.inv['targets'].items():
        if not (target['name'].startswith('SlpPharosProxy') or 'RewardsVault' in target['name']):
            continue
        row = {'status': 'UNVERIFIED', 'reason': 'Authenticated source mapping missing', 'upgradeEvents': []}
        original = proof.get('proxies', {}).get(address) if proof and proof.get('status') == 'PASS' else None
        if original and original.get('status') == 'PASS' and proof['snapshot']['number'] <= snap['number']:
            impl = original['implementation']
            binding = proof['implementations'][impl]
            if digest(canonical(binding['abi'])) != binding['abiSha256'] or original['abi'] != binding['abi'] + PROXY_ABI:
                raise EvidenceError('authenticated ABI checksum mismatch')
            row.update(implementation=slots.get(address, {}).get('implementation'),
                       sourceSnapshot=proof['snapshot'], sourceIdentity={k: binding[k] for k in
                       ('runtimeKeccak', 'metadataSha256', 'abiSha256', 'sourceKeccak256', 'recordSha256', 'compilerInputSha256')})
            upgrades = []
            for log in logs:
                if log['address'].lower() == address and number(log['blockNumber']) > proof['snapshot']['number']:
                    event = decode(log, PROXY_ABI)
                    if event and event['name'] == 'BeaconUpgraded':
                        upgrades.append({'implementation': 'UNVERIFIED_BEACON', 'block': number(log['blockNumber']), 'transaction': log['transactionHash']})
                    if event and event['name'] == 'Upgraded':
                        upgrades.append({'implementation': event['args']['implementation'].lower(),
                                         'block': number(log['blockNumber']), 'transaction': log['transactionHash']})
            row['upgradeEvents'] = upgrades
            slot = slots.get(address, {})
            if not slot.get('implementation') or not slot.get('runtimeKeccak'):
                row.update(status='UNVERIFIED', reason='Current snapshot proxy slot/runtime evidence missing')
            elif (slot.get('implementation') != impl or slot.get('runtimeKeccak') != original['runtimeKeccak'] or
                    any(e['implementation'] != impl for e in upgrades)):
                row.update(status='BLOCKED', reason='Unknown or changed implementation/proxy history; ABI timeline requires verified mapping')
            elif binding.get('status') != 'PASS':
                row.update(status='BLOCKED', reason='Source identity is not authenticated')
            else:
                try:
                    code = collector.rpc.call('eth_getCode', [impl, hex(snap['number'])])
                    actual = keccak256(bytes.fromhex(code.removeprefix('0x')))
                    row['currentRuntimeKeccak'] = actual
                    row.update(status='PASS' if actual == binding['runtimeKeccak'] else 'BLOCKED',
                               reason='Current implementation matches authenticated full runtime' if actual == binding['runtimeKeccak'] else 'Current runtime differs from authenticated implementation')
                except RpcError:
                    row.update(status='UNVERIFIED', reason='Current implementation runtime unavailable')
            row['adminChanged'] = slot.get('admin') != original['admin']
        out['entries'][address] = row
    collector.store.put('cache/snapshots/incremental-external.json', out)
    return out


def bound_inventory(inv, proof, current, snap):
    """Never install an old external ABI unless this snapshot proves its runtime history."""
    import copy
    from .inventory import PROXY_ABI
    from .incremental import same
    from .storage import canonical, digest
    if same(current.get('snapshot'), snap) and current.get('sourceProofSha256') != digest(canonical(proof)):
        raise EvidenceError('snapshot source proof binding mismatch')
    result = copy.deepcopy(inv)
    for address, target in result['targets'].items():
        if not (target['name'].startswith('SlpPharosProxy') or 'RewardsVault' in target['name']):
            continue
        row = current.get('entries', {}).get(address, {}) if same(current.get('snapshot'), snap) else {}
        if row.get('status') == 'PASS' and proof and address in proof['proxies']:
            target['abi'] = proof['proxies'][address]['abi']
            target['abiStatus'] = 'AUTHENTICATED_RECORDED_RUNTIME'
        else:
            target['abi'] = PROXY_ABI
            target['abiStatus'] = 'UNVERIFIED_IMPLEMENTATION'
    return result
