"""Offline build-boundary regressions. Run only inside the approved export.

These are not the 76 Solidity behavior tests, which migrate in Batch 2.
Mutations are made only to the disposable export, then restored exactly.
"""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('slp_native_build', Path(__file__).resolve().parents[1] / 'tools/build.py')
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)


@contextmanager
def changed(path, data):
    original = path.read_bytes()
    try:
        path.write_bytes(data)
        yield
    finally:
        path.write_bytes(original)


class BuildBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        b.approved_export()
        cls.reference = b.read_json('build-reference.json')
        # Explicit installed candidate paths only, never a download or PATH search.
        candidates = [Path.home() / 'Library/Application Support/svm/0.8.28/solc-0.8.28',
                      Path.home() / '.svm/0.8.28/solc-0.8.28']
        cls.solc = str(next(p for p in candidates if p.is_file()
                           and b.sha(p.read_bytes()) == cls.reference['compilerSha256']))
        cls.good = b.build(cls.solc)

    def refused(self, pattern):
        marker = b.safe_path('out-native/manifest.json')
        marker.write_text('{"status":"PASS","stale":true}')
        with self.assertRaisesRegex(ValueError, pattern):
            b.build(self.solc)
        self.assertFalse(marker.exists(), 'failure left a stale success manifest')

    def test_full_build_is_byte_deterministic(self):
        self.assertEqual(b.build(self.solc), self.good)

    def test_two_slp_instances_keep_distinct_addresses_and_same_runtime(self):
        rows = self.good['instances']
        self.assertNotEqual(rows['SlpPharosProxy']['proxy'], rows['SlpPharosProxy_1']['proxy'])
        self.assertNotEqual(rows['SlpPharosProxy']['implementation'], rows['SlpPharosProxy_1']['implementation'])
        self.assertEqual(rows['SlpPharosProxy']['implementationRuntimeKeccak'],
                         rows['SlpPharosProxy_1']['implementationRuntimeKeccak'])

    def test_canonical_abi_preserves_overloaded_error_missing_from_aggregate(self):
        artifact = b.read_json('out-native/SlpPharosProxy/artifact.json')
        errors = [x for x in artifact['abi'] if x.get('name') == 'InsufficientBalance']
        self.assertEqual(sorted(len(x['inputs']) for x in errors), [0, 2])
        aggregate = b.read_json('deployments/pharos_pacific_mainnet/SlpPharosProxy.json')
        self.assertNotEqual(artifact['abi'], aggregate['abi'])

    def test_modified_source_rejected(self):
        p = b.safe_path('contracts/pharos/SlpPharosProxy.sol')
        with changed(p, p.read_bytes() + b'\n// altered source\n'):
            self.refused('source drift')

    def test_modified_dependency_rejected(self):
        p = b.safe_path('lib/openzeppelin-contracts/utils/Address.sol')
        with changed(p, p.read_bytes() + b'\n// altered dependency\n'):
            self.refused('source drift')

    def test_missing_source_has_no_historical_input_fallback(self):
        p = b.safe_path('contracts/interfaces/pharos/IStaking.sol')
        original = p.read_bytes()
        try:
            p.unlink()
            self.refused('missing module file')
        finally:
            p.write_bytes(original)

    def test_modified_historical_compiler_input_rejected(self):
        ref = self.reference['contracts']['SlpPharosProxy']
        p = b.safe_path(ref['inputRecord'])
        value = json.loads(p.read_bytes())
        value['settings']['evmVersion'] = 'cancun'
        with changed(p, b.canonical(value)):
            self.refused('deployment record drift')

    def test_extra_solidity_source_rejected(self):
        p = b.safe_path('contracts/Unexpected.sol')
        self.assertFalse(p.exists())
        try:
            p.write_text('pragma solidity 0.8.28; contract Unexpected {}')
            self.refused('source inventory drift')
        finally:
            p.unlink()

    def test_wrong_compiler_not_executed(self):
        with tempfile.TemporaryDirectory(dir=b.MODULE) as directory:
            p = Path(directory) / 'unapproved-solc'
            sentinel = Path(directory) / 'EXECUTED'
            p.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\n')
            p.chmod(0o700)
            with self.assertRaisesRegex(ValueError, 'unapproved compiler bytes'):
                b.build(str(p))
            self.assertFalse(sentinel.exists())

    def test_modified_dependency_manifest_rejected(self):
        p = b.safe_path('DEPENDENCIES.json')
        with changed(p, p.read_bytes() + b' '):
            self.refused('configuration drift')

    def test_test_config_drift_rejected(self):
        p = b.safe_path('foundry.toml')
        with changed(p, p.read_bytes() + b'\n# changed\n'):
            self.refused('configuration drift')

    def test_deployment_address_tamper_rejected(self):
        p = b.safe_path('deployments/pharos_pacific_mainnet/SlpPharosProxy_1_Proxy.json')
        value = json.loads(p.read_bytes())
        value['address'] = self.good['instances']['SlpPharosProxy']['proxy']
        with changed(p, b.canonical(value)):
            self.refused('deployment record drift')

    def test_source_symlink_rejected_before_read(self):
        p = b.safe_path('contracts/interfaces/pharos/IOracle.sol')
        original = p.read_bytes()
        try:
            p.unlink()
            p.symlink_to('IStaking.sol')
            self.refused('symlink')
        finally:
            p.unlink()
            p.write_bytes(original)

    def test_noncanonical_path_rejected(self):
        with self.assertRaisesRegex(ValueError, 'non-canonical'):
            b.safe_path('../outside.sol')

    def test_frozen_selector_change_rejected(self):
        p = b.safe_path('build-reference.json')
        value = json.loads(p.read_bytes())
        value['contracts']['SlpPharosProxy']['selectors']['transferToRewardsVault(uint256)'] = '00000000'
        with changed(p, b.canonical(value)):
            self.refused('selector drift')

    def test_frozen_namespace_offset_change_rejected(self):
        p = b.safe_path('build-reference.json')
        value = json.loads(p.read_bytes())
        ns = value['contracts']['SlpPharosProxy']['namespaces'][0]
        self.assertEqual(ns['namespace'], 'bifrost.storage.SlpPharosProxy')
        ns['layout']['members'][-1]['slot'] = '6'
        with changed(p, b.canonical(value)):
            self.refused('namespace/storage layout drift')


if __name__ == '__main__':
    unittest.main(verbosity=2)
