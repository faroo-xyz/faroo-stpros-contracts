"""Receipt-first regressions; all state is temporary and RPC is mocked."""
import copy
import tempfile
import unittest
from unittest.mock import patch
from tools.lib import abi
from .collect import Collector, transaction_readiness
from .inventory import event
from .rpc import Rpc, RpcError, BudgetExceeded
from .storage import Store, EvidenceError, write
from . import incremental, rewards
from .test_reconcile import log, test_inventory
from .test_incremental import header, snapshot


class ReceiptSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.snap = snapshot(21737768)
        self.previous = snapshot(21647956)
        self.st, self.slp, self.vault = ('0x' + c * 40 for c in ('1', '2', '3'))
        self.deposit = event('EthDeposited', [('sender','address',True), ('amount','uint256',False)])
        self.inv = {'targets': {self.st: {'name':'StPROS','abi':[]},
                               self.slp: {'name':'SlpPharosProxy','abi':[]},
                               self.vault: {'name':'SlpPharosRewardsVault','abi':[self.deposit]}},
                    'addresses': {'proxies': {'StPROS':self.st}}, 'release': {'upgradeTransaction':'migration'}}
        self.logs = [log(21737760+i, tx=i, address=self.vault if i%2 else self.st) for i in range(4)]
        for i, row in enumerate(self.logs):
            if i%2:
                row.update(topics=[abi.event_topic('EthDeposited(address,uint256)'), '0x'+abi.enc_word(self.slp)], data='0x'+abi.enc_word(10))
        self.by_tx = {l['transactionHash']: l for l in self.logs}
        self.store.put('cache/snapshots/headers.json', {hex(n):header(n) for n in range(21737759,21737769)})
        self.plan = {'snapshot':self.snap, 'scopes':[{'id':'s','start':21737760,'filter':{'address':[self.st,self.slp,self.vault]}}]}
        self.store.put('checkpoints/snapshot.json', self.snap)
        self.store.put('checkpoints/plan.json', self.plan)
        self.store.put('checkpoints/rpc-usage.json', dict(requests=10,estimatedCU=2_960_000,methods={},errors={},retries=0))
        self.calls = []

    def transport(self, method, params):
        self.calls.append((method,params))
        if method == 'eth_getTransactionReceipt':
            row = self.by_tx[params[0]]
            return {'status':'0x1', 'blockHash':row['blockHash'], 'blockNumber':row['blockNumber'], 'logs':[row]}
        if method == 'eth_getTransactionByHash':
            return {'blockHash':self.by_tx[params[0]]['blockHash']}
        if method == 'debug_traceTransaction':
            # Every detail pair, including optional transactions, precedes every trace.
            for tx in self.by_tx:
                self.assertIsNotNone(self.store.get('cache/receipts/'+tx+'.json'))
                self.assertIsNotNone(self.store.get('cache/receipts/'+tx+'-transaction.json'))
            return {'type':'CALL','from':self.st,'to':self.vault,'value':'0x0'}
        if method == 'eth_getBlockByNumber': return header(int(params[0],16))
        if method == 'eth_getBalance': return '0x0'
        raise AssertionError('Unexpected RPC: '+method)

    def collector(self, budget=100_000, cumulative=5_000_000, transport=None):
        c = Collector(self.store,Rpc(self.store,budget=cumulative,interval=0,
                      run_budget=budget,transport=transport or self.transport),self.inv)
        c.boundary = {'snapshot':self.previous,'context':{}}
        return c

    def reward_events(self):
        return [{'name':'EthDeposited','address':self.vault,'args':{'sender':self.slp,'amount':10},'raw':l}
                for l in self.logs if l['address']==self.vault]

    def test_repeated_small_budgets_finish_receipts_before_any_trace(self):
        counts = []
        for _ in range(4):
            c = self.collector(250)
            try: c.transactions(self.logs)
            except BudgetExceeded: pass
            counts.append(sum(self.store.get('cache/receipts/'+tx+'-transaction.json') is not None for tx in self.by_tx))
        self.assertEqual(counts,[1,2,3,4])
        self.assertFalse(any(m=='debug_traceTransaction' for m,p in self.calls))
        self.assertEqual(self.store.get('checkpoints/rpc-usage.json')['estimatedCU'],2_960_800)
        first = self.collector(10_200).transactions(self.logs,include_optional=False)
        self.assertEqual(len(first['missingRequiredTraces']),1)
        second = self.collector(10_200).transactions(self.logs,include_optional=False)
        self.assertTrue(second['requiredEvidenceComplete'])
        self.assertEqual(self.store.get('checkpoints/snapshot.json'),self.snap)
        self.assertEqual(sum(m=='eth_getTransactionReceipt' for m,p in self.calls),4)
        self.assertEqual(sum(m=='eth_getTransactionByHash' for m,p in self.calls),4)

    def test_initial_preflight_also_requires_successful_receipt_for_trace(self):
        self.inv.update(start=21737760)
        self.inv['release']['upgradeBlock']=21737760
        self.plan['scopes'][0]['label']='test'
        self.store.put('checkpoints/plan.json',self.plan)
        def unavailable(method,params):
            if method=='eth_chainId':return '0x688'
            if method=='eth_getTransactionReceipt':return None
            if method=='eth_call':return '0x'+abi.enc_word(0)
            if method=='eth_getCode':return '0x'
            if method=='eth_getLogs':return []
            return self.transport(method,params)
        c=self.collector(transport=unavailable)
        with patch('tools.reconcile.collect.scopes',return_value=self.plan['scopes']):
            result=c.preflight()
        self.assertEqual(result['capabilities']['trace'],'UNVERIFIED_RECEIPT')
        self.assertFalse(any(m=='debug_traceTransaction' for m,p in self.calls))

    def test_default_budget_and_critical_traces_precede_optional(self):
        c=self.collector()
        self.assertEqual((c.rpc.run_budget,c.rpc.budget),(100_000,5_000_000))
        c.transactions(self.logs)
        methods=[m for m,p in self.calls]
        self.assertEqual(methods[:8],['eth_getTransactionReceipt','eth_getTransactionByHash']*4)
        traces=[p[0] for m,p in self.calls if m=='debug_traceTransaction']
        self.assertEqual(traces[:2],[self.logs[i]['transactionHash'] for i in (1,3)])

    def test_partial_receipt_detail_pair_resumes_without_redownload(self):
        with self.assertRaises(BudgetExceeded): self.collector(100).transactions(self.logs)
        self.calls.clear()
        self.collector(800).transactions(self.logs,max_traces=0)
        self.assertEqual(sum(m=='eth_getTransactionReceipt' for m,p in self.calls),3)
        self.assertEqual(sum(m=='eth_getTransactionByHash' for m,p in self.calls),4)

    def test_receipt_cap_and_provider_failure_prevent_traces(self):
        coverage=self.collector().transactions(self.logs,max_receipts=1)
        self.assertEqual(len(coverage['missingReceipts']),3)
        self.assertFalse(any(m=='debug_traceTransaction' for m,p in self.calls))
        def unavailable(m,p):
            if m=='eth_getTransactionReceipt':raise RpcError('unsupported')
            return self.transport(m,p)
        coverage=self.collector(transport=unavailable).transactions(self.logs)
        self.assertFalse(coverage['requiredEvidenceComplete'])
        self.assertFalse(any(m=='debug_traceTransaction' for m,p in self.calls))

    def test_unsuccessful_or_wrong_block_receipt_fails_closed(self):
        for override in ({'status':'0x0'}, {'blockHash':'wrong'}):
            with self.subTest(override=override):
                def bad(m,p):
                    value=self.transport(m,p)
                    if m=='eth_getTransactionReceipt':value.update(override)
                    return value
                # Use a distinct transaction so an unsuccessful cached receipt remains preserved.
                rows=[self.logs[0 if 'status' in override else 2]]
                with self.assertRaises(EvidenceError):self.collector(transport=bad).transactions(rows)
        self.assertFalse(any(m=='debug_traceTransaction' for m,p in self.calls))

    def test_orphan_reward_trace_never_certifies_case_without_receipt(self):
        row=self.logs[1];tx=row['transactionHash']
        self.store.put('cache/traces/'+tx+'.json',{'type':'CALL'})
        self.store.put('cache/snapshots/reward-evidence.json',{'snapshot':self.snap,'cases':{
            tx:{'blockHash':row['blockHash'],'complete':True,'trace':'AVAILABLE','balances':{'preserved':1}}},'balanceReads':{}})
        original=self.store.get('cache/snapshots/reward-evidence.json')['cases']
        result=rewards.collect_evidence(self.collector(),self.snap,self.reward_events())
        self.assertFalse(result['requiredEvidenceComplete'])
        self.assertIn(tx,result['missingRequiredCases'])
        self.assertEqual(original,result['cases'])
        self.assertFalse(any(m in ('eth_getBalance','debug_traceTransaction') for m,p in self.calls))

    def test_reward_trace_requires_verified_successful_receipt(self):
        row=self.logs[1];tx=row['transactionHash']
        self.store.put('cache/receipts/'+tx+'.json',{'status':'0x1','blockHash':'wrong'})
        with self.assertRaises(EvidenceError):
            rewards.collect_evidence(self.collector(),self.snap,self.reward_events())
        self.assertFalse(any(m=='debug_traceTransaction' for m,p in self.calls))

    def test_optional_trace_defers_and_keeps_final_anchor_budget(self):
        c=self.collector(20_940)
        c.transactions(self.logs,include_optional=False)
        result=rewards.collect_evidence(c,self.snap,[],max_new_traces=0)
        self.assertTrue(result['requiredEvidenceComplete'])
        c.collect_transaction_traces()
        self.assertEqual(sum(m=='debug_traceTransaction' for m,p in self.calls),2)
        c.check_snapshot(self.snap)
        c.check_snapshot(self.snap)
        self.assertLessEqual(c.rpc.run_usage()['estimatedCU'],20_940)

    def test_cumulative_ceiling_defers_trace_without_reset(self):
        c=self.collector(cumulative=2_961_000)
        coverage=c.transactions(self.logs)
        self.assertEqual(coverage['missingReceipts'],[])
        self.assertEqual(len(coverage['missingRequiredTraces']),2)
        self.assertEqual(c.rpc.ledger['estimatedCU'],2_960_800)

    def test_offline_gate_detects_missing_receipts_and_failed_traces(self):
        c=self.collector();c.transactions(self.logs,max_traces=0)
        tx=self.logs[1]['transactionHash']
        self.store.put('cache/traces/'+tx+'.json',{'type':'CALL','error':'failed'})
        gate=transaction_readiness(self.store,self.inv,self.logs,self.snap,self.previous['number'])
        self.assertFalse(gate['requiredEvidenceComplete'])
        self.assertIn(tx,gate['missingRequiredTraces'])

    def test_reward_balance_cases_resume_without_more_traces(self):
        c=self.collector();c.transactions(self.logs,include_optional=False)
        before=sum(m=='debug_traceTransaction' for m,p in self.calls)
        first=rewards.collect_evidence(c,self.snap,self.reward_events(),max_cases=1,max_new_traces=0)
        self.assertFalse(first['requiredEvidenceComplete'])
        second=rewards.collect_evidence(c,self.snap,self.reward_events(),max_cases=1,max_new_traces=0)
        self.assertTrue(second['requiredEvidenceComplete'])
        self.assertEqual(sum(m=='debug_traceTransaction' for m,p in self.calls),before)


class PendingReportTests(unittest.TestCase):
    def test_offline_incomplete_pending_report_cannot_replace_previous_snapshot(self):
        from .__main__ import main, output_dir
        from .storage import read
        with tempfile.TemporaryDirectory() as temp:
            store=Store(temp);old=snapshot(21647956);pending=snapshot(21737768)
            old_plan={'snapshot':old,'scopes':[]}
            store.put('checkpoints/snapshot.json',old)
            store.put('checkpoints/plan.json',old_plan)
            store.put('cache/snapshots/headers.json',{hex(old['number']):header(old['number'])})
            folder=incremental.report_dir(store,old)
            prior={'snapshot':old,'collectionComplete':True,'executionBlocker':None,'checks':[],'replay':{}}
            write(folder/'RECONCILIATION-RESULTS.json',prior)
            incremental.seal(store,old_plan,prior,folder)
            previous_bytes=(folder/'RECONCILIATION-RESULTS.json').read_bytes()
            inv=copy.deepcopy(test_inventory());vault=next(a for a,t in inv['targets'].items() if 'RewardsVault' in t['name'])
            row=log(pending['number'],address=vault)
            row.update(topics=[abi.event_topic('EthDeposited(address,uint256)'), '0x'+abi.enc_word(vault)],data='0x'+abi.enc_word(1))
            plan={'snapshot':pending,'historicalBoundary':incremental.identity(old),
                  'scopes':[{'id':'pending','start':pending['number'],'filter':{'address':vault}}]}
            store.put('checkpoints/snapshot.json',pending);store.put('checkpoints/plan.json',plan)
            store.put('cache/snapshots/inventory.json',inv)
            store.put('cache/snapshots/headers.json',{hex(n):header(n) for n in (old['number'],pending['number'])})
            store.chunk('pending',pending['number'],pending['number'],pending,[row],{str(pending['number']):pending['hash']})
            store.put('cache/traces/'+row['transactionHash']+'.json',{'type':'CALL'})
            store.put('checkpoints/collection-finished.json',{'snapshot':pending,'complete':True})
            with patch('tools.reconcile.__main__.Rpc',side_effect=AssertionError('No live RPC')),patch('builtins.print'):
                self.assertEqual(main(['reconcile','--data-dir',temp]),2)
            result=read(output_dir(store,plan)/'RECONCILIATION-RESULTS.json')
            self.assertFalse(result['requiredEvidenceComplete'])
            self.assertEqual(result['traceCount'],0)
            self.assertEqual(next(c['status'] for c in result['checks'] if c['id']=='incremental-transaction-evidence'),'UNVERIFIED')
            self.assertFalse(incremental.report_dir(store,pending).exists())
            self.assertEqual(store.get('checkpoints/snapshot.json'),pending)
            self.assertEqual(previous_bytes,(folder/'RECONCILIATION-RESULTS.json').read_bytes())
            with self.assertRaises(EvidenceError):incremental.commit_snapshot(store,plan,result,output_dir(store,plan))


class PendingCacheIntegrationTests(unittest.TestCase):
    def test_existing_pending_snapshot_receipts_progress_under_default_budget(self):
        """Run the public CLI on a disposable cache copy, never the owner's cache."""
        import shutil
        from pathlib import Path
        from .storage import read, digest
        from .collect import cached_logs
        from .__main__ import main
        origin=Path('/Volumes/project/tmp/stpros-mainnet-reconciliation')
        plan_path=origin/'checkpoints/plan.json'
        if not plan_path.exists() or read(plan_path)['payload']['snapshot']['number'] != 21737768:
            self.skipTest('the specified pending-snapshot fixture is not available')
        protected=['checkpoints/plan.json','checkpoints/snapshot.json','checkpoints/rpc-usage.json',
                   'output/reconciliation/RECONCILIATION-RESULTS.json']
        original={p:digest((origin/p).read_bytes()) for p in protected}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for rel in ('cache','checkpoints','output/reconciliation'):
                shutil.copytree(origin/rel,root/rel)
            store=Store(root);plan=store.get('checkpoints/plan.json')
            boundary=incremental.load_boundary(store)
            logs,_,complete=cached_logs(store,plan)
            self.assertTrue(complete)
            grouped={}
            for row in logs:grouped.setdefault(row['transactionHash'].lower(),[]).append(row)
            pending={tx for tx,rows in grouped.items() if int(rows[0]['blockNumber'],16)>boundary['snapshot']['number']}
            prior_report=(root/boundary['result']).read_bytes()
            calls=[]
            def transport(method,params):
                calls.append(method)
                if method=='eth_chainId':return '0x688'
                if method=='eth_getBlockByNumber':
                    return store.get('cache/snapshots/headers.json',{}).get(params[0],header(int(params[0],16)))
                if method=='eth_getTransactionReceipt':
                    rows=grouped[params[0]]
                    return {'status':'0x1','blockHash':rows[0]['blockHash'],'blockNumber':rows[0]['blockNumber'],'logs':rows}
                if method=='eth_getTransactionByHash':
                    row=grouped[params[0]][0]
                    return {'blockHash':row['blockHash'],'to':row['address'],'from':'0x'+'99'*20,'input':'0x'}
                if method=='debug_traceTransaction':
                    for tx in pending:
                        self.assertIsNotNone(store.get('cache/receipts/'+tx+'.json'))
                        self.assertIsNotNone(store.get('cache/receipts/'+tx+'-transaction.json'))
                    return {'type':'CALL','from':'0x'+'99'*20,'to':grouped[params[0]][0]['address'],'input':'0x','output':'0x','value':'0x0'}
                if method=='eth_getCode':return '0x'
                if method=='eth_getStorageAt':return '0x'+'00'*32
                if method=='eth_getBalance':return '0x0'
                if method=='eth_call':raise RpcError('unsupported')
                raise AssertionError('Historical log download or snapshot advance: '+method)
            runs=[]
            def client(s,budget,interval,**kwargs):
                rpc=Rpc(s,budget,0,transport=transport,**kwargs);runs.append(rpc);return rpc
            with patch('tools.reconcile.__main__.Rpc',side_effect=client),patch('builtins.print'):
                for _ in range(4):
                    code=main(['all','--data-dir',temp])
                    self.assertEqual(sum(store.get('cache/receipts/'+tx+'-transaction.json') is not None for tx in pending),len(pending))
                    if code==0:break
            self.assertEqual(code,0,store.get('checkpoints/execution.json'))
            self.assertTrue(all(r.run_usage()['estimatedCU']<=100_000 for r in runs))
            self.assertLessEqual(store.get('checkpoints/rpc-usage.json')['estimatedCU'],5_000_000)
            self.assertNotIn('eth_getLogs',calls)
            self.assertEqual(store.get('checkpoints/snapshot.json'),plan['snapshot'])
            self.assertEqual(prior_report,(root/boundary['result']).read_bytes())
            result=read(incremental.report_dir(store,plan['snapshot'])/'RECONCILIATION-RESULTS.json')
            self.assertTrue(result['requiredEvidenceComplete'])
            self.assertEqual(result['rewardProvenance']['status'],'UNVERIFIED')
        self.assertEqual(original,{p:digest((origin/p).read_bytes()) for p in protected})


if __name__=='__main__':
    unittest.main()
