"""Targeted regressions for source authentication and reward-cash closeout only."""
import copy
import json
import unittest
import tempfile
import contextlib
import io
from unittest.mock import patch
from tools.lib.keccak import keccak256
from .attestation import SNAPSHOT, runtime_binding, solc_ipfs_multihash
from .closeout import consume_credit, reward_ledger
from .storage import EvidenceError


def artifact_fixture():
    abi = [{'type':'function','name':'transferToRewardsVault','inputs':[{'name':'amount','type':'uint256'}],
            'outputs':[],'stateMutability':'nonpayable'}]
    content = 'pragma solidity 0.8.28; contract S {}'
    metadata = json.dumps({'compiler':{'version':'0.8.28'}, 'settings':{'libraries':{}},
        'output':{'abi':abi}, 'sources':{'contracts/S.sol':{'content':content,'keccak256':keccak256(content.encode())}}},sort_keys=True)
    ipfs = solc_ipfs_multihash(metadata)
    cbor = bytes.fromhex('a264697066735822'+ipfs+'64736f6c634300081c')
    code = '0x'+(b'\x60\x00'+cbor+len(cbor).to_bytes(2,'big')).hex()
    record = {'metadata':metadata,'abi':abi,'deployedBytecode':code}
    cached = {'runtimeCode':code,'snapshot':SNAPSHOT.copy(), 'identity':{'keccak256':keccak256(bytes.fromhex(code[2:])),
              'cbor_metadata':{'ipfs':ipfs,'solc':'00081c'}}}
    return record,cached


class ArtifactAttestationTests(unittest.TestCase):
    def test_exact_runtime_metadata_and_abi_binding(self):
        record,cached=artifact_fixture()
        result=runtime_binding(record,cached)
        self.assertIn('FULL_BYTES',result['comparison'])
        self.assertEqual(result['runtimeKeccak'],cached['identity']['keccak256'])

    def test_runtime_change_is_not_hidden_by_metadata_removal(self):
        record,cached=artifact_fixture();record['deployedBytecode']='0x61'+record['deployedBytecode'][4:]
        with self.assertRaisesRegex(EvidenceError,'runtime mismatch'):runtime_binding(record,cached)

    def test_metadata_tampering_rejected(self):
        record,cached=artifact_fixture();record['metadata']=record['metadata'].replace('contract S','contract T')
        with self.assertRaisesRegex(EvidenceError,'metadata is not bound'):runtime_binding(record,cached)

    def test_cached_cbor_summary_cannot_replace_actual_bytecode_metadata(self):
        record,cached=artifact_fixture();cached['identity']['cbor_metadata']['ipfs']='1220'+'11'*32
        with self.assertRaisesRegex(EvidenceError,'summary differs'):runtime_binding(record,cached)

    def test_selector_match_without_abi_match_is_insufficient(self):
        record,cached=artifact_fixture();record['abi']=[]
        with self.assertRaisesRegex(EvidenceError,'ABI differs'):runtime_binding(record,cached)

    def test_snapshot_mismatch_is_blocked(self):
        record,cached=artifact_fixture();cached['snapshot']['number']+=1
        with self.assertRaisesRegex(EvidenceError,'different snapshot'):runtime_binding(record,cached)

    def test_large_metadata_requires_real_multichunk_verification(self):
        with self.assertRaises(EvidenceError):solc_ipfs_multihash('a'*262145)


class RewardCreditTests(unittest.TestCase):
    def test_credit_is_consumed_once_across_transactions(self):
        lots=[{'transaction':'claim1','remaining':10},{'transaction':'claim2','remaining':7}]
        self.assertEqual(consume_credit(lots,12),[{'claimTransaction':'claim1','amount':10},{'claimTransaction':'claim2','amount':2}])
        self.assertEqual(sum(x['remaining'] for x in lots),5)
        with self.assertRaises(EvidenceError):consume_credit(lots,12)

    def test_principal_or_donation_without_reward_credit_cannot_be_attributed(self):
        with self.assertRaises(EvidenceError):consume_credit([],10)

    def test_rejected_forward_does_not_mutate_known_credits(self):
        lots=[{'transaction':'claim','remaining':3}];before=copy.deepcopy(lots)
        with self.assertRaises(EvidenceError):consume_credit(lots,4)
        self.assertEqual(before,lots)

    def test_negative_amount_rejected(self):
        with self.assertRaises(EvidenceError):consume_credit([], -1)


class CloseoutIsolationTests(unittest.TestCase):
    def test_cli_forbids_advance_and_env_file_option(self):
        from . import __main__ as cli
        # Parser refuses this combination before calling closeout or any RPC loader.
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()), patch.object(cli,'load_project_rpc_override',side_effect=AssertionError('environment read')), \
             patch('tools.reconcile.closeout.run',side_effect=AssertionError('closeout run')):
            with self.assertRaises(SystemExit):cli.main(['closeout','--data-dir',tmp,'--advance'])
            with self.assertRaises(SystemExit):cli.main(['closeout','--data-dir',tmp,'--rpc-from-project-env'])


class AdditionalEvidenceTests(unittest.TestCase):
    def test_event_correlation_without_native_evidence_stays_unverified(self):
        from .inventory import STAKING
        snapshot=SNAPSHOT.copy();slp='0x'+'11'*20
        class FakeStore:
            def get(self,key,default=None):
                return {'checkpoints/snapshot.json':snapshot,
                        'cache/snapshots/reward-evidence.json':{'snapshot':snapshot,'cases':{}},
                        'cache/snapshots/state.json':{'custody':{slp:{'native':10}}}}.get(key,default)
        att={'proxies':{slp:{'name':'SlpPharosProxy'}}}
        events=[{'address':slp,'name':'RewardClaimed','args':{'amount':10},'raw':{'transactionHash':'claim','blockNumber':'0x1'}}]
        result=reward_ledger(FakeStore(),{},att,events,[])
        self.assertEqual(result['cashFlowStatus'],'UNVERIFIED')
        self.assertEqual(result['generationStatus'],'UNVERIFIED')

    def test_closeout_cli_does_not_construct_rpc(self):
        from . import __main__ as cli
        result={'statusCounts':{},'rpc':{'additionalRequests':0,'additionalEstimatedCU':0}}
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
             patch.object(cli,'Rpc',side_effect=AssertionError('network client')), \
             patch.object(cli,'load_project_rpc_override',side_effect=AssertionError('environment file')), \
             patch('tools.reconcile.closeout.run',return_value=result) as run:
            self.assertEqual(cli.main(['closeout','--data-dir',tmp]),0)
            run.assert_called_once()

    def test_compiler_version_must_match_cbor(self):
        record,cached=artifact_fixture();old=cached['identity']['cbor_metadata']['ipfs']
        record['metadata']=record['metadata'].replace('0.8.28','0.8.29')
        new=solc_ipfs_multihash(record['metadata'])
        record['deployedBytecode']=record['deployedBytecode'].replace(old,new)
        cached['runtimeCode']=record['deployedBytecode'];cached['identity']['keccak256']=keccak256(bytes.fromhex(cached['runtimeCode'][2:]))
        cached['identity']['cbor_metadata']['ipfs']=new
        with self.assertRaisesRegex(EvidenceError,'compiler version'):runtime_binding(record,cached)

    def test_safe_batch_decoder_is_exact_and_rejects_truncation(self):
        from .closeout import decode_safe_batch
        from tools.lib import abi
        from tools.lib.keccak import selector
        target='0x'+'11'*20;multi='0x'+'22'*20;zero='0x'+'00'*20
        packed=b'\0'+bytes.fromhex(target[2:])+bytes(32)+(2).to_bytes(32,'big')+b'\x12\x34'
        def envelope(data):
            inner=selector('multiSend(bytes)')+abi.encode_parameters([{'type':'bytes'}],['0x'+data.hex()])[2:]
            types=['address','uint256','bytes','uint8','uint256','uint256','uint256','address','address','bytes']
            return selector('execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)')+abi.encode_parameters([{'type':t} for t in types],[multi,0,inner,1,0,0,0,zero,zero,'0x01'])[2:]
        self.assertEqual(decode_safe_batch(envelope(packed))['calls'],[{'to':target,'value':0,'data':'0x1234'}])
        with self.assertRaises(EvidenceError):decode_safe_batch(envelope(packed[:-1]))


class CheckerScopeTests(unittest.TestCase):
    def test_stale_admin_assertion_is_an_operational_failure(self):
        from pathlib import Path
        from .closeout import stale_checker_finding
        root=Path('/fixture/read-only');gov='0x'+'33'*20;tl='0x'+'44'*20;slp='0x'+'55'*20
        files={str(root/'constants/constants.ts'):"export const MULTISIG_ADDRESS = { [CHAIN_ID.PHAROS_PACIFIC_MAINNET]: '"+gov+"' };"}
        for n in ['SlpPharosProxy','SlpPharosRewardsVault']:
            files[str(root/'tasks'/f'{n}.ts')]='expect(await x.hasRole(DEFAULT_ADMIN_ROLE, MULTISIG_ADDRESS[chainId])).to.be.true;'
        with patch.object(Path,'read_text',lambda p:files[str(p)]):
            f=stale_checker_finding(root,{'roles':{slp:{'0x'+'00'*32:[tl]}}},{'proxies':{slp:{}}})
        self.assertEqual(f['status'],'FAIL')
        self.assertIn('not a financial/contract failure',f['reason'])
        self.assertEqual(f['actual'][slp]['checkerExpectedAdminPresent'],False)


if __name__=='__main__':
    unittest.main()
