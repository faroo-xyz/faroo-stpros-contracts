#!/usr/bin/env python3
"""Reconstitute the two deployed SLP artifacts offline, in an approved export.

Sources come only from this module's files. Historical inputs/records are
append-only comparison evidence, never a fallback for missing module sources.
No reference generation, compiler download, RPC or transaction route exists.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parents[1]
ROOT = MODULE.parent
sys.path.insert(0, str(ROOT / 'tools/lib'))
import common  # noqa: E402
from keccak import erc7201_slot, keccak256  # noqa: E402

NAMES = ('SlpPharosProxy', 'SlpPharosRewardsVault')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(relative):
    """Validate names and every ancestor before opening any module file."""
    require(isinstance(relative, str) and relative, 'invalid module path')
    path = Path(relative)
    require(not path.is_absolute() and path.as_posix() == relative
            and all(x not in ('', '.', '..') for x in relative.split('/')),
            'non-canonical module path')
    require(not common.is_secret_like(relative), 'protected module path refused')
    result = MODULE / path
    for item in (result, *result.parents):
        require(not item.is_symlink(), 'symlink in module path refused')
        if item == MODULE:
            break
    require(result.resolve().is_relative_to(MODULE), 'module path escaped')
    return result


def read(relative):
    path = safe_path(relative)
    require(path.is_file(), 'missing module file: ' + relative)
    return path.read_bytes()


def read_json(relative):
    return json.loads(read(relative))


def approved_export():
    require((ROOT / 'EXPORT-MANIFEST.txt').is_file(), 'run only in an approved isolated export')
    require(not any((p / '.git').exists() for p in (ROOT, *ROOT.parents)),
            'refusing execution in a Git checkout')
    common.assert_no_env_in_ancestors(MODULE)
    export = json.loads((ROOT / 'EXPORT-MANIFEST.txt').read_text())
    require(export.get('schema') == 'faroo-native-export-v1', 'unknown export schema')
    require('slp/build-reference.json' in export.get('files', {}), 'SLP module missing from export')


def compiler_identity(solc, reference):
    require(not common.is_secret_like(solc), 'protected compiler path refused')
    path = Path(solc)
    require(path.is_absolute() and path.is_file() and not path.is_symlink(),
            'compiler must be an installed absolute regular file')
    require(sha(path.read_bytes()) == reference['compilerSha256'], 'unapproved compiler bytes')
    version = subprocess.check_output([str(path), '--version'], cwd=MODULE, env={}, text=True)
    require(version == reference['compiler'], 'compiler version mismatch')
    return str(path)


def configuration(reference):
    require(reference.get('schema') == 'faroo-slp-build-v1', 'unknown build reference')
    require(set(reference['contracts']) == set(NAMES), 'unexpected target set')
    for path, field in [('DEPENDENCIES.json', 'dependenciesSha256'),
                        ('deployments/PROVENANCE.json', 'provenanceSha256'),
                        ('foundry.toml', 'testConfigSha256')]:
        require(sha(read(path)) == reference[field], 'configuration drift: ' + path)
    deps = read_json('DEPENDENCIES.json')
    require(deps.get('schema') == 'faroo-slp-dependencies-v1', 'unknown dependency manifest')
    expected = set()
    for virtual, entry in deps['sources'].items():
        path = entry['path']
        require(virtual.endswith('.sol') and path.endswith('.sol')
                and path.startswith(('contracts/', 'lib/')), 'invalid source mapping')
        require(path not in expected, 'duplicate source path')
        expected.add(path)
        require(sha(read(path)) == entry['sha256'], 'source drift: ' + path)
    actual = {p.relative_to(MODULE).as_posix() for folder in ('contracts', 'lib')
              for p in safe_path(folder).rglob('*.sol')}
    require(actual == expected, 'SLP production source inventory drift')
    provenance = read_json('deployments/PROVENANCE.json')
    require(provenance.get('schema') == 'faroo-slp-deployments-v1'
            and provenance['chainId'] == 1672, 'unexpected deployment provenance')
    for path, entry in provenance['records'].items():
        require(path.startswith('deployments/pharos_pacific_mainnet/')
                and (path.endswith('.json') or path.endswith('/.chainId')),
                'deployment path outside allowlist')
        require(sha(read(path)) == entry['sha256'], 'deployment record drift: ' + path)
    require(read('deployments/pharos_pacific_mainnet/.chainId').strip() == b'1672',
            'deployment chain mismatch')
    return deps, provenance


def standard_input(target, deps):
    original = read_json(target['inputRecord'])
    require(sorted(original['sources']) == target['sourceNames'], 'historical source set mismatch')
    require(original['settings'] == target['settings'], 'historical settings mismatch')
    std = {'language': 'Solidity', 'settings': target['settings'], 'sources': {}}
    for virtual in target['sourceNames']:
        content = read(deps['sources'][virtual]['path']).decode('utf-8')
        std['sources'][virtual] = {'content': content}
    require(std == original, 'module disk input differs from historical standard JSON')
    require(sha(canonical(std)) == target['inputCanonicalSha256'], 'compiler input drift')
    return std


def compile_input(std, solc):
    proc = subprocess.run([solc, '--standard-json', '--no-import-callback'],
                          input=canonical(std), capture_output=True, cwd=MODULE, env={}, check=True)
    output = json.loads(proc.stdout)
    errors = [e for e in output.get('errors', []) if e['severity'] == 'error']
    require(not errors, 'solc error: ' + str(errors))
    return output


def namespace_specs(std, output, name, source):
    index = {n['id']: (key, n) for key, entry in output['sources'].items()
             for n in entry['ast'].get('nodes', []) if n.get('nodeType') == 'ContractDefinition'}
    contract = next(n for key, n in index.values() if key == source and n['name'] == name)
    result = []
    for ident in contract['linearizedBaseContracts']:
        key, base = index[ident]
        for node in base.get('nodes', []):
            if node.get('nodeType') != 'StructDefinition':
                continue
            annotation = (node.get('documentation') or {}).get('text', '')
            match = re.search(r'@custom:storage-location\s+erc7201:([\w.\-]+)', annotation)
            if match:
                namespace = match.group(1)
            elif base['name'] == name and node['name'] == name + 'Storage':
                namespace = 'bifrost.storage.' + name
            else:
                continue
            root = erc7201_slot(namespace)
            constants = [n['name'] for n in base['nodes'] if n.get('constant')
                         and n.get('value', {}).get('value', '').lower() == root]
            require(constants, 'namespace root constant mismatch: ' + namespace)
            result.append({'namespace': namespace, 'root': root, 'source': key,
                           'contract': base['name'], 'struct': node['name'],
                           'declaredConstants': sorted(constants)})
    require(len(result) == 5, 'expected own and four inherited storage namespaces')
    return sorted(result, key=lambda x: x['namespace'])


def describe_type(types, ident):
    """Remove compiler AST IDs, preserve every nested storage offset and type."""
    value = types[ident]
    result = {k: value[k] for k in ('label', 'encoding', 'numberOfBytes')}
    for key in ('key', 'value', 'base'):
        if key in value:
            result[key] = describe_type(types, value[key])
    if 'members' in value:
        result['members'] = [{**{k: m[k] for k in ('label', 'slot', 'offset')},
                              'type': describe_type(types, m['type'])} for m in value['members']]
    return result


def namespace_layouts(std, output, name, source, solc):
    specs = namespace_specs(std, output, name, source)
    key = 'SLPStorageProbe.sol'
    text = 'pragma solidity 0.8.28;\n' + ''.join(
        'import "' + path + '";\n' for path in sorted({s['source'] for s in specs}))
    text += 'contract SLPStorageProbe {\n' + ''.join(
        f"{s['contract']}.{s['struct']} private probe{i};\n" for i, s in enumerate(specs)) + '}\n'
    probe = {'language': 'Solidity', 'sources': {**std['sources'], key: {'content': text}},
             'settings': {**std['settings'], 'outputSelection': {key: {'SLPStorageProbe': ['storageLayout']}}}}
    compiled = compile_input(probe, solc)
    layout = compiled['contracts'][key]['SLPStorageProbe']['storageLayout']
    rows = {x['label']: x for x in layout['storage']}
    result = [{**s, 'layout': describe_type(layout['types'], rows[f'probe{i}']['type'])}
              for i, s in enumerate(specs)]
    return result, probe, compiled


def validate_deployments(provenance, artifacts):
    require(set(provenance['instances']) == {'SlpPharosProxy', 'SlpPharosProxy_1',
                                           'SlpPharosRewardsVault'}, 'instance inventory mismatch')
    proxies, implementations = set(), set()
    for name, row in provenance['instances'].items():
        proxy = read_json(row['proxyRecord'])
        implementation = read_json(row['implementationRecord'])
        aggregate = read_json(row['aggregateRecord'])
        require(proxy['address'].lower() == aggregate['address'].lower() == row['proxy'],
                'proxy address mismatch: ' + name)
        require(implementation['address'].lower() == aggregate['implementation'].lower()
                == row['implementation'], 'implementation address mismatch: ' + name)
        require(proxy['args'][0].lower() == row['implementation']
                and proxy['args'][1].lower() == row['proxyAdmin'], 'proxy constructor identity mismatch')
        require(keccak256(bytes.fromhex(proxy['deployedBytecode'][2:])) == row['proxyRuntimeKeccak'],
                'proxy runtime mismatch')
        artifact = artifacts[row['contract']]
        require(artifact['runtime'] == implementation['deployedBytecode'], 'implementation bytes mismatch')
        require(implementation['abi'] == artifact['abi'], 'implementation ABI mismatch')
        # Hardhat-deploy aggregates include proxy ABI and replace the constructor.
        # The historical SLP aggregate also drops the no-argument overloaded
        # InsufficientBalance error. Preserve that original file (hash-locked),
        # but always expose the complete implementation ABI as canonical.
        require(all(item in proxy['abi'] or item in implementation['abi'] for item in aggregate['abi']),
                'aggregate contains an unknown ABI entry')
        require(all(item in aggregate['abi'] for item in implementation['abi']
                    if item['type'] in ('function', 'event')), 'aggregate lost a function/event')
        require(artifact['runtimeKeccak'] == row['implementationRuntimeKeccak'], 'implementation hash mismatch')
        proxies.add(row['proxy'])
        implementations.add(row['implementation'])
    require(len(proxies) == 3 and len(implementations) == 3 and not proxies & implementations,
            'proxy/implementation identities merged')
    admin = provenance['proxyAdmin']
    rec = read_json(admin['record'])
    require(rec['address'].lower() == admin['address']
            and keccak256(bytes.fromhex(rec['deployedBytecode'][2:])) == admin['runtimeKeccak'],
            'ProxyAdmin identity mismatch')
    require(all(x['proxyAdmin'] == admin['address'] for x in provenance['instances'].values()),
            'unexpected separate ProxyAdmin')


def build(solc):
    approved_export()
    # Only a manifest declares a completed build. Never leave an old success marker after failure.
    dest = safe_path('out-native')
    safe_path('out-native/manifest.json').unlink(missing_ok=True)
    reference = read_json('build-reference.json')
    deps, provenance = configuration(reference)
    solc = compiler_identity(solc, reference)
    jobs, artifacts = {}, {}
    for name in NAMES:
        target = reference['contracts'][name]
        std = standard_input(target, deps)
        output = compile_input(std, solc)
        contract = output['contracts'][target['source']][name]
        require(sha(canonical(contract)) == target['targetOutputSha256'], 'complete target output drift: ' + name)
        require(contract['evm']['methodIdentifiers'] == target['selectors'], 'selector drift')
        require(contract['storageLayout'] == target['storageLayout'], 'linear storage drift')
        layouts, probe, probe_output = namespace_layouts(std, output, name, target['source'], solc)
        require(layouts == target['namespaces'], 'namespace/storage layout drift: ' + name)
        runtime = '0x' + contract['evm']['deployedBytecode']['object']
        require(keccak256(bytes.fromhex(runtime[2:])) == target['runtimeKeccak'], 'runtime Keccak mismatch')
        for path in target['implementationRecords']:
            recorded = read_json(path)
            require(runtime == recorded['deployedBytecode'] and contract['metadata'] == recorded['metadata']
                    and contract['abi'] == recorded['abi'], 'full deployment comparison failed: ' + path)
            require('0x' + contract['evm']['bytecode']['object'] == recorded['bytecode'],
                    'creation bytecode mismatch: ' + path)
        artifacts[name] = {'source': target['source'], 'abi': contract['abi'], 'metadata': contract['metadata'],
                           'selectors': contract['evm']['methodIdentifiers'], 'storageLayout': contract['storageLayout'],
                           'namespaces': layouts, 'runtime': runtime, 'runtimeKeccak': target['runtimeKeccak'],
                           'creationBytecode': '0x' + contract['evm']['bytecode']['object']}
        jobs[name] = {'input.json': std, 'output.json': output, 'artifact.json': artifacts[name],
                      'storage-probe-input.json': probe, 'storage-probe-output.json': probe_output,
                      'build-info.json': {'solcVersion': '0.8.28', 'input': std, 'output': output}}
    validate_deployments(provenance, artifacts)
    manifest = {'schema': 'faroo-slp-build-result-v1', 'status': 'PASS',
                'scope': 'Offline deployed-artifact reproduction, not current chain/audit/upgrade approval',
                'referenceSha256': sha(read('build-reference.json')), 'compilerSha256': reference['compilerSha256'],
                'dependenciesSha256': reference['dependenciesSha256'], 'provenanceSha256': reference['provenanceSha256'],
                'contracts': {}, 'instances': provenance['instances']}
    # Validate every output path before publishing any files; no path options or symlink writes.
    for name, files in jobs.items():
        for filename in files:
            safe_path(f'out-native/{name}/{filename}')
    for name, files in jobs.items():
        manifest['contracts'][name] = {'runtimeKeccak': artifacts[name]['runtimeKeccak'], 'files': {}}
        for filename, value in files.items():
            path = safe_path(f'out-native/{name}/{filename}')
            path.parent.mkdir(parents=True, exist_ok=True)
            data = canonical(value)
            path.write_bytes(data)
            manifest['contracts'][name]['files'][filename] = sha(data)
        print(name + ': exact full runtime, metadata, ABI, selectors and five namespaces')
    dest.mkdir(exist_ok=True)
    safe_path('out-native/manifest.json').write_bytes(canonical(manifest))
    print('PASS: two deployed contract types, three distinct proxy/implementation identities')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--solc', required=True, help='Absolute installed compiler path; exact bytes checked before execution')
    args = parser.parse_args()
    try:
        build(args.solc)
    except (ValueError, OSError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        parser.exit(1, 'SLP build refused: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
