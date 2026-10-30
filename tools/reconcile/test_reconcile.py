"""Focused synthetic tests; no network, Foundry, environment files or signing."""
import gzip
import json
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch
from tools.lib import abi
from .storage import Store, EvidenceError, canonical, digest, gaps, ordered
from .rpc import Rpc, RpcError, BudgetExceeded
from .collect import Collector, cached_logs
from .inventory import discover, decode, UPGRADE, ZERO
from .replay import Queue, commission, replay


@lru_cache(maxsize=1)
def test_inventory():
    return discover()


def log(block=10, index=0, tx=0, address='0x'+'11'*20):
    return dict(blockNumber=hex(block), transactionIndex=hex(tx), logIndex=hex(index),
                blockHash='0x'+f'{block:064x}', transactionHash='0x'+f'{tx+1:064x}',
                address=address, topics=[], data='0x', removed=False)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    def test_interrupted_payload_is_not_committed(self):
        p = self.store.root / 'cache/events/orphan.json.gz'
        p.write_bytes(gzip.compress(b'[]', mtime=0))
        self.assertEqual(self.store.chunks('a'), [])
        self.store.chunk('a', 1, 10, {'number': 10}, [], {'10': 'hash'})
        self.assertEqual(list(gaps([(1, 10)], 1, 20)), [(11, 20)])
        self.assertEqual(len(self.store.chunks('a')), 1)

    def test_corruption_detected(self):
        m = self.store.chunk('a', 1, 10, {}, [], {})
        (self.store.root / m['file']).write_bytes(b'bad')
        with self.assertRaises(EvidenceError):
            self.store.chunks('a')

    def test_manifest_corruption_detected(self):
        self.store.put('checkpoints/a.json', {'a': 1})
        p = self.store.root / 'checkpoints/a.json'
        p.write_text(p.read_text().replace('"a":1', '"a":2'))
        with self.assertRaises(EvidenceError):
            self.store.get('checkpoints/a.json')

    def test_empty_intervals_and_missing_coverage(self):
        self.assertEqual(list(gaps([(1, 2), (5, 8)], 1, 10)), [(3, 4), (9, 10)])
        with self.assertRaises(EvidenceError):
            list(gaps([(1, 3), (3, 4)], 1, 4))

    def test_order_duplicate_conflict(self):
        a, b = log(2), log(1)
        self.assertEqual(ordered([a, b]), [b, a])
        with self.assertRaises(EvidenceError):
            ordered([a, a])
        self.assertEqual(ordered([a, a], allow_identical=True), [a])
        with self.assertRaises(EvidenceError):
            ordered([log(tx=0), log(tx=1)])
        with self.assertRaises(EvidenceError):
            ordered([a, dict(a, data='0xff')], allow_identical=True)

    def test_removed_log_refused(self):
        with self.assertRaises(EvidenceError):
            ordered([dict(log(), removed=True)])

    def test_deterministic_compression(self):
        a = self.store.chunk('a', 1, 1, {}, [log()], {})
        b = self.store.chunk('a', 1, 1, {}, [log()], {})
        self.assertEqual(a['sha256'], b['sha256'])

    def test_budget_exhaustion_persists(self):
        rpc = Rpc(self.store, budget=40, interval=0, transport=lambda m, p: '0x688')
        rpc.call('eth_chainId', [])
        rpc.call('eth_chainId', [])
        with self.assertRaises(BudgetExceeded):
            rpc.call('eth_chainId', [])
        self.assertEqual(self.store.get('checkpoints/rpc-usage.json')['requests'], 2)
        again = Rpc(self.store, budget=40, interval=0, transport=lambda m, p: None)
        with self.assertRaises(BudgetExceeded):
            again.call('eth_chainId', [])

    def test_readonly_methods(self):
        rpc = Rpc(self.store, interval=0, transport=lambda m, p: None)
        for method in ('eth_sendRawTransaction', 'eth_sendTransaction', 'personal_sign', 'debug_setHead'):
            with self.assertRaises(EvidenceError):
                rpc.call(method, [])
        self.assertEqual(rpc.ledger['requests'], 0)

    def test_failed_requests_charged_and_redacted(self):
        def transport(m, p):
            raise ValueError('https://sensitive.example/API-SECRET')
        rpc = Rpc(self.store, interval=0, transport=transport)
        with self.assertRaises(RpcError) as caught:
            rpc.call('eth_call', [], retries=0)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(rpc.ledger['requests'], 1)
        self.assertEqual(rpc.ledger['estimatedCU'], 100)

    def test_chain_reorganization(self):
        self.store.put('cache/snapshots/headers.json', {'0xa': {'hash': 'old'}})
        rpc = Rpc(self.store, interval=0, transport=lambda m, p: dict(number='0xa', hash='new', parentHash='p', timestamp='0x1'))
        c = Collector(self.store, rpc, {})
        with self.assertRaises(EvidenceError):
            c.header(10, fresh=True)

    def test_snapshot_hash_mismatch(self):
        rpc = Rpc(self.store, interval=0, transport=lambda m, p: dict(number='0xa', hash='new', parentHash='p', timestamp='0x1'))
        with self.assertRaises(EvidenceError):
            Collector(self.store, rpc, {}).check_snapshot({'number': 10, 'hash': 'old'})

    def test_range_scope_validation(self):
        a = log()
        with self.assertRaises(EvidenceError):
            Collector.validate_logs([a], {'address': a['address']}, 1, 9)
        with self.assertRaises(EvidenceError):
            Collector.validate_logs([a], {'address': ZERO}, 1, 20)


class AccountingTests(unittest.TestCase):
    def test_legacy_aggregate_per_owner_not_global_fifo(self):
        q = Queue()
        q.enqueue('a', 'x', 100, 0, 7, 'V1')
        q.enqueue('b', 'b', 30, 0, 7, 'V1')
        q.enqueue('a', 'x', 50, 0, 7, 'V1')
        q.pay('b', 'b', 30, 8, 'V1')
        self.assertEqual(q.pay('a', 'chosen-at-claim', 125, 8, 'V1'), 2)
        self.assertEqual(q.remaining(), 25)
        self.assertEqual(q.records['a'][1]['queued'], 155)
        self.assertEqual(q.queued - q.completed, q.remaining())

    def test_v4_partial_full_repeated(self):
        q = Queue()
        q.enqueue('a', 'receiver', 100, 1, 10, 'V4')
        q.pay('a', 'receiver', 40, 11, 'V4')
        q.pay('a', 'receiver', 60, 12, 'V4')
        self.assertEqual(q.remaining(), 0)
        self.assertEqual(q.head['a'], 1)
        with self.assertRaises(EvidenceError):
            q.pay('a', 'receiver', 1, 12, 'V4')

    def test_v4_record_bound_and_receiver(self):
        q = Queue()
        q.enqueue('a', 'r', 10, 0, 1, 'V4')
        with self.assertRaises(EvidenceError):
            q.pay('a', 'wrong', 1, 2, 'V4')
        q.enqueue('a', 'r', 10, 0, 1, 'V4')
        with self.assertRaises(EvidenceError):
            q.pay('a', 'r', 15, 2, 'V4')

    def test_waiting_period_snapshot(self):
        q = Queue()
        q.enqueue('a', 'a', 3, 0, 100, 'V4')
        with self.assertRaises(EvidenceError):
            q.pay('a', 'a', 1, 99, 'V4')
        q.pay('a', 'redirect', 3, 100, 'V4')
        self.assertEqual(q.completed, 3)

    def test_exact_commission_rounding(self):
        # 10% of 101 = 10; 10*1000/(1000+101-10) = floor(9.16...) = 9.
        self.assertEqual(commission(1000, 1000, 101, 100000), (10, 9))
        self.assertEqual(commission(1000, 1000, 1, 100000), (0, 0))
        self.assertEqual(commission(10**30, 10**30, 10**25, 0), (0, 0))
        with self.assertRaises(EvidenceError):
            commission(0, 0, 1, 100000)

    @classmethod
    def setUpClass(cls):
        cls.inv = test_inventory()

    def test_no_evidence_never_pass(self):
        result = replay(self.inv, [])
        self.assertTrue(all(c['status'] != 'PASS' for c in result['checks']))
        self.assertIn('no solvency conclusion', result['coverageConclusion'])

    def test_unknown_version_not_decoded_as_v4(self):
        st = self.inv['addresses']['proxies']['StPROS'].lower()
        result = replay(self.inv, [log(address=st)])
        self.assertEqual(len(result['unknownEvents']), 1)
        self.assertTrue(all(c['status'] != 'PASS' for c in result['checks']))

    def test_same_signature_different_indexing(self):
        old = next(x for x in self.inv['implementations'].values() if x['contract'] == 'Oracle' and x['version'] == 'V1')
        new = next(x for x in self.inv['implementations'].values() if x['contract'] == 'Oracle' and x['version'] == 'V4')
        sig = abi.event_topic('SetTokenAmount(address,uint256,uint256)')
        l = dict(log(), topics=[sig], data='0x'+abi.enc_word(ZERO)+abi.enc_word(5)+abi.enc_word(7))
        self.assertEqual(decode(l, old['abi'])['args'], [ZERO, 5, 7])
        with self.assertRaises(ValueError):
            decode(l, new['abi'])


if __name__ == '__main__':
    unittest.main()

class TraceTests(unittest.TestCase):
    def test_no_delegatecall_double_count_or_reverted_value(self):
        from .assets import native_movements
        trace = {'type': 'CALL', 'from': 'a', 'to': 'b', 'value': '0xa', 'calls': [
            {'type': 'DELEGATECALL', 'from': 'a', 'to': 'b', 'value': '0xa'},
            {'type': 'CALL', 'from': 'b', 'to': 'c', 'value': '0x3', 'error': 'reverted',
             'calls': [{'type': 'CALL', 'from': 'c', 'to': 'd', 'value': '0x3'}]},
            {'type': 'CALL', 'from': 'b', 'to': 'e', 'value': '0x7'}]}
        self.assertEqual([r['value'] for r in native_movements(trace)], [10, 7])

    def test_unknown_outflow_is_unverified_not_authorized(self):
        from .assets import assess
        inv = test_inventory()
        st = inv['addresses']['proxies']['StPROS'].lower()
        result = assess(inv, {'tx': {'type': 'CALL', 'from': st, 'to': 'stranger', 'value': '0x1'}},
                        {'tx': {'blockNumber': '0x1'}}, {'replay': {'migrations': []}})
        self.assertIn('unresolved', result['movements'][0]['classification'])
        self.assertIsNone(result['verifiedAttributableAssets'])
        self.assertEqual(result['status'], 'UNVERIFIED')


class SyncTests(unittest.TestCase):
    setUp = StorageTests.setUp
    def test_resume_does_not_redownload_logs(self):
        addr = log()['address']
        snap = {'number': 10, 'hash': '0x'+f'{10:064x}'}
        filt = {'address': addr}
        plan = {'snapshot': snap, 'initialChunk': 10, 'rangeCeiling': 10,
                'scopes': [{'id': 's', 'start': 1, 'filter': filt}]}
        calls = []
        def transport(method, params):
            calls.append(method)
            if method == 'eth_chainId':
                return '0x688'
            if method == 'eth_getBlockByNumber':
                b = int(params[0], 16)
                return {'number': hex(b), 'hash': '0x'+f'{b:064x}', 'parentHash': '0x'+f'{b-1:064x}', 'timestamp': hex(b)}
            if method == 'eth_getLogs':
                return []
            raise AssertionError(method)
        rpc = Rpc(self.store, interval=0, transport=transport)
        c = Collector(self.store, rpc, {})
        c.sync(plan)
        first = calls.count('eth_getLogs')
        c.sync(plan)
        self.assertEqual(first, 1)
        self.assertEqual(calls.count('eth_getLogs'), first)
        self.assertTrue(cached_logs(self.store, plan)[2])

    def test_batch_each_element_charged(self):
        rpc = Rpc(self.store, interval=0, transport=lambda m, p: [])
        rpc.batch([('eth_getLogs', [])] * 3)
        self.assertEqual(rpc.ledger['requests'], 3)
        self.assertEqual(rpc.ledger['estimatedCU'], 450)

class ReplayTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inv = test_inventory()

    def event_log(self, addr, version, name, values, block, idx, tx):
        inv = self.inv
        entry = next((v for v in inv['implementations'].values()
                      if v['contract'] == inv['targets'][addr]['name'] and v['version'] == version), None)
        items = [UPGRADE] if name == 'Upgraded' else entry['abi']
        item = next(x for x in items if x.get('type') == 'event' and x['name'] == name)
        topics = [abi.event_topic(abi.item_signature(item))]
        params, nonindexed = [], []
        for p, value in zip(item['inputs'], values):
            if p.get('indexed'):
                topics.append(abi.encode_parameters([p], [value]))
            else:
                params.append(p); nonindexed.append(value)
        raw = log(block, idx, tx, addr)
        raw['topics'], raw['data'] = topics, abi.encode_parameters(params, nonindexed)
        return raw

    def test_v1_v4_intrablock_migration_and_zero_supply(self):
        import copy
        inv = copy.deepcopy(self.inv)
        st, oracle = [inv['addresses']['proxies'][n].lower() for n in ('StPROS', 'Oracle')]
        asset = inv['addresses']['asset']['WPROS'].lower()
        holder = '0x'+'ab'*20
        initial = next(k for k,v in inv['implementations'].items() if v['contract']=='StPROS' and v['version']=='V1')
        current = next(k for k,v in inv['implementations'].items() if v['contract']=='StPROS' and v['version']=='V4')
        old_o = next(k for k,v in inv['implementations'].items() if v['contract']=='Oracle' and v['version']=='V1')
        new_o = next(k for k,v in inv['implementations'].items() if v['contract']=='Oracle' and v['version']=='V4')
        inv['release']['upgradeTransaction'] = '0x'+f'{3:064x}'
        specs = [
            (st,'V1','Upgraded',[initial],1,0,0), (oracle,'V1','Upgraded',[old_o],1,1,0),
            (st,'V1','Transfer',[ZERO,holder,100],1,2,0),
            (st,'V1','Deposit',[holder,holder,100,100],1,3,0),
            (st,'V1','Transfer',[holder,ZERO,30],2,0,1),
            (st,'V1','Transfer',[ZERO,st,30],2,1,1),
            (st,'V1','Withdraw',[holder,holder,holder,30,30],2,2,1),
            # Same block, different transaction: upgrade precedes migration burn and initialization.
            (oracle,'V4','Upgraded',[new_o],2,3,2), (st,'V4','Upgraded',[current],2,4,2),
            (st,'V4','Transfer',[st,ZERO,30],2,5,2),
            (oracle,'V4','SetTokenAmount',[asset,70,70],2,6,2),
            (st,'V4','Transfer',[holder,ZERO,70],3,0,3),
            (oracle,'V4','PoolUpdated',[asset,0,0,False],3,1,3),
            (st,'V4','Withdraw',[holder,holder,holder,70,70],3,2,3)]
        logs = [self.event_log(*x) for x in specs]
        headers = {hex(b): {'timestamp': hex(b)} for b in (1,2,3)}
        result = replay(inv, list(reversed(logs)), {'number':3}, headers=headers)
        self.assertEqual(result['replay']['supply'],0)
        self.assertEqual((result['replay']['T'],result['replay']['V']),(0,0))
        self.assertEqual(result['replay']['liability'],100)
        self.assertEqual(result['replay']['migrations'][0]['escrowBurn'],30)
        self.assertEqual(result['replay']['poolComparisons'],1)
        self.assertEqual(result['replay']['burned'],130)
        self.assertEqual(result['replay']['minted'],130)
        replay_again = replay(inv, logs, {'number':3}, headers=headers)
        self.assertEqual(canonical(result),canonical(replay_again))

class CoverageTests(unittest.TestCase):
    def test_disjoint_staking_buckets(self):
        from .assets import provisional_coverage
        e = {'positions': {'slp': {'pools': [{'activeStakeIncludingCompoundedRewards': 100,
            'pendingStakeIncludingCompoundedRewards': 20, 'principalStake': 110, 'pendingWithdrawStake': 80,
            'unclaimedRewards': 7}], 'pendingUndelegations': [{'processed': 0, 'amount': 80, 'principalAmount': 75}]}}}
        result = provisional_coverage({'cash': {'native': 10, 'WPROS': 0}}, e,
                                      {'T': 180, 'queued': 50, 'completed': 20})
        self.assertEqual(result['provisionalGrossAssets'], 210)
        self.assertEqual(result['principalMemoOnlyNotAddedAgain'], 185)
        self.assertEqual(result['provisionalDifference'], 0)
        self.assertEqual(result['status'], 'UNVERIFIED')

class ConversionTests(unittest.TestCase):
    def test_deposit_floor_mint_ceil_and_mismatched_evidence(self):
        from .conversion import check
        from tools.lib.keccak import selector
        inv = test_inventory()
        st = inv['addresses']['proxies']['StPROS'].lower()
        events = [{'transaction':'tx', 'logIndex':'0x1', 'kind':'Deposit', 'preT':3,'preV':2, 'assets':2,'shares':1}]
        tx = {'tx': {'to':st,'input':selector('deposit(uint256,address)')}}
        self.assertEqual(check(inv, events, tx, {}, True)['status'],'PASS')
        tx['tx']['input']=selector('mint(uint256,address)')
        self.assertEqual(check(inv, events, tx, {}, True)['status'],'PASS')
        events[0]['assets']=1
        self.assertEqual(check(inv, events, tx, {}, True)['status'],'FAIL')
        self.assertEqual(check(inv, events, {}, {}, True)['status'],'UNVERIFIED')

    def test_corrupt_offline_report_is_blocked(self):
        from .__main__ import offline
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            store.put('checkpoints/plan.json', {'wrong':'schema'})
            result=offline(store, test_inventory())
            self.assertEqual(result['checks'][0]['status'],'BLOCKED')
            self.assertEqual(result['checks'][0]['id'],'cache-integrity')

class ReceiptTests(unittest.TestCase):
    def test_receipt_reveals_missing_scoped_log(self):
        inv = test_inventory()
        st = inv['addresses']['proxies']['StPROS'].lower()
        one, two = log(block=inv['start'],address=st), log(block=inv['start'],index=1,address=st)
        def transport(method, params):
            if method == 'eth_getTransactionReceipt':
                return {'blockHash':one['blockHash'],'status':'0x1','logs':[one,two]}
            if method == 'eth_getTransactionByHash':
                return {'blockHash':one['blockHash']}
            raise AssertionError(method)
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            c=Collector(store,Rpc(store,interval=0,transport=transport),inv)
            with self.assertRaisesRegex(EvidenceError,'missing scoped log'):
                c.transactions([one],max_traces=0)

class EnvironmentIsolationTests(unittest.TestCase):
    def test_offline_flag_never_loads_environment_or_rpc(self):
        import contextlib
        import io
        from . import __main__ as cli
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(cli, 'load_project_rpc_override', side_effect=AssertionError('unexpected environment read')), \
             patch.object(cli, 'Rpc', side_effect=AssertionError('unexpected network client')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['reconcile','--data-dir',tmp,'--rpc-from-project-env']),0)

    def test_literal_override_never_executes_shell(self):
        import io
        import os
        from .rpc import load_project_rpc_override
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(Path, 'open', return_value=io.StringIO('IGNORED_TEST_VALUE=ignored\nPHAROS_RPC_URL="$(touch forbidden)"\n')), \
             patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EvidenceError):
                load_project_rpc_override(Path(tmp))
            self.assertNotIn('PHAROS_RPC_URL',os.environ)

class SnapshotArchiveTests(unittest.TestCase):
    def test_advance_archive_preserves_multiple_evidence_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            snap={'number':1,'hash':'h'}
            store.put('cache/snapshots/state.json',{'snapshot':snap,'supply':2})
            store.archive_snapshot(snap)
            store.put('cache/snapshots/state.json',{'snapshot':snap,'supply':2,'holders':{}})
            store.archive_snapshot(snap)
            files=list((store.root/'cache/snapshots/history').rglob('cache__snapshots__state.json.*'))
            self.assertEqual(len(files),2)

class IncrementalEvidenceTests(unittest.TestCase):
    def test_cached_receipts_do_not_starve_new_transactions(self):
        inv=test_inventory();st=inv['addresses']['proxies']['StPROS'].lower()
        old=log(block=inv['start'],index=0,tx=0,address=st)
        new=log(block=inv['start'],index=1,tx=1,address=st)
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            store.put('cache/receipts/'+old['transactionHash']+'.json',{'status':'0x1','blockHash':old['blockHash'],'logs':[old]})
            store.put('cache/receipts/'+old['transactionHash']+'-transaction.json',{'blockHash':old['blockHash']})
            def transport(method,params):
                self.assertEqual(params,[new['transactionHash']])
                if method=='eth_getTransactionReceipt':
                    return {'status':'0x1','blockHash':new['blockHash'],'logs':[new]}
                return {'blockHash':new['blockHash']}
            rpc=Rpc(store,interval=0,transport=transport)
            Collector(store,rpc,inv).transactions([old,new],max_receipts=1,max_traces=0)
            coverage=store.get('checkpoints/transaction-coverage.json')
            self.assertEqual(coverage['receipts'],2)
            self.assertEqual(coverage['newReceiptAttempts'],1)
            self.assertEqual(rpc.ledger['requests'],2)

class RewardAttributionTests(unittest.TestCase):
    def test_reward_credit_crosses_transactions_without_claiming_provenance(self):
        from .rewards import provenance
        from .inventory import STAKING
        inv=test_inventory();slp=inv['addresses']['slpDestinations'][0].lower();rv=inv['addresses']['actors']['reporter'].lower()
        events=[{'address':slp,'name':'RewardClaimed','args':{'amount':10},'raw':{'transactionHash':'claim'}},
                {'address':STAKING,'name':'RewardClaimed','args':{'delegator':slp,'amount':10},'raw':{'transactionHash':'claim'}},
                {'address':STAKING,'name':'CoinMinted','args':{'beneficiary':slp,'reason':1,'amount':10},'raw':{'transactionHash':'claim'}},
                {'address':rv,'name':'EthDeposited','args':{'sender':slp,'amount':10},'raw':{'transactionHash':'forward'}}]
        result=provenance(inv,events,[],{rv:{'native':10}})
        self.assertEqual(result['vaults'][rv]['stakingCorrelatedInflow'],10)
        self.assertEqual(result['vaults'][rv]['unclassifiedInflow'],0)
        self.assertEqual(result['status'],'UNVERIFIED')

class CreationTransactionTests(unittest.TestCase):
    def test_null_to_is_valid_creation_transaction(self):
        from .__main__ import _offline
        from .conversion import calls
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            store.put('cache/receipts/0x01-transaction.json',{'to':None,'input':'0x1234'})
            result=_offline(store,test_inventory())
            self.assertIsNone(result['executionBlocker'])
        self.assertEqual(calls({'type':'CALL','to':None,'input':'0x1234'},'a'),[])

class MigrationAssetsTests(unittest.TestCase):
    def test_isolated_migration_asset_transition_and_ambiguous_block(self):
        from .assets import assess
        inv=test_inventory();p=inv['addresses']['proxies'];st=p['StPROS'].lower();bridge=p['BridgeVault'].lower();dist=p['SlpDistributor'].lower()
        wp=inv['addresses']['asset']['WPROS'].lower();tx=inv['release']['upgradeTransaction'].lower()
        trace={'type':'CALL','calls':[{'type':'CALL','from':st,'to':bridge,'value':'0x5'},{'type':'CALL','from':st,'to':dist,'value':'0xa'}]}
        result={'replay':{'migrations':[{'transaction':tx,'outstandingLiability':5}]},'collectionComplete':True,
                'deploymentIdentity':{st:{'status':'PASS'},bridge:{'status':'PASS'}}}
        receipt={tx:{'blockNumber':'0x1'}};logs=[]
        for i,amount in enumerate([5,10]):
            l=log(block=1,index=i,address=wp);l.update(transactionHash=tx,topics=[abi.event_topic('Withdrawal(address,uint256)'),'0x'+abi.enc_word(st)],data='0x'+abi.enc_word(amount));logs.append(l)
        state={'beforeWrapped':15,'afterWrapped':0}
        def status(data):
            return next(c['status'] for c in assess(inv,{tx:trace},receipt,result,state,data,True)['checks'] if c['id']=='migration-active-assets')
        self.assertEqual(status(logs),'PASS')
        self.assertEqual(status(logs+[dict(logs[0],transactionHash='other')]),'UNVERIFIED')

class HistoricalAuthorityTests(unittest.TestCase):
    def test_closed_window_remains_visible_without_inventing_violation(self):
        import copy
        from .governance import historical_admin_findings
        inv=copy.deepcopy(test_inventory());inv['release']['upgradeBlock']=100
        admin='admin';timelock=inv['addresses']['governance']['timelock'].lower()
        es=[{'address':admin,'name':'OwnershipTransferred','args':{'newOwner':'operator'},'raw':{'blockNumber':'0x32'}},
            {'address':admin,'name':'OwnershipTransferred','args':{'newOwner':timelock},'raw':{'blockNumber':'0x96'}}]
        result=historical_admin_findings(inv,es,{'proxySlots':{'slp':{'admin':admin}}},{'number':200})
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['status'],'UNVERIFIED')
        self.assertEqual(result[0]['actual']['windows'][0]['fromBlock'],100)
        self.assertEqual(result[0]['actual']['windows'][0]['untilBlockExclusiveApproximation'],150)
        self.assertEqual(result[0]['actual']['currentOwnerFromEvents'],timelock)
