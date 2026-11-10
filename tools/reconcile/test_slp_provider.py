"""Local recorded-build identity and fixed-history rejection tests (no RPC)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools.lib import abi
from tools.lib.keccak import keccak256
from tools.release.provenance import evm
from .attestation import authenticate, SNAPSHOT
from .slp_provider import LocalProvider, DEFAULT_MODULE
from .storage import EvidenceError


class LocalProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = LocalProvider()

    def fixture(self):
        data = {'cache/snapshots/external-implementations.json': {},
                'cache/snapshots/external-state.json': {'snapshot': SNAPSHOT.copy(), 'proxySlots': {}},
                'cache/snapshots/headers.json': {}}
        logs = []
        for address, i in self.provider.instances.items():
            r, p = i['record'], i['proxyRecordData']
            code = r['deployedBytecode']
            data['cache/snapshots/external-implementations.json'][i['implementation']] = {
                'snapshot': SNAPSHOT.copy(), 'runtimeCode': code, 'identity': evm.code_identity(code)}
            data['cache/snapshots/external-state.json']['proxySlots'][address] = {
                'implementation': i['implementation'], 'admin': i['proxyAdmin'], 'runtimeKeccak': i['proxyRuntimeKeccak']}
            tx = p['transactionHash'].lower(); receipt = copy.deepcopy(p['receipt'])
            data['cache/receipts/'+tx+'.json'] = receipt
            constructor = next(x for x in p['abi'] if x['type']=='constructor')
            data['cache/receipts/'+tx+'-transaction.json'] = {'input': p['bytecode'] + abi.encode_parameters(constructor['inputs'],p['args'])[2:]}
            data['cache/snapshots/headers.json'][receipt['blockNumber']] = {'hash': receipt['blockHash']}
            logs.append({'address': address, 'topics': [abi.event_topic('Upgraded(address)'), '0x'+i['implementation'][2:].rjust(64,'0')],
                         'data':'0x', 'transactionHash':tx, 'blockNumber':receipt['blockNumber'], 'logIndex':0})
        class Store:
            def get(self,key,default=None): return data.get(key,default)
        return Store(), data, logs

    def test_all_three_identities_without_git_or_external_checkout(self):
        store, _, logs = self.fixture()
        with patch('subprocess.check_output', side_effect=AssertionError('Git/external checkout forbidden')):
            result = authenticate(DEFAULT_MODULE, store, logs)
        self.assertEqual(set(result['proxies']),set(self.provider.instances))
        self.assertIsNone(result['repositoryHead'])
        self.assertEqual(result['provider'],'local-origin-manifest')
        for item in result['implementations'].values(): self.assertEqual(item['status'],'PASS')

    def test_both_overloaded_errors_and_reward_selectors(self):
        for i in self.provider.instances.values():
            if i['contract'] != 'SlpPharosProxy': continue
            signatures = {abi.item_signature(x) for x in i['record']['abi'] if x['type'] in ('error','function')}
            self.assertTrue({'InsufficientBalance()','InsufficientBalance(uint256,uint256)',
                             'claimReward(bytes32)','transferToRewardsVault(uint256)'} <= signatures)

    def tamper(self, relative, mutate=None, remove=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)/'slp'
            paths = ['build-reference.json','DEPENDENCIES.json','deployments/PROVENANCE.json']
            paths += list(self.provider.provenance['records'])
            paths += [v['path'] for v in self.provider.deps['sources'].values()]
            for name in paths:
                target=root/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(self.provider.read(name))
            target=root/relative
            if remove: target.unlink()
            else: target.write_bytes(mutate(target.read_bytes()) if mutate else target.read_bytes()+b' ')
            with self.assertRaises(EvidenceError): LocalProvider(root)

    def test_missing_manifest(self): self.tamper('deployments/PROVENANCE.json',remove=True)
    def test_tampered_manifest(self): self.tamper('deployments/PROVENANCE.json')
    def test_modified_source(self): self.tamper('contracts/pharos/SlpPharosProxy.sol')
    def test_reference_drift(self): self.tamper('build-reference.json')
    def test_deployment_abi_substitution(self):
        self.tamper('deployments/pharos_pacific_mainnet/SlpPharosProxy_Implementation.json',lambda b: b.replace(b'claimReward',b'wrongReward'))
    def test_metadata_drift(self): self.tamper('deployments/pharos_pacific_mainnet/SlpPharosRewardsVault_Implementation.json')
    def test_wrong_chain(self): self.tamper('deployments/pharos_pacific_mainnet/.chainId',lambda b:b'688689')
    def test_proxy_record_drift(self): self.tamper('deployments/pharos_pacific_mainnet/SlpPharosProxy_Proxy.json')
    def test_unsupported_historical_version(self):
        with self.assertRaises(EvidenceError): self.provider.origin_bytes('unknown','contracts/pharos/SlpPharosProxy.sol')

    def test_snapshot_number_and_hash_rejected(self):
        for k,v in [('number',22633551),('hash','0x'+'11'*32)]:
            store,data,logs=self.fixture();data['cache/snapshots/external-state.json']['snapshot'][k]=v
            with self.assertRaises(EvidenceError): authenticate(DEFAULT_MODULE,store,logs)

    def test_wrong_slot_unknown_runtime_and_missing_receipt(self):
        for case in ['slot','runtime','receipt','upgrade']:
            store,data,logs=self.fixture();i=next(iter(self.provider.instances.values()))
            if case=='slot': data['cache/snapshots/external-state.json']['proxySlots'][i['proxy']]['implementation']='0x'+'11'*20
            elif case=='runtime': data['cache/snapshots/external-implementations.json'][i['implementation']]['runtimeCode']='0x6000'
            elif case=='receipt': del data['cache/receipts/'+i['proxyRecordData']['transactionHash'].lower()+'.json']
            else: logs.append(copy.deepcopy(logs[0]))
            with self.subTest(case=case), self.assertRaises(EvidenceError): authenticate(DEFAULT_MODULE,store,logs)

    def test_removed_root_files_are_not_needed(self):
        self.assertFalse((DEFAULT_MODULE.parent/'contracts/SlpPharosProxy.sol').exists())
        from .inventory import discover
        inv=discover()
        for addr in self.provider.instances:
            self.assertEqual(inv['targets'][addr]['abiStatus'],'RECORDED_LOCAL_ORIGIN_MANIFEST')

    def test_local_record_is_not_current_runtime_authentication(self):
        from .external import bound_inventory
        inv = {'targets': {a: {'name': i['name'], 'abi': i['record']['abi'],
                              'abiStatus': 'RECORDED_LOCAL_ORIGIN_MANIFEST'}
                           for a,i in self.provider.instances.items()}}
        result = bound_inventory(inv, None, {}, {'number':22633551,'hash':'0x'+'11'*32})
        for row in result['targets'].values():
            self.assertEqual(row['abiStatus'],'UNVERIFIED_IMPLEMENTATION')
            self.assertFalse(any(x['type']=='function' for x in row['abi']))

    def test_original_testnet_runtime_and_abi_cannot_be_mainnet_identity(self):
        from .storage import digest
        raw = (Path(__file__).parent/'fixtures/slp-testnet-negative.json').read_bytes()
        self.assertEqual(digest(raw),'e0c3c0acabcfc806f57cba21b0262db034b073fd878ef0fe8211f05244345cf1')
        testnet=json.loads(raw)
        self.assertEqual(testnet['chainId'],688689)
        for name, record in testnet['records'].items():
            store,data,logs=self.fixture()
            i=next(i for i in self.provider.instances.values() if i['contract']==name)
            cached=data['cache/snapshots/external-implementations.json'][i['implementation']]
            cached['runtimeCode']=record['runtime'];cached['identity']=evm.code_identity(record['runtime'])
            with self.assertRaisesRegex(EvidenceError,'full runtime mismatch'):
                authenticate(DEFAULT_MODULE,store,logs)
            self.tamper(i['implementationRecord'],lambda b: json.dumps(dict(json.loads(b),abi=record['abi'])).encode())

    def test_wrong_reward_selector_and_topic_are_not_interpreted(self):
        from .inventory import decode
        from tools.lib.keccak import selector
        i=next(i for i in self.provider.instances.values() if i['contract']=='SlpPharosProxy')
        items=i['record']['abi']
        with self.assertRaises(ValueError):
            abi.decode_function(items,selector('claimRewards(bytes32)')+'00'*32)
        self.assertIsNone(decode({'topics':[abi.event_topic('RewardClaimed(bytes32,address,uint128)')],
                                 'data':'0x'+'00'*96},items))
