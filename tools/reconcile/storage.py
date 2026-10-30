"""Atomic, checksummed evidence storage. No cache cleanup or implicit reset."""
import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path
from tools.lib.common import is_secret_like


class EvidenceError(Exception):
    pass


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe(path):
    path = Path(path)
    if is_secret_like(str(path)):
        raise EvidenceError('secret-like path refused')
    return path


def atomic(path, data):
    path = safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)  # Only this operation's own temporary file.


def read(path):
    return json.loads(safe(path).read_bytes())


def write(path, value):
    atomic(path, canonical(value))


class Store:
    def __init__(self, root):
        self.root = safe(root).resolve()
        for p in ('cache/events', 'cache/receipts', 'cache/traces', 'cache/snapshots',
                  'cache/manifests', 'checkpoints', 'output/reconciliation',
                  'output/anomalies', 'output/rpc-usage'):
            (self.root / p).mkdir(parents=True, exist_ok=True)

    def archive_snapshot(self, snapshot):
        """Retain prior snapshot evidence/reports on advance; raw chunks stay shared."""
        folder = self.root / 'cache/snapshots/history' / digest(canonical(snapshot))
        sources = ['checkpoints/plan.json', 'checkpoints/snapshot.json', 'checkpoints/transaction-coverage.json',
                   'cache/snapshots/inventory.json', 'cache/snapshots/state.json', 'cache/snapshots/identities.json',
                   'cache/snapshots/external-state.json', 'cache/snapshots/governance-state.json',
                   'cache/snapshots/fixed-code-identities.json', 'cache/snapshots/reward-evidence.json',
                   'cache/snapshots/external-implementations.json', 'cache/snapshots/final-anchor-verification.json',
                   'output/reconciliation/RECONCILIATION-REPORT.md', 'output/reconciliation/RECONCILIATION-RESULTS.json',
                   'output/reconciliation/DATA-MANIFEST.json', 'output/reconciliation/RPC-USAGE.json',
                   'output/reconciliation/ANOMALIES.csv']
        index = read(folder / 'index.json') if (folder / 'index.json').exists() else {'snapshot': snapshot, 'files': {}}
        for relative in sources:
            source = safe(self.root / relative)
            if source.exists():
                data = source.read_bytes()
                checksum = digest(data)
                name = relative.replace('/', '__') + '.' + checksum
                if not (folder / name).exists():
                    atomic(folder / name, data)
                index['files'][name] = {'origin': relative, 'sha256': checksum}
        write(folder / 'index.json', index)

    def put(self, relative, value):
        payload = canonical(value)
        write(self.root / relative, {'sha256': digest(payload), 'payload': value})

    def get(self, relative, default=None):
        p = self.root / relative
        if not p.exists():
            return default
        try:
            obj = read(p)
            if digest(canonical(obj['payload'])) != obj['sha256']:
                raise EvidenceError('cache checksum mismatch')
            return obj['payload']
        except (ValueError, KeyError, OSError):
            raise EvidenceError('invalid cache envelope') from None

    def chunk(self, scope, start, end, snapshot, logs, headers):
        name = f'{scope}-{start:012d}-{end:012d}'
        data = gzip.compress(canonical(logs), mtime=0)
        rel = f'cache/events/{name}.json.gz'
        atomic(self.root / rel, data)
        meta = dict(chainId=1672, scope=scope, startBlock=start, endBlock=end,
                    snapshot=snapshot, count=len(logs), sha256=digest(data), file=rel,
                    headers=headers, status='COMPLETE')
        # Manifest publication is the commit point; orphan payloads are ignored.
        self.put(f'cache/manifests/{name}.json', meta)
        return meta

    def chunks(self, scope):
        result = []
        for p in sorted((self.root / 'cache/manifests').glob(f'{scope}-*.json')):
            m = self.get(str(p.relative_to(self.root)))
            if m['scope'] != scope or m['chainId'] != 1672 or m['status'] != 'COMPLETE':
                raise EvidenceError('chunk identity mismatch')
            rel = Path(m['file'])
            if rel.is_absolute() or '..' in rel.parts or rel.parent != Path('cache/events'):
                raise EvidenceError('invalid chunk path')
            try:
                data = safe(self.root / rel).read_bytes()
                if digest(data) != m['sha256']:
                    raise EvidenceError('chunk checksum mismatch')
                logs = json.loads(gzip.decompress(data))
            except (OSError, ValueError, EOFError):
                raise EvidenceError('invalid compressed chunk') from None
            if len(logs) != m['count']:
                raise EvidenceError('chunk count mismatch')
            result.append((m, logs))
        for (a, _), (b, _) in zip(result, result[1:]):
            if a['endBlock'] >= b['startBlock']:
                raise EvidenceError('overlapping intervals')
        return result


def gaps(intervals, start, end):
    cursor = start
    for lo, hi in sorted(intervals):
        if hi < start or lo > end:
            continue
        if lo < cursor:
            raise EvidenceError('overlapping coverage')
        if lo > cursor:
            yield cursor, lo - 1
        cursor = hi + 1
    if cursor <= end:
        yield cursor, end


def ordered(logs, allow_identical=False):
    unique = {}
    block_log_owners = {}
    for log in logs:
        key = (int(log['blockNumber'], 16), int(log['transactionIndex'], 16), int(log['logIndex'], 16))
        block_key = (key[0], key[2])
        owner = (key[1], log['transactionHash'], log['blockHash'])
        if block_key in block_log_owners and block_log_owners[block_key] != owner:
            raise EvidenceError('conflicting block-global log index')
        block_log_owners[block_key] = owner
        if log.get('removed'):
            raise EvidenceError('removed log')
        if key in unique:
            if not allow_identical or unique[key] != log:
                raise EvidenceError('duplicate or conflicting log')
        unique[key] = log
    return [unique[k] for k in sorted(unique)]
