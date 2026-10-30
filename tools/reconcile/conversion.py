"""Path-specific rounding checks using successful call evidence, never guessed ERC4626 defaults."""
from tools.lib.keccak import selector
from .replay import finding

METHODS = {selector(s): (kind, direction) for s, kind, direction in [
    ('deposit(uint256,address)', 'Deposit', 'assets-floor'),
    ('depositWithPROS()', 'Deposit', 'assets-floor'),
    ('mint(uint256,address)', 'Deposit', 'shares-ceil'),
    ('withdraw(uint256,address,address)', 'Withdraw', 'assets-ceil'),
    ('redeem(uint256,address,address)', 'Withdraw', 'shares-floor')]}


def calls(trace, st):
    output = []
    def visit(node):
        if node.get('error'):
            return
        if node.get('type', '').upper() == 'CALL' and (node.get('to') or '').lower() == st:
            sig = node.get('input', '')[:10]
            if sig in METHODS:
                output.append((METHODS[sig], node))
        for child in node.get('calls', []):
            visit(child)
    visit(trace)
    return output


def check(inv, conversions, transactions, traces, qualified):
    st = inv['addresses']['proxies']['StPROS'].lower()
    grouped = {}
    for c in conversions:
        grouped.setdefault(c['transaction'], []).append(c)
    checked, missing, failures = 0, [], []
    for tx, events in grouped.items():
        if tx in traces:
            paths = calls(traces[tx], st)
        else:
            tr = transactions.get(tx, {})
            paths = calls(dict(tr, type='CALL'), st)
        if len(paths) != len(events):
            missing.append(tx)
            continue
        for c, ((kind, direction), call) in zip(events, paths):
            if kind != c['kind']:
                missing.append(tx)
                continue
            t, v = c['preT'], c['preV']
            if not t or not v:
                t = v = 1
            if direction.startswith('assets'):
                numerator, denominator, actual = c['assets'] * v, t, c['shares']
            else:
                numerator, denominator, actual = c['shares'] * t, v, c['assets']
            expected = (numerator + denominator - 1) // denominator if direction.endswith('ceil') else numerator // denominator
            if expected != actual:
                failures.append(finding('conversion-rounding-' + tx + '-' + c['logIndex'],
                                        'FAIL' if qualified else 'UNVERIFIED',
                                        'Actual accepted conversion differs from source-specific call-path rounding.',
                                        expected, actual, [tx, c], contract=st))
            checked += 1
    status = 'UNVERIFIED'
    if qualified and not missing:
        status = 'FAIL' if failures else 'PASS'
    return {'status': status, 'checked': checked, 'missingTransactions': sorted(set(missing)),
            'failures': failures}
