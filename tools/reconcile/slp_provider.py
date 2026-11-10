"""Frozen origin-manifest adapter; no Git, compiler, RPC or external checkout reads.

This authenticates recorded builds, not current-chain identity. The historical
closeout separately binds records to its exact cached chain snapshot/history.
"""
import json
from pathlib import Path
from tools.lib import abi
from tools.lib.keccak import keccak256, selector
from .storage import EvidenceError, digest, safe

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODULE = ROOT / 'slp'
# Reviewed Batch 1 references. Updating these is a design/evidence change.
REFERENCE_SHA256 = 'b701b84fe13e380156beb56622e2829c90a93f27dd7eadfed5e30dbcc19e8978'


class LocalProvider:
    def __init__(self, module=DEFAULT_MODULE):
        self.root = safe(module)
        raw = self.read('build-reference.json')
        if digest(raw) != REFERENCE_SHA256:
            raise EvidenceError('SLP build reference drift')
        self.reference = json.loads(raw)
        for path, field in [('DEPENDENCIES.json', 'dependenciesSha256'),
                            ('deployments/PROVENANCE.json', 'provenanceSha256')]:
            if digest(self.read(path)) != self.reference[field]:
                raise EvidenceError('SLP manifest drift: ' + path)
        self.deps = json.loads(self.read('DEPENDENCIES.json'))
        self.provenance = json.loads(self.read('deployments/PROVENANCE.json'))
        for path, entry in self.provenance['records'].items():
            if digest(self.read(path)) != entry['sha256']:
                raise EvidenceError('SLP deployment record drift: ' + path)
        for entry in self.deps['sources'].values():
            if digest(self.read(entry['path'])) != entry['sha256']:
                raise EvidenceError('SLP source drift: ' + entry['path'])
        self.instances = {}
        from .attestation import SNAPSHOT, runtime_binding
        from tools.release.provenance import evm
        if self.provenance['chainId'] != 1672 or self.provenance['attestedAt']['historical'] != SNAPSHOT:
            raise EvidenceError('SLP historical chain/snapshot mismatch')
        for name, instance in self.provenance['instances'].items():
            record = json.loads(self.read(instance['implementationRecord']))
            proxy = json.loads(self.read(instance['proxyRecord']))
            code = record['deployedBytecode']
            binding = runtime_binding(record, {'snapshot': SNAPSHOT, 'runtimeCode': code,
                                               'identity': evm.code_identity(code)})
            target = self.reference['contracts'][instance['contract']]
            if (binding['runtimeKeccak'] != target['runtimeKeccak']
                    or binding['runtimeKeccak'] != instance['implementationRuntimeKeccak']
                    or record['address'].lower() != instance['implementation']
                    or proxy['address'].lower() != instance['proxy']
                    or proxy['args'][0].lower() != instance['implementation']
                    or proxy['args'][1].lower() != instance['proxyAdmin']
                    or keccak256(bytes.fromhex(proxy['deployedBytecode'][2:])) != instance['proxyRuntimeKeccak']):
                raise EvidenceError('SLP instance binding mismatch')
            meta = json.loads(record['metadata'])
            std = json.loads(self.read(target['inputRecord']))
            for virtual, source in meta['sources'].items():
                if (self.read(self.deps['sources'][virtual]['path']).decode() != source['content']
                        or std['sources'][virtual]['content'] != source['content']):
                    raise EvidenceError('SLP compiler/source closure mismatch')
            selectors = {abi.item_signature(i): selector(abi.item_signature(i))[2:] for i in record['abi'] if i['type'] == 'function'}
            if selectors != target['selectors']:
                raise EvidenceError('SLP selector mismatch')
            self.instances[instance['proxy']] = dict(instance, name=name, record=record, proxyRecordData=proxy)

    def read(self, relative):
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts:
            raise EvidenceError('invalid SLP relative path')
        path = safe(self.root / path)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise EvidenceError('SLP symlink refused')
        try:
            return path.read_bytes()
        except OSError as exc:
            raise EvidenceError('missing SLP evidence: ' + relative) from exc

    def origin_bytes(self, revision, path):
        entry = self.provenance['records'].get(path)
        if entry and revision == entry['originCommit'] and path == entry['originPath']:
            return self.read(path)
        entry = self.deps['sources'].get(path)
        if entry and path == entry['origin']['path'] and revision in (
                entry['origin']['commit'], entry['origin'].get('sourceChangeCommit')):
            return self.read(entry['path'])
        raise EvidenceError('unsupported SLP historical source version')
