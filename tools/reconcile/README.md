# stPROS mainnet reconciliation

Read-only mainnet evidence collector and conservative V1/V4 accounting replay.
Uses Python standard library and existing `tools/lib` ABI/Keccak code. Does not
invoke Foundry, Hardhat, signing services, deployment tools or backend databases.
No production contract, deployment record or protected snapshot is modified.

From the production repository:

```sh
python3 -m tools.reconcile plan
python3 -m tools.reconcile all
python3 -m tools.reconcile all --advance
python3 -m tools.reconcile reconcile
python3 -m tools.reconcile status
python3 -m unittest tools.reconcile.test_reconcile tools.reconcile.test_closeout tools.reconcile.test_incremental -v
```

`PHAROS_RPC_URL` is normally inherited. During the recorded audit, the owner authorized the narrow
`--rpc-from-project-env` exception: it extracts only that literal
variable from this project's `.env`, without shell execution, expansion or output.
It is ignored for offline commands; neither default execution nor offline replay
loads any environment file. No endpoint is accepted as a CLI argument or written
to logs, reports, exception messages or cache.

```sh
python3 -m tools.reconcile all --advance --rpc-from-project-env
python3 -m tools.reconcile reconcile
```

Data lives at `/Volumes/project/tmp/stpros-mainnet-reconciliation/`. `--data-dir`
selects an alternative outside production. The cache is never automatically reset
or deleted. One process owns it through an advisory lock. Source and ABI identities
are recorded; reports identify the replay source hashes as well as repository HEAD.

`plan` makes a bounded chain/finality/archive/log/receipt/trace preflight and pins
a snapshot. `sync` fills only uncovered intervals. `all` also reads snapshot,
migration, external staking/roles, implementation identities and selected transaction
evidence. `reconcile` never constructs an RPC client and regenerates reports offline.
`--advance` pins a later finalized block, or resumes the pending snapshot after interruption;
the previous finalized hash and new interval anchors must remain canonical. Reorganizations stop the run and preserve evidence for manual
investigation. Use a separate data directory to audit a different canonical history.

Defaults: 5,000,000 **cumulative estimated CU** for this cache and 100,000
**per-invocation estimated CU**, with 0.5 seconds/request. Log queries are bounded
to 1,000 blocks, including previously learned larger limits; smaller learned limits
are retained. The legacy `--chunk` option is accepted but capped at 1,000 at runtime. At most ten read requests share an HTTP
batch; every item, failed request and retry is charged independently. Traces are
sequential, with a conservative estimate of 10,000 CU each. Weights are in `rpc.py`;
they are internal estimates, not Alchemy's actual invoice. `--budget-cu` may lower the
ceiling but cannot raise it above 5,000,000. `--run-budget-cu` explicitly sets the
independent per-invocation ceiling (default 100,000); neither ceiling increases automatically.
Default per-run caps are 2,000 new receipt/transaction pairs
and 40 new traces; cached evidence does not consume these caps. `--max-receipts`, `--max-traces`, `--request-interval`, `--chunk` and
`--fallback-depth` are configurable. No automatic budget reset exists.

Chunks are deterministic gzip, with atomic checksummed manifests including empty
intervals. Manifest publication commits a chunk; orphan temporary output is ignored.
Duplicate/conflicting logs, missing ranges, removed logs, receipt inconsistencies and
changed block hashes prevent qualified conclusions. Cross-filter WPROS overlap is
intentionally deduplicated by exact raw log identity. Provider completeness itself
is a trust assumption; receipt checks only detect omissions in discovered transactions.

Replay uses integer arithmetic and ordered `(block, transaction, log)` boundaries.
V1's burn/remint escrow, aggregate completion and four-field queues are distinct from
V4's immediate burn, per-record payout and receiver binding. Migration burns are
separate transitions, with block-boundary state and targeted native trace evidence.
Oracle reward dilution uses `floor(fee * V / (T + reward - fee))`, with ppm fees,
historical caps, cooldown, APR bounds and zero-supply handling.

Original audit outputs under `output/reconciliation/` are retained unchanged. New
attempts and completed snapshots use the subdirectories described below, with the same files:

- `RECONCILIATION-REPORT.md`, `RECONCILIATION-RESULTS.json`
- `ANOMALIES.csv`, `RPC-USAGE.json`, `DATA-MANIFEST.json`

PASS applies only to the stated check with sufficient evidence. Collection success
is not accounting correctness. Incomplete history is UNVERIFIED, an unavailable
prerequisite is BLOCKED, and a qualified contradiction is FAIL. Empty replay totals
are not observed on-chain zero balances.

The original inventory uses provisional external source hints. The completed fixed-snapshot
P0 closeout below authenticates the mainnet SLP and RewardsVault bytecode/source mapping
against the supplied historical deployment artifacts. The underlying Pharos Staking
implementation and reward-generation semantics remain UNVERIFIED; its public interface
is a provisional adapter, not deployment attestation. Staking `principalStake` includes
pending principal; `pendingWithdrawStake` overlaps pending-undelegation records.
Client-minted rewards may be represented by `CoinMinted` rather than an EVM value call.
No solvency certification follows from these unverified positions or custody balances.
Targeted traces cannot establish unsolicited native flows in unrelated transactions.
Call-path conversion rounding and economic authorization of governance corrections
remain evidence-dependent. Full historical native provenance is not implemented as
a chain-wide indexer. Unknown versions/events are preserved, never interpreted with
a silently substituted ABI.

## Fixed-snapshot P0 evidence closeout

```sh
python3 -m tools.reconcile closeout --slp-repo /Volumes/project/workspace/vETH-3.0-contract
python3 -m unittest tools.reconcile.test_closeout -v
```

This mode is strictly offline and fixed to block 21647956 and its original hash.
It reads the external source repository without modifying it, authenticates complete
recorded runtime bytes, runtime-embedded metadata CID, ABI, literal source closure,
compiler settings and historical Git artifacts, and reuses the existing replay.
It requires the preserved first-round outputs under
`output/reconciliation/baseline-before-p0-closeout/`; it never overwrites them or the
original outputs. New results go to `output/reconciliation/p0-closeout/`.
`--advance` and `--rpc-from-project-env` are rejected in this mode. Precisely scoped
additional evidence, when authorized, is stored separately with request justifications;
re-running closeout adds no RPC requests. Existing core V1/V4 replay is compared
byte-for-byte with the preserved result before statuses can be updated.

A recorded-build correspondence PASS is not a fresh compilation or a solvency verdict.
A verified native reward credit/forwarding path is distinct from authentication of the
underlying Staking reward formula. The closeout retains those distinctions and reports
stale off-chain administrator assertions separately from financial discrepancies.

## Daily/weekly incremental reconciliation

```sh
python3 -m tools.reconcile status
python3 -m tools.reconcile all --advance
python3 -m tools.reconcile reconcile
```

The first advance validates the completed block **21647956** report, its published
manifest, cached headers, full interval coverage and checksums, then seals that
historical boundary. Each existing filter starts at **21647957**, or at its next
uncovered block after an interrupted collection. A pending snapshot is completed
before a repeated `all --advance` can select another one. A newly discovered routing
address still needs a bounded backfill; already verified intervals are never downloaded
again. Raw events, receipts, traces and the original P0 closeout remain shared and intact.

Every online run checks chainId, the previous finalized block hash, the new finalized
snapshot and the first new block's parent. New event-block and interval-end hashes,
including empty intervals, are checked; resumed new intervals are checked freshly.
Previously sealed historical anchors are checked against local checksums and header
records without thousands of historical RPC reads. Canonical hash-by-number and the
provider's finalized tag remain explicit RPC trust assumptions; this is not a light
client or independent consensus proof. Incremental mode refuses a missing finalized
tag instead of falling back to a probabilistic depth. Reorganizations, changed sealed
files, gaps in sealed history and conflicting events fail closed.

Historical migration receipt/trace and archive-call probes, the original log sample,
completed reward balance probes, known historical implementation checks, and the
WPROS audit-start code read are reused. Snapshot-dependent runtime, proxy/admin slots,
routing, role, custody and staking observations are still refreshed. New transactions
are collected within the receipt/trace and CU caps; missing historical evidence is
not silently upgraded or repeatedly enriched. Replay still evaluates the complete
ordered V1/V4 event history offline; parsed chunks are reused within an invocation.

The immutable SLP source/ABI closure comes from
`cache/snapshots/p0-closeout-attestation.json`. Every new snapshot compares current
proxy runtime, implementation slots and full implementation runtime hashes and examines
all subsequent upgrade events before installing that ABI. A changed/unknown version,
beacon or intervening upgrade blocks ABI reuse (even an upgrade away and back).
Supporting additional verified versions requires a reviewed historical ABI mapping;
there is no automatic source re-attestation. Routing getters are compared with
initialization and ordered configuration events; roles and authority are separate
checks. Reward provenance and underlying Staking asset backing remain **UNVERIFIED**.

New completed reports live at:

`output/reconciliation/snapshots/<block>-<full-block-hash>/`

The usual Markdown/JSON/CSV outputs are joined by `INCREMENTAL-SUMMARY.json`, containing
snapshot hashes/ranges, new events/transactions, exact integer accounting deltas and
18-decimal displays, custody/staking observations, findings introduced/resolved/changed,
and run/cumulative CU. Exchange-rate deltas are exact fractions with a presentation rounded down
to 18 decimal places. stPROS shares and PROS assets retain distinct units. All stake
amounts and reward origin remain provisional; cash observations do not establish
attributable backing or economic solvency. A completed *collection* can contain FAIL,
BLOCKED or UNVERIFIED findings; it does not certify the protocol.

`output/reconciliation/attempts/<block>-<full-block-hash>/` holds incomplete/interrupted
attempts and offline regeneration. A completed directory is never overwritten.
Preparation seals the report and evidence before atomic directory publication, so
interruption can be recovered without changing a completed report. Finalized history
seals live in `checkpoints/finalized/`; `completed-snapshot.json` selects the latest.
Raw evidence is not duplicated in Research Git. The original fixed-snapshot closeout
and first-round reports are not rewritten.

`status` separates cumulative usage from the most recent online invocation. The
historical ledger is never reset. Each attempt (including retries and failed requests)
is charged before sending; a batch reserves its full estimated cost before transmission.
The run checkpoint and `output/rpc-usage/runs/<run-id>.json` retain usage per invocation.
Reaching either ceiling preserves published chunks/checkpoints and writes an incomplete
attempt; rerun the same command to resume under a new run allowance. The cumulative
5,000,000-CU cap still applies across every invocation. Estimates are not provider bills.

Development acceptance uses synthetic transports and an isolated copy of the existing
cache when present. No live mainnet advance, Foundry, Hardhat or full CI is required.
