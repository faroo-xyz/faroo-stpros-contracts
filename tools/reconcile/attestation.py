"""Bind existing local deployment artifacts to cached mainnet runtime; no build or RPC.

PASS is recorded-build correspondence, including the full runtime and its metadata.
It does not claim that a compiler was re-executed during this closeout.
"""
import json
import subprocess
from pathlib import Path
from tools.lib import abi
from tools.lib.keccak import keccak256
from tools.release.provenance import evm  # Existing offline CBOR/code-identity helpers; no compilation or RPC.
from .inventory import PROXY_ABI, number, decode
from .storage import EvidenceError, canonical, digest, safe

SNAPSHOT = {'number': 21647956, 'hash': '0x48563791c4c5c1dad91d3c832ac0c559fbc5b1fe250c12142b6290145cfad6bf'}
DEPLOYMENT_COMMIT = '8943b4b2247354ccb5dc0c314649fe656517d3af'
SOURCE_COMMITS = {
    'contracts/pharos/SlpPharosProxy.sol': 'efc989b032a1ecd30a06bd62ddf343b48d9c8f57',
    'contracts/pharos/SlpPharosRewardsVault.sol': 'dc7df6abc7f29eec40d508540e926cd11d09d364',
    'contracts/interfaces/pharos/IStaking.sol': '213d4bf1fe6ff7f1a36697aca9086a52331bd40a',
    'contracts/interfaces/pharos/IOracle.sol': '6338c28831b634abfa8d671ce508cb84b8f4f962',
}


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def solc_ipfs_multihash(metadata):
    """Solc's single-chunk IPFS UnixFS file hash, NOT sha256(JSON) directly.

    Larger/chunked metadata is deliberately unsupported rather than guessed.
    """
    data = metadata.encode()
    if len(data) > 262144:
        raise EvidenceError('chunked metadata requires independent IPFS verification')
    unixfs = b'\x08\x02\x12' + varint(len(data)) + data + b'\x18' + varint(len(data))
    node = b'\x0a' + varint(len(unixfs)) + unixfs
    return '1220' + digest(node)


def git_bytes(repo, revision, path):
    path = str(path)
    if '..' in Path(path).parts or not path.startswith(('contracts/', 'deployments/pharos_pacific_mainnet/')):
        raise EvidenceError('source provenance path outside allowlist')
    safe(repo / path)
    return subprocess.check_output(['git', '-C', str(repo), 'show', revision + ':' + path])


def runtime_binding(record, cached):
    """No stripping metadata, immutable masking, or replacing linked addresses."""
    if cached['snapshot']['number'] != SNAPSHOT['number'] or cached['snapshot']['hash'] != SNAPSHOT['hash']:
        raise EvidenceError('implementation evidence belongs to a different snapshot')
    runtime = record['deployedBytecode']
    runtime = runtime.get('object') if isinstance(runtime, dict) else runtime
    expected = bytes.fromhex(runtime.removeprefix('0x'))
    actual = bytes.fromhex(cached['runtimeCode'].removeprefix('0x'))
    if expected != actual or keccak256(actual) != cached['identity']['keccak256']:
        raise EvidenceError('full runtime mismatch')
    parsed_identity = evm.code_identity(cached['runtimeCode'])
    if parsed_identity['cbor_metadata'] != cached['identity']['cbor_metadata']:
        raise EvidenceError('cached metadata summary differs from runtime bytes')
    metadata_text = record['metadata']
    metadata = json.loads(metadata_text)
    if solc_ipfs_multihash(metadata_text) != parsed_identity['cbor_metadata'].get('ipfs'):
        raise EvidenceError('metadata is not bound to the deployed runtime CID')
    version = metadata['compiler']['version'].split('+')[0]
    compiler_bytes = bytes(int(v) for v in version.split('.')).hex()
    if parsed_identity['cbor_metadata'].get('solc') != compiler_bytes:
        raise EvidenceError('compiler version metadata mismatch')
    if metadata['output']['abi'] != record['abi']:
        raise EvidenceError('ABI differs from runtime-bound compiler metadata')
    if metadata['settings'].get('libraries') or record.get('deployedLinkReferences'):
        raise EvidenceError('linked library attestation requires explicit address bindings')
    hashes = {}
    for path, source in metadata['sources'].items():
        if 'content' not in source or keccak256(source['content'].encode()) != source['keccak256']:
            raise EvidenceError('literal compiler source checksum mismatch')
        hashes[path] = source['keccak256']
    return {'runtimeKeccak': keccak256(actual), 'metadataSha256': digest(metadata_text.encode()),
            'metadataIpfsMultihash': solc_ipfs_multihash(metadata_text),
            'abiSha256': digest(canonical(record['abi'])), 'sourceKeccak256': hashes,
            'compiler': metadata['compiler'], 'settings': metadata['settings'],
            'comparison': 'FULL_BYTES including metadata; no immutable masking; no linked libraries',
            'compilation': 'NOT RUN; existing recorded build used'}


def authenticate(repo, store, logs):
    repo = safe(repo).resolve()
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    cached = store.get('cache/snapshots/external-implementations.json', {})
    external = store.get('cache/snapshots/external-state.json', {})
    headers = store.get('cache/snapshots/headers.json', {})
    if external.get('snapshot', {}).get('hash') != SNAPSHOT['hash']:
        raise EvidenceError('external snapshot binding missing')
    out = {'snapshot': SNAPSHOT, 'sourceRepository': str(repo), 'repositoryHead': head,
           'deploymentCommit': DEPLOYMENT_COMMIT, 'implementations': {}, 'proxies': {},
           'records': {}, 'status': 'PASS', 'limitations': []}
    folder = repo / 'deployments/pharos_pacific_mainnet'
    chain = safe(folder / '.chainId').read_text().strip()
    if int(chain) != 1672:
        raise EvidenceError('SLP deployment registry is not chain 1672')
    # Named allowlist excludes unrelated network records and secret material.
    for name in ('SlpPharosProxy', 'SlpPharosProxy_1', 'SlpPharosRewardsVault'):
        rel = f'deployments/pharos_pacific_mainnet/{name}_Implementation.json'
        raw = safe(repo / rel).read_bytes()
        if git_bytes(repo, DEPLOYMENT_COMMIT, rel) != raw:
            raise EvidenceError('deployment artifact differs from pinned historical commit')
        record = json.loads(raw); impl = record['address'].lower()
        if impl not in cached:
            raise EvidenceError('no cached runtime for local implementation record')
        binding = runtime_binding(record, cached[impl])
        meta = json.loads(record['metadata'])
        project_sources = {}
        for path, source in meta['sources'].items():
            if not path.startswith('contracts/'):
                continue
            content = source['content'].encode()
            if git_bytes(repo, DEPLOYMENT_COMMIT, path) != content:
                raise EvidenceError('compiler source differs from deployment-commit source')
            introduced = SOURCE_COMMITS.get(path)
            if introduced and git_bytes(repo, introduced, path) != content:
                raise EvidenceError('source-change commit is not the compiled source')
            project_sources[path] = {'sourceChangeCommit': introduced, 'keccak256': source['keccak256'],
                                     'headFileMatches': safe(repo / path).read_bytes() == content}
        input_rel = f'deployments/pharos_pacific_mainnet/solcInputs/{record["solcInputHash"]}.json'
        input_raw = safe(repo / input_rel).read_bytes()
        if git_bytes(repo, DEPLOYMENT_COMMIT, input_rel) != input_raw:
            raise EvidenceError('compiler input differs from historical record')
        compiler_input = json.loads(input_raw)
        for key, default in [('optimizer', {}), ('evmVersion', None), ('libraries', {}), ('remappings', []), ('viaIR', False)]:
            if compiler_input['settings'].get(key, default) != meta['settings'].get(key, default):
                raise EvidenceError('compiler input settings mismatch')
        if compiler_input['settings'].get('metadata', {}).get('useLiteralContent') is not True:
            raise EvidenceError('literal-source compiler input required')
        if any(compiler_input['sources'].get(p, {}).get('content') != v['content'] for p, v in meta['sources'].items()):
            raise EvidenceError('compiler input source closure mismatch')
        binding.update(status='PASS', abi=record['abi'], sourceFiles=project_sources,
                       record=rel, recordSha256=digest(raw), compilerInput=input_rel,
                       compilerInputSha256=digest(input_raw), deploymentTransaction=record['transactionHash'],
                       deploymentBlock=number(record['receipt']['blockNumber']))
        out['implementations'][impl] = binding
        proxy_rel = f'deployments/pharos_pacific_mainnet/{name}_Proxy.json'
        proxy_raw = safe(repo / proxy_rel).read_bytes(); proxy = json.loads(proxy_raw)
        if git_bytes(repo, DEPLOYMENT_COMMIT, proxy_rel) != proxy_raw:
            raise EvidenceError('proxy record differs from deployment commit')
        address = proxy['address'].lower(); slot = external['proxySlots'][address]
        if slot['implementation'] != impl:
            raise EvidenceError('proxy slot does not select artifact implementation')
        proxy_code = proxy['deployedBytecode']
        if keccak256(bytes.fromhex(proxy_code.removeprefix('0x'))) != slot['runtimeKeccak']:
            raise EvidenceError('proxy runtime mismatch')
        if proxy['args'][0].lower() != impl or proxy['args'][1].lower() != slot['admin']:
            raise EvidenceError('constructor target/admin mismatch')
        tx = proxy['transactionHash'].lower()
        receipt = store.get(f'cache/receipts/{tx}.json')
        transaction = store.get(f'cache/receipts/{tx}-transaction.json')
        if not receipt or not transaction or number(receipt['status']) != 1 or receipt['contractAddress'].lower() != address:
            raise EvidenceError('successful cached proxy deployment receipt missing')
        if headers[receipt['blockNumber']]['hash'] != receipt['blockHash']:
            raise EvidenceError('proxy deployment receipt is not header-bound')
        constructor = next(i for i in proxy['abi'] if i['type'] == 'constructor')
        init_code = proxy['bytecode'] + abi.encode_parameters(constructor['inputs'], [a.lower() if isinstance(a, str) else a for a in proxy['args']])[2:]
        if transaction['input'].lower() != init_code.lower():
            raise EvidenceError('proxy creation input does not match artifact and arguments')
        history = []
        for log in logs:
            if log['address'].lower() == address:
                event = decode(log, PROXY_ABI)
                if event and event['name'] == 'Upgraded':
                    history.append({'implementation': event['args']['implementation'].lower(), 'transaction': log['transactionHash'],
                                    'block': number(log['blockNumber']), 'logIndex': number(log['logIndex'])})
        if len(history) != 1 or history[0]['implementation'] != impl or history[0]['transaction'] != tx:
            raise EvidenceError('implementation history requires additional version mappings')
        initialization = abi.decode_function(record['abi'], proxy['args'][2])
        out['proxies'][address] = {'name': name, 'implementation': impl, 'admin': slot['admin'],
            'runtimeKeccak': slot['runtimeKeccak'], 'abi': record['abi'] + PROXY_ABI,
            'initialization': initialization, 'upgradeHistory': history, 'status': 'PASS',
            'proxyRecord': proxy_rel, 'proxyRecordSha256': digest(proxy_raw)}
        out['records'][rel] = digest(raw); out['records'][proxy_rel] = digest(proxy_raw); out['records'][input_rel] = digest(input_raw)
    return out
