"""Exact original mock suites and isolated test-only dependency closure."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1]
ORIGINALS = {
    'tests/SlpPharosProxy.t.sol': ('2bf6ecc2df5a6ccb4809895ebdacd16083ad62303ea96271bbfb65687c7f13dd', 59),
    'tests/SlpPharosRewardsVault.t.sol': ('daa4154598a107a9f1a222bd1d96c2bf2f817efa863f4848c326f1255f5f46ff', 17),
}


def verify(module):
    manifest = json.loads((module / 'tests/DEPENDENCIES.json').read_text())
    if set(manifest['tests']) != set(ORIGINALS):
        raise ValueError('original suite set changed')
    for path, (sha, count) in ORIGINALS.items():
        raw = (module / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha or len(re.findall(rb'function\s+test\w*\s*\(', raw)) != count:
            raise ValueError('original test identity/count changed')
        if manifest['tests'][path]['sha256'] != sha or manifest['tests'][path]['testCount'] != count:
            raise ValueError('original test manifest changed')
    for path, entry in manifest['files'].items():
        rel = Path(path)
        if not path.startswith('test-lib/') or rel.suffix != '.sol' or '..' in rel.parts:
            raise ValueError('test dependency path outside closure')
        p = module / rel
        if any(part.is_symlink() for part in (p, *p.parents)):
            raise ValueError('symlink test dependency')
        if hashlib.sha256(p.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('test dependency checksum mismatch')
    actual = {str(p.relative_to(module)) for p in (module / 'test-lib').rglob('*.sol')}
    if actual != set(manifest['files']):
        raise ValueError('unexpected test dependency')
    prod = json.loads((module / 'DEPENDENCIES.json').read_text())
    if sum(p.startswith('@openzeppelin/') for p in prod['sources']) != 19 or any(s['path'].startswith('test-lib/') for s in prod['sources'].values()):
        raise ValueError('test dependencies contaminate production closure')


class MigrationTests(unittest.TestCase):
    def test_exact_original_suites_and_dependency_closure(self):
        verify(MODULE)

    def tamper(self, change):
        with tempfile.TemporaryDirectory(prefix='slp-migration-') as tmp:
            root = Path(tmp)
            for folder in ('tests', 'test-lib'):
                shutil.copytree(MODULE / folder, root / folder, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(MODULE / 'DEPENDENCIES.json', root / 'DEPENDENCIES.json')
            change(root)
            with self.assertRaises((ValueError, FileNotFoundError)):
                verify(root)

    def test_changed_assertion_rejected(self):
        self.tamper(lambda r: (r / 'tests/SlpPharosProxy.t.sol').write_text(''))

    def test_missing_original_suite_rejected(self):
        self.tamper(lambda r: (r / 'tests/SlpPharosRewardsVault.t.sol').unlink())

    def test_altered_dependency_rejected(self):
        self.tamper(lambda r: (r / 'test-lib/forge-std/src/Test.sol').write_text(''))

    def test_extra_dependency_rejected(self):
        self.tamper(lambda r: (r / 'test-lib/unreviewed.sol').write_text(''))

    def test_dependency_traversal_rejected(self):
        def mutate(r):
            p = r / 'tests/DEPENDENCIES.json'
            data = json.loads(p.read_text())
            data['files']['test-lib/../../outside.sol'] = {'sha256': ''}
            p.write_text(json.dumps(data))
        self.tamper(mutate)

    def test_test_dependency_in_production_rejected(self):
        def mutate(r):
            p = r / 'DEPENDENCIES.json'
            data = json.loads(p.read_text())
            next(iter(data['sources'].values()))['path'] = 'test-lib/forge-std/src/Test.sol'
            p.write_text(json.dumps(data))
        self.tamper(mutate)


if __name__ == '__main__':
    unittest.main()
