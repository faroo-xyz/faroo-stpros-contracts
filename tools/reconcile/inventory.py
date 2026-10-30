"""Repository evidence inventory; deployment records are evidence, not live attestations."""
import json
from functools import lru_cache
import re
import subprocess
from pathlib import Path
from tools.lib import abi
from tools.lib.keccak import keccak256
from .storage import canonical, digest, read, safe

ROOT = Path(__file__).resolve().parents[2]
CORE = ('StPROS', 'Oracle', 'BridgeVault', 'SlpDistributor')
IMPL_SLOT = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
ZERO = '0x' + '00' * 20
STAKING = '0x4100000000000000000000000000000000000000'


def number(n):
    return int(n, 16) if isinstance(n, str) and n.startswith('0x') else int(n)


def event(name, fields):
    return {'type': 'event', 'name': name, 'inputs': [
        {'name': n, 'type': t, 'indexed': i} for n, t, i in fields]}


UPGRADE = event('Upgraded', [('implementation', 'address', True)])
PROXY_ABI = [UPGRADE, event('AdminChanged', [('previousAdmin', 'address', False), ('newAdmin', 'address', False)]),
             event('BeaconUpgraded', [('beacon', 'address', True)])]
TRANSFER = event('Transfer', [('from', 'address', True), ('to', 'address', True), ('value', 'uint256', False)])
WPROS_ABI = [TRANSFER, event('Deposit', [('dst', 'address', True), ('wad', 'uint256', False)]),
             event('Withdrawal', [('src', 'address', True), ('wad', 'uint256', False)])]


def source_event_hints(path):
    """Explicitly UNVERIFIED ABI hints from external source, never trusted for economic PASS."""
    text = safe(path).read_text()
    result = []
    for name, fields in re.findall(r'event\s+(\w+)\s*\(([^)]*)\)\s*;', text):
        parsed = []
        for field in fields.split(','):
            bits = field.split()
            if len(bits) < 2:
                break
            parsed.append((bits[-1], bits[0], 'indexed' in bits))
        else:
            result.append(event(name, parsed))
    for name in ('RoleGranted', 'RoleRevoked'):
        result.append(event(name, [('role', 'bytes32', True), ('account', 'address', True), ('sender', 'address', True)]))
    result.append(UPGRADE)
    return result


def discover():
    registry = read(ROOT / 'deployments/registry.json')['networks']['mainnet']
    address_table = read(ROOT / 'deployments/mainnet/v4/addresses.json')
    history = read(ROOT / 'snapshots/deployed/provenance.json')['implementation_history']['mainnet']
    targets, implementations, files, sources = {}, {}, {}, {}

    def record(rel):
        p = safe(ROOT / rel)
        data = p.read_bytes()
        files[rel] = digest(data)
        return json.loads(data)

    for name in CORE:
        current = record(f'deployments/mainnet/v4/{name}.json')
        proxy_rel = f'deployments/mainnet/{name}_Proxy.json'
        if not (ROOT / proxy_rel).exists():
            proxy_rel = f'deployments/mainnet/v4/{name}_Proxy.json'
        proxy = record(proxy_rel)
        addr = current['address'].lower()
        targets[addr] = {'name': name, 'address': addr, 'start': number(proxy['receipt']['blockNumber']),
                         'abi': current['abi'] + [UPGRADE], 'abiStatus': 'RECORDED', 'known': {}}
        previous = history.get(addr, {}).get('history', [])
        for h in previous:
            rec = record(h['matching_records'][0])
            imp = h['implementation'].lower()
            targets[addr]['known'][imp] = 'V1'
            implementations[imp] = {'version': 'V1', 'contract': name, 'abi': rec['abi'],
                                   'runtimeKeccak': h['code_keccak256'], 'record': h['matching_records'][0]}
            meta = rec.get('metadata')
            meta = json.loads(meta) if isinstance(meta, str) else meta or {}
            for source, entry in meta.get('sources', {}).items():
                if source.startswith('project/contracts/') and source.endswith('.sol') and 'content' in entry:
                    computed = keccak256(entry['content'].encode())
                    sources[f'{imp}:{source}'] = {'keccak256': computed,
                        'metadataMatch': computed == entry.get('keccak256'), 'origin': h['matching_records'][0]}
        rec = record(f'deployments/mainnet/v4/{name}_Implementation.json')
        imp = rec['address'].lower()
        targets[addr]['known'][imp] = 'V4'
        implementations[imp] = {'version': 'V4', 'contract': name, 'abi': current['abi'],
                               'runtimeKeccak': rec['runtimeKeccak'], 'record': f'deployments/mainnet/v4/{name}_Implementation.json'}

    start = min(t['start'] for t in targets.values())
    for row in registry['governance']:
        if row.get('abi') and row['name'] in ('TimelockController', 'ProxyAdmin'):
            rec = record(row['abi']['file'])
            addr = row['address'].lower()
            targets[addr] = dict(name=row['name'], address=addr, start=number(rec['receipt']['blockNumber']),
                                 abi=rec['abi'], abiStatus='RECORDED', known={})
    for row in registry['external']:
        if row['name'] == 'WPROS':
            continue
        addr = row['address'].lower()
        source = 'contracts/SlpPharosRewardsVault.sol' if 'RewardsVault' in row['name'] else 'contracts/SlpPharosProxy.sol'
        targets[addr] = dict(name=row['name'], address=addr, start=start,
                             abi=source_event_hints(ROOT / source), abiStatus='UNVERIFIED_SOURCE_HINT', known={})
    for rel in ['deployments/registry.json', 'deployments/mainnet/v4/addresses.json',
                'deployments/mainnet/v4/V4_UpgradeReceipt.json', 'snapshots/deployed/provenance.json',
                'tools/ci/r4-reference.json']:
        record(rel)
    for p in sorted((ROOT / 'contracts').rglob('*.sol')):
        if 'release' not in p.parts:
            files[str(p.relative_to(ROOT))] = digest(safe(p).read_bytes())
    return {'chainId': 1672, 'sourceCommit': subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
        'targets': targets, 'implementations': implementations,
        'files': files, 'legacySources': sources, 'release': registry['release'],
        'addresses': address_table, 'start': min(t['start'] for t in targets.values()),
        'limitations': ['External SLP/RewardsVault deployment-source identity is unverified.',
                         'Underlying Pharos Staking verified ABI/source is absent; principal positions cannot be attested.',
                         'V4 bytecode/source mapping uses recorded canonical artifact identities; recompilation NOT RUN.']}


def abi_hashes(inv):
    from .external import STAKING_ABI
    result = {k: digest(canonical(v['abi'])) for k, v in inv['implementations'].items()}
    result.update({'target:' + k: digest(canonical(v['abi'])) for k, v in inv['targets'].items()})
    result['WPROS-recorded-behavior-schema'] = digest(canonical(WPROS_ABI))
    result['Staking-provisional-public-schema'] = digest(canonical(STAKING_ABI))
    return result


@lru_cache(maxsize=512)
def topic(signature):
    return abi.event_topic(signature)


def decode(log, items):
    if not log['topics']:
        return None
    for item in items:
        if item.get('type') == 'event' and topic(abi.item_signature(item)) == log['topics'][0]:
            # Decode only the selected version ABI; unknown signatures remain explicit.
            result = abi.decode_event(item, log['topics'], log['data'])
            return {'name': item['name'], 'args': result['args']}
    return None


def scopes(inv):
    # Batch independent addresses and indexed holders to minimize provider requests.
    result = [{'filter': {'address': sorted(inv['targets'])}, 'start': inv['start'], 'label': 'protocol'}]
    wpros = inv['addresses']['asset']['WPROS'].lower()
    owners = ['0x' + a[2:].rjust(64, '0') for a, t in sorted(inv['targets'].items())
              if t['name'] not in ('ProxyAdmin', 'TimelockController')]
    transfer = abi.event_topic('Transfer(address,address,uint256)')
    for label, topics in [('out', [transfer, owners]), ('in', [transfer, None, owners]),
                          ('wrap', [[abi.event_topic('Deposit(address,uint256)'),
                                     abi.event_topic('Withdrawal(address,uint256)')], owners])]:
        result.append({'filter': {'address': wpros, 'topics': topics}, 'start': inv['start'],
                       'label': 'WPROS:' + label})
    from .external import slps
    result.append({'filter': {'address': STAKING, 'topics': [None, ['0x' + a[2:].rjust(64, '0') for a in slps(inv)]]},
                   'start': inv['start'], 'label': 'Staking:protocol-delegators'})
    for row in result:
        row['id'] = digest(canonical(row['filter']))[:24]
    return result


def source_binding(inv):
    """Bind pinned project-source closure and decoded ABI to frozen canonical records.

    This carries recorded build evidence; it does not claim a fresh compilation.
    """
    reference_path = ROOT / 'tools/ci/r4-reference.json'
    raw = reference_path.read_bytes()
    from .storage import EvidenceError
    if digest(raw) != inv['files']['tools/ci/r4-reference.json']:
        raise EvidenceError('pinned artifact reference changed')
    reference = json.loads(raw)
    result = {}
    for address, implementation in inv['implementations'].items():
        if implementation['version'] != 'V4':
            continue
        name = implementation['contract']
        data = (ROOT / implementation['record']).read_bytes()
        if digest(data) != inv['files'][implementation['record']]:
            raise EvidenceError('pinned deployment record changed')
        record = json.loads(data)
        matches = {p.removeprefix('project/'): inv['files'].get(p.removeprefix('project/')) == checksum
                   for p, checksum in reference['contracts'][name]['sourceClosureSha256'].items()
                   if p.startswith('project/')}
        abi_match = digest((json.dumps(implementation['abi'], sort_keys=True, indent=2) + '\n').encode()) == record['artifact']['abiSha256']
        result[address] = {'status': 'PASS' if all(matches.values()) and abi_match else 'BLOCKED',
                           'sourceClosureProjectFiles': matches, 'abiMatchesRecordedArtifact': abi_match,
                           'scope': 'Pinned project source and ABI matched to recorded canonical build; recompilation NOT RUN'}
    return result
