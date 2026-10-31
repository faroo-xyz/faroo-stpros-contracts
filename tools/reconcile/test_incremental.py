"""Incremental reconciliation regressions. All RPC transports are synthetic."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools.lib import abi
from tools.lib.keccak import keccak256
from .storage import Store, EvidenceError, canonical, digest, write, read
from .rpc import Rpc, RpcError, BudgetExceeded
from .collect import Collector, cached_logs
from .inventory import PROXY_ABI, UPGRADE
from .external import authenticate_incremental, bound_inventory
from . import incremental
from .test_reconcile import log


def header(n):
    return dict(number=hex(n), hash='0x'+f'{n:064x}', parentHash='0x'+f'{n-1:064x}', timestamp=hex(n))


def snapshot(n):
    return dict(number=n, hash=header(n)['hash'], finality='provider-finalized-tag', observedHead=n+1)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.calls = []
        self.scope = dict(id='scope', start=1, filter={'address': log()['address']}, label='test')
        self.plan = dict(snapshot=snapshot(10), scopes=[self.scope], initialChunk=2, rangeCeiling=2)
        self.store.put('checkpoints/snapshot.json', snapshot(10))
        self.store.put('checkpoints/plan.json', self.plan)
        self.store.put('cache/snapshots/headers.json', {hex(n): header(n) for n in (2, 4, 6, 8, 10)})
        for n in (2, 4, 6, 8, 10):
            self.store.chunk('scope', n-1, n, snapshot(10), [], {str(n): header(n)['hash']})
        self.result = {'snapshot': snapshot(10), 'collectionComplete': True, 'executionBlocker': None,
                       'replay': dict(supply=100, T=120, V=100, liability=2, queued=5, completed=3), 'checks': []}
        folder = incremental.report_dir(self.store, snapshot(10))
        write(folder / 'RECONCILIATION-RESULTS.json', self.result)
        self.store.put('cache/snapshots/state.json', {'snapshot': snapshot(10), 'supply': 100})
        self.boundary = incremental.seal(self.store, self.plan, self.result, folder)
        self.inv = {'start': 1, 'addresses': {'proxies': {'StPROS': log()['address']}},
                    'release': {'upgradeBlock': 2, 'upgradeTransaction': 'migration'}}

    def transport(self, method, params):
        self.calls.append((method, params))
        if method == 'eth_chainId': return '0x688'
        if method == 'eth_blockNumber': return '0xf'
        if method == 'eth_getBlockByNumber': return header(14 if params[0] == 'finalized' else int(params[0], 16))
        if method == 'eth_getLogs': return []
        raise AssertionError((method, params))

    def collector(self, transport=None, **kwargs):
        return Collector(self.store, Rpc(self.store, interval=0, transport=transport or self.transport, **kwargs), self.inv)

    def advance(self, collector):
        with patch('tools.reconcile.collect.scopes', return_value=[copy.deepcopy(self.scope)]):
            return collector.preflight(advance=True)

    def test_only_new_intervals_no_historical_anchor_or_preflight_probe(self):
        c = self.collector()
        plan = self.advance(c)
        c.sync(plan)
        ranges = [p[0] for m, p in self.calls if m == 'eth_getLogs']
        self.assertEqual([(int(r['fromBlock'],16),int(r['toBlock'],16)) for r in ranges], [(11,12),(13,14)])
        self.assertTrue(cached_logs(self.store, plan)[2])
        self.assertFalse(any(m == 'debug_traceTransaction' or m == 'eth_call' for m,p in self.calls))
        historical = [int(p[0],16) for m,p in self.calls if m == 'eth_getBlockByNumber' and p[0] != 'finalized' and int(p[0],16) < 10]
        self.assertEqual(historical, [])
        self.calls.clear()
        self.collector().sync(plan)
        self.assertFalse(any(m == 'eth_getLogs' for m,p in self.calls))

    def test_interrupted_advance_resumes_same_snapshot_and_gaps(self):
        c = self.collector()
        plan = self.advance(c)
        # First new interval was committed before the process stopped.
        c.header(12)
        self.store.chunk('scope',11,12,snapshot(14),[],{'12':header(12)['hash']})
        self.calls.clear()
        c = self.collector()
        resumed = self.advance(c)
        self.assertEqual(resumed['snapshot'], plan['snapshot'])
        self.assertFalse(any(m == 'eth_blockNumber' for m,p in self.calls))
        c.sync(resumed)
        ranges = [p[0] for m,p in self.calls if m == 'eth_getLogs']
        self.assertEqual([int(r['fromBlock'],16) for r in ranges], [13])

    def test_budget_exhaustion_preserves_committed_interval_and_resumes(self):
        c=self.collector(run_budget=500);plan=self.advance(c)
        plan.pop('rangeCeiling')  # Single-range requests make the interruption boundary explicit.
        plan['effectiveChunk']=2
        with self.assertRaises(BudgetExceeded):c.sync(plan)
        self.assertEqual(max(m['endBlock'] for m,_ in self.store.chunks('scope')),12)
        self.calls.clear()
        self.collector().sync(plan)
        self.assertEqual([int(p[0]['fromBlock'],16) for m,p in self.calls if m=='eth_getLogs'],[13])
        self.assertTrue(cached_logs(self.store,plan)[2])

    def test_reorg_at_previous_boundary_fails_before_logs(self):
        def reorg(m,p):
            value = self.transport(m,p)
            if m == 'eth_getBlockByNumber' and p[0] == '0xa': value['hash'] = 'changed'
            return value
        with self.assertRaises(EvidenceError): self.advance(self.collector(reorg))
        self.assertFalse(any(m == 'eth_getLogs' for m,p in self.calls))

    def test_broken_parent_continuity(self):
        c = self.collector(); plan = self.advance(c)
        def broken(m,p):
            value = self.transport(m,p)
            if m == 'eth_getBlockByNumber' and p[0] == '0xb': value['parentHash'] = 'wrong'
            return value
        with self.assertRaises(EvidenceError): self.collector(broken).sync(plan)

    def test_historical_anchor_tampering_with_valid_envelope(self):
        headers = self.store.get('cache/snapshots/headers.json'); headers['0x2']['hash'] = 'altered'
        self.store.put('cache/snapshots/headers.json', headers)
        with self.assertRaises(EvidenceError): self.advance(self.collector())

    def test_corrupted_chunk_and_missing_manifest_fail_closed(self):
        files = list((self.store.root/'cache/events').glob('*'))
        original = files[0].read_bytes(); files[0].write_bytes(b'corrupt')
        with self.assertRaises(EvidenceError): self.advance(self.collector())
        files[0].write_bytes(original)
        next((self.store.root/'cache/manifests').glob('*')).unlink()
        with self.assertRaises(EvidenceError): self.advance(self.collector())

    def test_new_interval_reorg_on_resume(self):
        c = self.collector(); plan = self.advance(c); c.sync(plan)
        def changed(m,p):
            value = self.transport(m,p)
            if m == 'eth_getBlockByNumber' and p[0] == '0xc': value['hash'] = 'new history'
            return value
        with self.assertRaises(EvidenceError): self.collector(changed).sync(plan)

    def test_advance_refuses_unfinalized_tag(self):
        def unavailable(m,p):
            if m == 'eth_getBlockByNumber' and p[0] == 'finalized': raise RpcError('unsupported')
            return self.transport(m,p)
        with self.assertRaises(EvidenceError): self.advance(self.collector(unavailable))

    def test_wrong_chain(self):
        with self.assertRaises(EvidenceError): self.advance(self.collector(lambda m,p:'0x1'))

    def test_cached_transaction_hash_is_checked(self):
        self.store.put('cache/receipts/a.json', {'blockHash':'wrong'})
        with self.assertRaises(EvidenceError):
            self.collector().cached_rpc('receipts','a','eth_getTransactionReceipt',['a'],'right')
        self.assertEqual(self.calls, [])

    def test_report_is_immutable_even_when_attempt_is_interrupted(self):
        source = self.store.root/'output/reconciliation/attempts/new'
        write(source/'RECONCILIATION-RESULTS.json', {'status':'complete'})
        dest = incremental.report_dir(self.store,snapshot(14))
        incremental.publish(self.store,source,dest)
        checksum = digest((dest/'RECONCILIATION-RESULTS.json').read_bytes())
        write(source/'RECONCILIATION-RESULTS.json', {'status':'BLOCKED'})
        incremental.publish(self.store,source,dest)
        self.assertEqual(checksum,digest((dest/'RECONCILIATION-RESULTS.json').read_bytes()))
        incremental.validate_boundary(self.store,self.boundary)

    def test_snapshot_delta_exact_units_and_findings(self):
        result = dict(self.result, snapshot=snapshot(14),
                      replay=dict(supply=103,T=127,V=103,liability=4,queued=9,completed=5),
                      conversions=[{'transaction':log(11)['transactionHash'],'kind':'Deposit','assets':7,'shares':3}],
                      checks=[{'id':'unknown','status':'UNVERIFIED'}],rpcUsage={})
        plan = dict(self.plan,snapshot=snapshot(14),historicalBoundary=incremental.identity(snapshot(10)))
        delta = incremental.summary(self.store,plan,result,[log(11)],{})
        self.assertEqual(delta['accounting']['supply']['change'],3)
        self.assertEqual(delta['accounting']['T']['change'],7)
        self.assertEqual(delta['movements']['Deposit'],{'assets':7,'shares':3,'assetsDecimal':'0.000000000000000007','sharesDecimal':'0.000000000000000003'})
        rate = delta['accounting']['PROSPerStPROS']['change']
        self.assertEqual((rate['numerator'],rate['denominator']),(17,515))
        self.assertEqual(delta['newBlockRange'],[11,14])
        self.assertEqual(delta['introducedFindings'],[{'id':'unknown','previous':None,'current':'UNVERIFIED'}])

    def test_reward_history_is_reused_without_balance_or_trace_reads(self):
        from .rewards import collect_evidence
        c = self.collector(); c.boundary = self.boundary
        c.inv={'targets':{}}
        prior = {'cases':{'old':{'complete':True,'trace':'AVAILABLE'}},'balanceReads':{'old:1':3}}
        c.boundary['context']['reward-evidence'] = prior
        out=collect_evidence(c,snapshot(14),[])
        self.assertEqual(out['cases'],prior['cases'])
        self.assertFalse(any(m in ('eth_getBalance','debug_traceTransaction') for m,p in self.calls))

    def test_prepared_publication_recovers_after_interrupt(self):
        c = self.collector(); plan = self.advance(c); c.sync(plan)
        result = dict(self.result, snapshot=plan['snapshot'])
        source = self.store.root / 'output/reconciliation/attempts/new'
        write(source / 'RECONCILIATION-RESULTS.json', result)
        destination = incremental.report_dir(self.store, plan['snapshot'])
        with patch('tools.reconcile.incremental.publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                incremental.commit_snapshot(self.store, plan, result, source)
        self.assertFalse(destination.exists())
        incremental.recover_snapshot(self.store)
        self.assertEqual(read(destination / 'RECONCILIATION-RESULTS.json'), result)
        self.assertEqual(incremental.load_boundary(self.store)['snapshot'], plan['snapshot'])

    def test_no_new_snapshot_keeps_original_comparison_boundary(self):
        c = self.collector(); plan = self.advance(c); c.sync(plan)
        source = self.store.root / 'output/reconciliation/attempts/new'
        result = dict(self.result, snapshot=plan['snapshot'])
        write(source / 'RECONCILIATION-RESULTS.json', result)
        incremental.commit_snapshot(self.store, plan, result, source)
        unchanged = self.advance(self.collector())
        self.assertEqual(unchanged, plan)
        self.assertEqual(unchanged['historicalBoundary'], incremental.identity(snapshot(10)))

    def test_historical_implementation_reads_reused_current_runtime_still_checked(self):
        address=log()['address']; implementation='0x'+'22'*20; runtime=keccak256(b'\x01')
        self.inv={'targets':{address:{'known':{implementation:'V4'}}},
                  'implementations':{implementation:{'runtimeKeccak':runtime}}}
        self.boundary['context']['identities']={'entries':{address:{'status':'PASS','verified':[
            {'implementation':implementation,'at':2,'runtimeKeccak':runtime}]}}}
        event=log(2,address=address)
        event['topics']=[abi.event_topic('Upgraded(address)'), '0x'+abi.enc_word(implementation)]
        def transport(m,p):
            self.calls.append((m,p))
            if m=='eth_getStorageAt':return '0x'+abi.enc_word(implementation)
            if m=='eth_getCode':return '0x01'
            raise AssertionError(m)
        c=self.collector(transport);c.boundary=self.boundary
        self.assertEqual(c.identities([event],snapshot(14))[address]['status'],'PASS')
        self.assertEqual([p[1] for m,p in self.calls if m=='eth_getCode'],['0xe'])

    def test_incomplete_or_unfinalized_collection_cannot_be_sealed(self):
        for result in (dict(self.result,collectionComplete=False), dict(self.result,executionBlocker='interrupted')):
            with self.assertRaises(EvidenceError):
                incremental.seal(self.store,self.plan,result,incremental.report_dir(self.store,snapshot(10)))



class RunBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Store(self.tmp.name)

    def test_per_run_and_cumulative_are_independent(self):
        self.store.put('checkpoints/rpc-usage.json',dict(requests=100,estimatedCU=2_960_000,methods={},errors={},retries=0))
        rpc=Rpc(self.store,interval=0,transport=lambda m,p:'ok',run_budget=40)
        rpc.call('eth_chainId',[]);rpc.call('eth_chainId',[])
        with self.assertRaises(BudgetExceeded):rpc.call('eth_chainId',[])
        self.assertEqual(rpc.run_usage()['estimatedCU'],40)
        self.assertEqual(rpc.ledger['estimatedCU'],2_960_040)
        again=Rpc(self.store,budget=2_960_060,interval=0,transport=lambda m,p:'ok',run_budget=100)
        again.call('eth_chainId',[])
        with self.assertRaises(BudgetExceeded):again.call('eth_chainId',[])
        self.assertEqual(again.ledger['estimatedCU'],2_960_060)
        self.assertEqual(len(list((self.store.root/'output/rpc-usage/runs').glob('*.json'))),2)

    def test_batch_ceiling_and_failed_attempt_charge(self):
        def bad(m,p):raise RpcError('unsupported')
        rpc=Rpc(self.store,interval=0,transport=bad,run_budget=200)
        with self.assertRaises(RpcError):rpc.batch([('eth_getLogs',[])])
        with self.assertRaises(BudgetExceeded):rpc.batch([('eth_getCode',[])])
        self.assertEqual(rpc.run_usage()['estimatedCU'],150)
        self.assertEqual(rpc.ledger['errors']['batch_rejected'],1)

    def test_retry_is_charged_and_stops_at_run_limit(self):
        def bad(m,p):raise RpcError('transport')
        rpc=Rpc(self.store,interval=0,transport=bad,run_budget=40)
        with patch('tools.reconcile.rpc.time.sleep'):
            with self.assertRaises(BudgetExceeded):rpc.call('eth_chainId',[])
        self.assertEqual(rpc.ledger['requests'],2)
        self.assertEqual(rpc.ledger['errors']['transport'],2)
        self.assertEqual(rpc.run_usage()['retries'],1)


class ExternalReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=Store(self.tmp.name)
        self.address='0x'+'11'*20;self.impl='0x'+'22'*20
        self.abi=[{'type':'function','name':'getBridgeVault','inputs':[],'outputs':[{'type':'address'}]}]
        binding={'status':'PASS','abi':self.abi,'abiSha256':digest(canonical(self.abi)),
                 'runtimeKeccak':keccak256(b'\x01'),'metadataSha256':'metadata','sourceKeccak256':{'source':'hash'},'recordSha256':'record','compilerInputSha256':'input'}
        self.proof={'status':'PASS','snapshot':snapshot(10),'implementations':{self.impl:binding},
                    'proxies':{self.address:{'status':'PASS','implementation':self.impl,'abi':self.abi+PROXY_ABI,'runtimeKeccak':'proxy','admin':'admin'}}}
        self.store.put('cache/snapshots/p0-closeout-attestation.json',self.proof)
        self.state={'snapshot':snapshot(14),'proxySlots':{self.address:{'implementation':self.impl,'runtimeKeccak':'proxy','admin':'admin'}}}
        self.store.put('cache/snapshots/external-state.json',self.state)
        self.inv={'targets':{self.address:{'name':'SlpPharosProxy','abi':[],'abiStatus':'UNVERIFIED_SOURCE_HINT'}}}
        self.c=Collector(self.store,Rpc(self.store,interval=0,transport=lambda m,p:'0x01'),self.inv)

    def test_same_implementation_installs_authenticated_abi_only_at_new_snapshot(self):
        current=authenticate_incremental(self.c,snapshot(14),[])
        self.assertEqual(current['entries'][self.address]['status'],'PASS')
        inv=bound_inventory(self.inv,self.proof,current,snapshot(14))
        self.assertEqual(inv['targets'][self.address]['abi'],self.abi+PROXY_ABI)
        stale=bound_inventory(self.inv,self.proof,current,snapshot(15))
        self.assertEqual(stale['targets'][self.address]['abi'],PROXY_ABI)

    def test_old_snapshot_slots_cannot_certify_new_implementation(self):
        self.state['snapshot']=snapshot(10)
        self.store.put('cache/snapshots/external-state.json',self.state)
        current=authenticate_incremental(self.c,snapshot(14),[])
        self.assertEqual(current['entries'][self.address]['status'],'UNVERIFIED')
        self.assertEqual(bound_inventory(self.inv,self.proof,current,snapshot(14))['targets'][self.address]['abi'],PROXY_ABI)

    def test_unknown_slot_never_uses_old_abi(self):
        self.state['proxySlots'][self.address]['implementation']='0x'+'33'*20
        self.store.put('cache/snapshots/external-state.json',self.state)
        current=authenticate_incremental(self.c,snapshot(14),[])
        self.assertEqual(current['entries'][self.address]['status'],'BLOCKED')
        self.assertEqual(bound_inventory(self.inv,self.proof,current,snapshot(14))['targets'][self.address]['abi'],PROXY_ABI)

    def test_upgrade_away_and_back_still_blocks_old_abi(self):
        row=log(11,address=self.address)
        row['topics']=[abi.event_topic('Upgraded(address)'), '0x'+abi.enc_word('0x'+'33'*20)]
        current=authenticate_incremental(self.c,snapshot(14),[row])
        self.assertEqual(current['entries'][self.address]['status'],'BLOCKED')

    def test_changed_runtime_is_blocked(self):
        self.c.rpc.transport=lambda m,p:'0x02'
        self.assertEqual(authenticate_incremental(self.c,snapshot(14),[])['entries'][self.address]['status'],'BLOCKED')

    def test_missing_mapping_stays_unverified(self):
        self.store.put('cache/snapshots/p0-closeout-attestation.json',{})
        current=authenticate_incremental(self.c,snapshot(14),[])
        self.assertEqual(current['entries'][self.address]['status'],'UNVERIFIED')

    def test_abi_tampering_fails_closed(self):
        self.proof['implementations'][self.impl]['abiSha256']='bad'
        self.store.put('cache/snapshots/p0-closeout-attestation.json',self.proof)
        with self.assertRaises(EvidenceError):authenticate_incremental(self.c,snapshot(14),[])


class ExistingCacheTests(unittest.TestCase):
    def completed_cache(self, root):
        """Build synthetic collected history; never read the mutable production cache."""
        from .test_reconcile import test_inventory
        from .inventory import scopes
        from .__main__ import offline, output_dir
        store = Store(root)
        inv = copy.deepcopy(test_inventory())
        old = snapshot(21647956)
        plan = {'snapshot': old, 'scopes': scopes(inv), 'initialChunk': 1000, 'rangeCeiling': 1000}
        store.put('cache/snapshots/inventory.json', inv)
        store.put('checkpoints/plan.json', plan)
        store.put('checkpoints/snapshot.json', old)
        store.put('cache/snapshots/headers.json', {hex(old['number']): header(old['number'])})
        for scope in plan['scopes']:
            store.chunk(scope['id'], scope['start'], old['number'], old, [], {str(old['number']): old['hash']})
        store.put('cache/snapshots/final-anchor-verification.json', {'snapshot': old, 'chainId': 1672, 'status': 'MATCH'})
        result = offline(store, inv)
        self.assertIsNone(result['executionBlocker'])
        self.assertTrue(result['collectionComplete'])
        for path in output_dir(store, plan).iterdir():
            shutil.copyfile(path, store.root / 'output/reconciliation' / path.name)
        boundary = incremental.bootstrap(store, plan)
        return store, inv, plan, boundary

    def test_original_cache_mock_advance_and_deterministic_offline(self):
        from .__main__ import main, offline, output_dir
        # Independent starting states: completed history may advance; pending history
        # must resume even when the provider could advertise a newer finalized block.
        for pending in (False, True):
            with self.subTest(pending=pending), tempfile.TemporaryDirectory() as temp:
                store, inv, plan, boundary = self.completed_cache(Path(temp))
                old = plan['snapshot']
                end = 21737768 if pending else old['number'] + 3
                advertised = end + 3 if pending else end
                vault = next(a for a, t in inv['targets'].items() if 'RewardsVault' in t['name'])
                slp = inv['addresses']['slpDestinations'][0].lower()
                row = log(end, address=vault)
                row.update(topics=[abi.event_topic('EthDeposited(address,uint256)'), '0x' + abi.enc_word(slp)],
                           data='0x' + abi.enc_word(10))
                tx = row['transactionHash']

                def selected(filt):
                    addresses = filt['address'] if isinstance(filt['address'], list) else [filt['address']]
                    return [row] if vault in addresses and int(filt['fromBlock'],16) <= end <= int(filt['toBlock'],16) else []

                if pending:
                    # Event-complete but receipt-incomplete pending snapshot, explicitly
                    # constructed independently of the operator's real progress.
                    pending_plan = dict(plan, snapshot=snapshot(end), historicalBoundary=incremental.identity(old))
                    store.put('checkpoints/plan.json', pending_plan)
                    store.put('checkpoints/snapshot.json', snapshot(end))
                    store.put('cache/snapshots/headers.json', {hex(n): header(n) for n in (old['number'], end)})
                    for scope in plan['scopes']:
                        filt = dict(scope['filter'], fromBlock=hex(old['number']+1), toBlock=hex(end))
                        store.chunk(scope['id'], old['number']+1, end, snapshot(end), selected(filt), {str(end): header(end)['hash']})
                before = {str(p.relative_to(store.root)): p.read_bytes()
                          for p in (store.root / 'output/reconciliation').rglob('*') if p.is_file()}
                calls = []

                def transport(method, params):
                    calls.append((method, params))
                    if method == 'eth_chainId': return '0x688'
                    if method == 'eth_blockNumber': return hex(advertised+1)
                    if method == 'eth_getBlockByNumber':
                        return header(advertised if params[0] == 'finalized' else int(params[0],16))
                    if method == 'eth_getLogs': return selected(params[0])
                    raise AssertionError(method)

                c = Collector(store, Rpc(store, interval=0, transport=transport), inv)
                advanced = c.preflight(advance=True)
                c.sync(advanced)
                self.assertEqual(advanced['snapshot'], snapshot(end))
                self.assertEqual(advanced['historicalBoundary'], incremental.identity(old))
                self.assertTrue(cached_logs(store, advanced)[2])
                ranges = [p[0] for m,p in calls if m == 'eth_getLogs']
                if pending:
                    self.assertEqual(ranges, [])
                    self.assertFalse(any(m == 'eth_blockNumber' or (m == 'eth_getBlockByNumber' and p[0] == 'finalized') for m,p in calls))
                else:
                    self.assertEqual(len(ranges), len(plan['scopes']))
                    self.assertTrue(all((int(r['fromBlock'],16), int(r['toBlock'],16)) == (old['number']+1,end) for r in ranges))
                    self.assertIn(('eth_getBlockByNumber', ['finalized', False]), calls)
                self.assertFalse(any(m in ('debug_traceTransaction','eth_call','eth_getCode') for m,p in calls))

                result = offline(store, inv)
                self.assertIsNone(result['executionBlocker'])
                self.assertFalse(result['externalState'])
                runtime_checks = [r for r in result['checks'] if r['id'].startswith('external-runtime-')]
                self.assertTrue(runtime_checks)
                self.assertTrue(all(r['status'] == 'UNVERIFIED' for r in runtime_checks))
                self.assertFalse(result['requiredEvidenceComplete'])
                self.assertEqual(result['transactionEvidence']['missingReceipts'], [tx])
                self.assertEqual(result['transactionEvidence']['missingRequiredTraces'], [tx])
                path = output_dir(store, advanced) / 'RECONCILIATION-RESULTS.json'
                first = path.read_bytes()
                offline(store, inv)
                self.assertEqual(first, path.read_bytes())
                published = incremental.report_dir(store, advanced['snapshot']) / 'RECONCILIATION-RESULTS.json'
                self.assertFalse(published.exists())

                def full_transport(method, params):
                    if method == 'eth_call': raise RpcError('unsupported')
                    if method == 'eth_getCode': return '0x'
                    if method == 'eth_getStorageAt': return '0x' + '00'*32
                    if method == 'eth_getBalance': return '0x0'
                    if method == 'eth_getTransactionReceipt':
                        calls.append((method, params))
                        self.assertEqual(params, [tx])
                        return {'blockNumber': row['blockNumber'], 'blockHash': row['blockHash'], 'status':'0x1', 'logs':[row]}
                    if method == 'eth_getTransactionByHash':
                        calls.append((method, params))
                        return {'blockHash': row['blockHash'], 'from':slp, 'to':vault, 'input':'0x'}
                    if method == 'debug_traceTransaction':
                        calls.append((method, params))
                        self.assertEqual(store.get('cache/receipts/' + tx + '.json')['status'], '0x1')
                        self.assertEqual(store.get('cache/receipts/' + tx + '-transaction.json')['blockHash'], row['blockHash'])
                        return {'type':'CALL', 'from':slp, 'to':vault, 'value':'0xa'}
                    return transport(method, params)

                def client(s, budget, interval, **kwargs):
                    return Rpc(s, budget, 0, transport=full_transport, **kwargs)

                calls.clear()
                with patch('tools.reconcile.__main__.Rpc', side_effect=client), patch('builtins.print'):
                    # Neither receipt-cap nor trace-cap exhaustion may finalize this snapshot.
                    for limits, missing_receipts in ((['--max-receipts','0'], [tx]), (['--max-traces','0'], [])):
                        self.assertEqual(main(['all','--advance','--data-dir',temp] + limits), 2)
                        attempt = read(path)
                        self.assertTrue(attempt['collectionComplete'])
                        self.assertFalse(attempt['requiredEvidenceComplete'])
                        self.assertEqual(attempt['transactionEvidence']['missingReceipts'], missing_receipts)
                        self.assertEqual(attempt['transactionEvidence']['missingRequiredTraces'], [tx])
                        self.assertFalse(published.exists())
                        self.assertEqual(incremental.load_boundary(store)['snapshot'], old)
                    self.assertEqual(main(['all','--advance','--data-dir',temp]), 0)
                self.assertFalse(any(m in ('eth_getLogs','eth_blockNumber') or (m == 'eth_getBlockByNumber' and p[0] == 'finalized') for m,p in calls))
                self.assertEqual([m for m,p in calls if m.startswith('eth_getTransaction') or m == 'debug_traceTransaction'],
                                 ['eth_getTransactionReceipt','eth_getTransactionByHash','debug_traceTransaction'])
                completed = read(published)
                self.assertTrue(completed['collectionComplete'])
                self.assertTrue(completed['requiredEvidenceComplete'])
                self.assertEqual(completed['rewardProvenance']['status'], 'UNVERIFIED')
                self.assertTrue(all(r['status'] == 'UNVERIFIED' for r in completed['checks'] if r['id'].startswith('external-runtime-')))
                self.assertEqual(incremental.load_boundary(store)['snapshot'], snapshot(end))
                saved = published.read_bytes()
                with patch('tools.reconcile.__main__.Rpc', side_effect=AssertionError('offline RPC forbidden')), patch('builtins.print'):
                    self.assertEqual(main(['reconcile','--data-dir',temp]), 0)
                self.assertEqual(saved, published.read_bytes())
                for relative, content in before.items():
                    self.assertEqual(content, (store.root / relative).read_bytes())
                incremental.validate_boundary(store, boundary)


if __name__ == '__main__':
    unittest.main()
