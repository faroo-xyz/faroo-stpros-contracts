"""Event-to-snapshot configuration reconciliation, distinct from authorization judgment."""
from tools.lib import abi
from .inventory import ZERO, number
from .rpc import RpcError
from .replay import finding

CONFIG = {
    'OwnershipTransferred': ('owner()', 'newOwner', 'address'),
    'GuardianChanged': ('guardian()', 'newGuardian', 'address'),
    'KeeperChanged': ('keeper()', 'newKeeper', 'address'),
    'BridgeVaultChanged': ('bridgeVault()', 'newBridgeVault', 'address'),
    'SlpChanged': ('slp()', 'newSlp', 'address'),
    'OracleChanged': ('oracle()', 'newOracle', 'address'),
    'BridgeVaultSet': ('getBridgeVault()', 'bridgeVault', 'address'),
    'SlpPharosProxySet': ('getSlpPharosProxy()', 'slpPharosProxy', 'address'),
    'MaxAprPpmChanged': ('maxAprPpm()', 'maxAprPpm', 'uint256'),
    'MinDelayChange': ('getMinDelay()', 'newDuration', 'uint256'),
    'UnbondingPeriodChanged': ('unbondingPeriod()', 'newUnbondingPeriod', 'uint256'),
    'MaxWithdrawCountChanged': ('maxWithdrawCount()', 'maxWithdrawCount', 'uint256')}


def observe(collector, snap, replay_result):
    out = collector.store.get('cache/snapshots/governance-state.json', {})
    if out.get('snapshot') != snap:
        out = {'snapshot': snap, 'reads': {}, 'errors': {}}
    desired = {}
    for e in replay_result['governance']:
        if e['name'] in CONFIG:
            sig, field, kind = CONFIG[e['name']]
            if field in e['args']:
                desired[e['address'] + ':' + sig] = (e['address'], sig, [], kind, e['args'][field], e['raw'])
        elif e['name'] in ('CommissionConfigChanged', 'UpdateLimitsChanged'):
            fields = [('commissionAccount()', 'account', 'address'), ('commissionRatePpm()', 'ratePpm', 'uint256')] if e['name'] == 'CommissionConfigChanged' else [('maxUpdateAmount()', 'maxUpdateAmount', 'uint256'), ('updateInterval()', 'updateInterval', 'uint256')]
            for sig, field, kind in fields:
                desired[e['address'] + ':' + sig] = (e['address'], sig, [], kind, e['args'][field], e['raw'])
        elif e['name'] in ('Paused', 'Unpaused'):
            desired[e['address'] + ':paused()'] = (e['address'], 'paused()', [], 'bool', e['name'] == 'Paused', e['raw'])
        elif e['name'] == 'CommissionVTokenSet':
            a = e['args']; sig = 'tokenToVToken(address)'
            desired[e['address'] + ':' + sig + ':' + a['token']] = (e['address'], sig, [a['token']], 'address', a['vToken'], e['raw'])
        elif e['name'] == 'VTokenSet':
            a = e['args']; sig = 'vTokenAddresses(address)'
            desired[e['address'] + ':' + sig + ':' + a['vTokenAddress']] = (e['address'], sig, [a['vTokenAddress']], 'bool', a['enabled'], e['raw'])
        elif e['name'] in ('RoleGranted', 'RoleRevoked'):
            a = e['args']
            key = e['address'] + ':role:' + a['role'] + ':' + a['account']
            desired[key] = (e['address'], 'hasRole(bytes32,address)', [a['role'], a['account']], 'bool',
                            e['name'] == 'RoleGranted', e['raw'])
        elif e['name'] == 'SlpWhitelistUpdated':
            a = e['args']; key = e['address'] + ':whitelist:' + a['slp']
            desired[key] = (e['address'], 'isSlp(address)', [a['slp']], 'bool', a['allowed'], e['raw'])
    for key, (addr, sig, args, kind, expected, evidence) in desired.items():
        if key in out['reads']:
            continue
        try:
            raw = collector.call(addr, sig, args, snap['number'])
            actual = abi.decode_parameters([{'type': kind}], raw)[0]
            actual = actual.lower() if isinstance(actual, str) else actual
            out['reads'][key] = {'expected': expected, 'actual': actual, 'evidence': evidence, 'contract': addr}
        except (RpcError, ValueError):
            out['errors'][key] = 'UNAVAILABLE'
        collector.store.put('cache/snapshots/governance-state.json', out)
    return out


def checks(observations, complete, verified_contracts=()):
    rows = []
    for key, obs in sorted(observations.get('reads', {}).items()):
        status = ('PASS' if obs['expected'] == obs['actual'] else 'FAIL') if complete and obs['contract'] in verified_contracts else 'UNVERIFIED'
        rows.append(finding('configuration-' + key, status,
                            'Last observed event compared with current getter; does not independently prove authorization.',
                            obs['expected'], obs['actual'], [obs['evidence']], 'P1', obs['contract']))
    return rows


def historical_admin_findings(inv, events, external, snapshot):
    """Observed historical control windows, without inventing an authorization violation."""
    if not snapshot:
        return []
    cutover = inv['release']['upgradeBlock']
    timelock = inv['addresses']['governance']['timelock'].lower()
    admins = {row['admin'] for row in external.get('proxySlots', {}).values() if row.get('admin') and row['admin'] != ZERO}
    findings = []
    for admin in sorted(admins):
        history = [e for e in events if e['address'] == admin and e['name'] == 'OwnershipTransferred']
        windows = []
        for i, event in enumerate(history):
            next_event = history[i+1] if i+1 < len(history) else None
            end = number(next_event['raw']['blockNumber']) if next_event else snapshot['number'] + 1
            start = number(event['raw']['blockNumber'])
            if event['args']['newOwner'] != timelock and end > cutover and start <= snapshot['number']:
                windows.append({'owner': event['args']['newOwner'], 'fromBlock': max(start, cutover),
                    'untilOwnershipEvent': next_event['raw'] if next_event else None,
                    'untilBlockExclusiveApproximation': min(end, snapshot['number'] + 1),
                    'startingOwnershipEvent': event['raw'],
                    'note': 'Exact ownership boundaries are the referenced transaction/log positions; block interval is a summary.'})
        if windows:
            proxies = {a for a, row in external.get('proxySlots', {}).items() if row.get('admin') == admin}
            upgrades = [e['raw'] for e in events if e['address'] in proxies and e['name'] == 'Upgraded'
                        and any(w['fromBlock'] <= number(e['raw']['blockNumber']) < w['untilBlockExclusiveApproximation'] for w in windows)]
            row = finding('historical-external-upgrade-control-' + admin, 'UNVERIFIED',
                'External proxy upgrade authority remained outside Timelock after core V4 activation. Verify the approved historical exception and release disclosures. Current Timelock ownership does not erase this window; no unauthorized action is inferred.',
                expected='approved external-authority transition inventory',
                actual={'windows': windows, 'extraUpgradesObservedInsideWindows': upgrades,
                        'currentOwnerFromEvents': history[-1]['args']['newOwner']},
                evidence=[e['raw'] for e in history], priority='P1', contract=admin)
            row['confidence'] = 'HIGH observed event history; authorization UNVERIFIED'
            findings.append(row)
    return findings
