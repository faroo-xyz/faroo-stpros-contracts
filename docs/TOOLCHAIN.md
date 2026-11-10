# Native toolchain

This document is for maintainers: how the repository is laid out, how sources are built, how commands run in
isolation, and which baselines are protected.

## Repository layout

| Path | Purpose |
| --- | --- |
| `contracts/` | Protocol contracts, access control, migration guard and atomic release assertions |
| `lib/` | Exact vendored dependency import closure, identities and licenses (`lib/DEPENDENCIES.json`) |
| `deployments/` | Append-only deployment records: mainnet V1 (top level), mainnet V4 (`mainnet/v4/`, current), testnet; index `registry.json` ([README](../deployments/README.md)) |
| `snapshots/` | Reviewed historical ABI, storage, runtime, state and gas baselines |
| `tools/ci/` | Native compiler and the 13-target source and artifact gate |
| `tools/lib/` | ABI / Keccak, strict JSON identity, read-only RPC and bytecode helpers |
| `tools/release/native/` | Unsigned upgrade plans, history and economic checkpoints, LOSS preparation |
| `tools/release/` | Registry, recorded-bytecode reconstruction, interfaces, provenance and log archives |
| `tools/snapshot/` | Reproducible historical snapshot generation and comparison |
| `tools/verify/` | Safe export, isolated execution and explicit historical checks |
| `tests/fixtures/legacy/` | Authoritative recorded legacy bytecode for migration tests |
| `tests/fixtures/native-foundation/`, `tests/fixtures/native-release/`, `tests/fixtures/upgrade-reference/` | Independent ABI / Keccak witnesses, upgrade-plan and LOSS values, recorded upgrade state and Timelock runtime witnesses |
| `tests/helpers/`, `tests/mocks/` | Shared protocol test base, legacy topology and boundary test doubles |
| `tests/release/`, `tests/protocol/` | Upgrade, recovery, custody, governance and policy regressions |
| `tests/toolchain/` | Compiler drift, primitive and isolation controls |
| `tests/legacy/`, `tests/security/`, `tests/invariant/` | Historical behaviour, security regressions and accounting properties |
| `tests/fork/`, `tests/gas/` | Explicitly gated chain-feasibility and gas baselines; not default commands |

Root contract tests protect business behaviour during tooling changes. Retired framework and application sources and
complete reference harnesses are kept in the research repository.

## Build identity

`make build` exports the current safe source tree and runs `tools/ci/canonical.py`
through the empty-environment runner. Python's standard library, reviewed native
solc and vendored Solidity sources are the only build dependencies.

Solidity 0.8.28 (`7893614a`), Cancun, optimizer 200, non-IR, literal-content IPFS
metadata are frozen. The exact compiler binary SHA-256 is checked before execution.
The current pin is platform-specific; other binaries need independent review.

`lib/DEPENDENCIES.json` binds OpenZeppelin 5.6.1 and forge-std 1.9.4 source bytes
and original licenses. Only the required Solidity import closure is vendored.
Effective compiler source names remain `project/contracts/...` and
`npm/@openzeppelin/<package>@5.6.1/...` despite changed physical paths.

All 13 protected targets preserve original ABI/selectors, storage/namespaces,
creation/runtime bytecode, metadata, link/immutable references and source closures.
Only configuration binding migrated to native files. Research preserves the
unchanged original reference and complete before-state artifacts. Never refresh
protected artifact hashes to obtain a pass.

The build writes per-contract input, output, artifact and build-info JSON plus a
manifest under the export's `cache/native-r4/`. Stale local generated files are
never inputs. No Node or package-manager installation is required.

## Independent SLP module

The exact deployed SLP sources live under `slp/contracts/`. Its OZ 5.3.0
production closure is separate from root OZ 5.6.1 and its test-only dependencies.
The two stale root copies were removed by a verified Batch 1 deletion manifest.
The frozen core inventory and all 13 artifact references remain unchanged.

- `make slp-build`: reproduce both full SLP runtimes, metadata, ABI, selectors,
  storage namespaces and three distinct proxy/implementation bindings.
- `make slp-test`: validate the test closure and run the original 76 mock tests.
- `make slp-verify`: build, test, native build/tamper regressions, reconciliation
  regressions, export/toolchain tests and the 13-artifact core comparison.

Each target creates an approved export and uses the existing clean-environment
runner. No dependency downloads, RPC or historical checkout are needed. CI can
invoke `make slp-verify`; no hosted CI workflow is present in this repository.
The compiler binary is platform-pinned by the unchanged references; a different
platform requires a separately reviewed compiler identity, not a hash refresh.

The original `slp/foundry.toml` remains frozen. Test-only remappings are passed explicitly
to Foundry and are separate from canonical release compilation. Passing these tests proves mock behavior and
artifact correspondence, not mainnet E2E behavior or external audit approval.
Fixed historical reconciliation retains its original block/hash and confidence
limitations. Current routing/roles and the Batch 3 campaign require separate evidence.

## Commands

Create a fresh export with `tools/verify/export.sh DESTINATION`, using a new directory under `/Volumes/project/tmp`
(without an argument the export goes to the system temporary directory, as the `make` targets do). Execute every command through
`tools/verify/run.sh EXPORT ...`. Exports copy only safe Git-known native paths,
reject symlinks, and exclude ignored local data and platform metadata.

| Command | Purpose |
| --- | --- |
| `python3 tools/ci/canonical.py` | Exact protected artifact compilation |
| `python3 -m tools.release.native artifact normalize ...` | Unapproved candidate artifact |
| `python3 -m tools.release.native release preflight ...` | Bundle admission, deployment definitions and state verification |
| `release authority` | Reviewed role inventory and completeness boundaries |
| `release p1-runbook` | Mainnet Factory_test preparation of the original V4 scope (not used) |
| `release build-handoff` | Atomic mainnet V4 handoff Safe calldata |
| `release build-u` | Exact V4 upgrade operation, operation ID and unsigned cancellation calldata |
| `release testnet-handoff` | Testnet handoff stages and approved recovery |
| `release estimate-gas` | Local individual estimates; never an atomic gas certification |
| `release postcheck` | Fresh identity, authority, version and economic checks |
| `python3 -m tools.release.native loss contingency ...` | Reviewed LOSS alternatives and pinned observations |

Abbreviated `release` rows use the same `python3 -m tools.release.native` prefix.
Use each action's `--help`. Inputs and independent review hashes are explicit.
RPC permits only reviewed public read-only endpoints or explicit local simulations.
Production tooling has no signing, write RPC or transaction-submission surface.

Deployment preparation validates configuration, constructors, candidate artifacts
and immutable-bound runtime. Recorded v4 ProxyAdmin identity is independent of
the current vendored v5 implementation. The `release` actions implement the
executed V4 release (`migrateFromV1`, `migrateFromV3`, `migrateToR4`, `assertR4`);
they are not a general builder for future versions. [UPGRADES](UPGRADES.md#scope-of-the-release-tooling)
describes that scope, the procedure, bundle admission, history evidence and
approval requirements.

## Focused checks

```sh
dest=$(tools/verify/export.sh /Volumes/project/tmp/faroo-export-$(date +%s) | tail -1)
tools/verify/run.sh "$dest" python3 tools/ci/canonical.py
tools/verify/run.sh "$dest" python3 -m unittest discover -s tests/toolchain -p 'test_*.py' -v
tools/verify/run.sh "$dest" python3 tests/release/native/gate.py
tools/verify/run.sh "$dest" python3 -m unittest discover -s tests/release/native -p 'test_*.py' -v
tools/verify/run.sh "$dest" python3 tests/release/rpc-transport.test.py -v
tools/verify/run.sh "$dest" FOUNDRY_PROFILE=final forge test --offline --match-path 'tests/security/*'
```

Foundry discovers tests under `tests/` (`foundry.toml`). Root unit tests cover each
contract; `tests/protocol/` holds the recorded-topology protocol and R4 migration
regressions; `tests/security/` holds the permanent attack and settlement regressions.

Run Release checks sequentially: historical replay uses one owned synthetic
input directory. Selected original Node/viem goldens retain exact record hashes;
complete historical corpora and retired harnesses are archived in Research.
No framework-dependent reference runner remains in production.

Focused Foundry/OZ tests cover proxy, beacon, migration, authority, custody and
recovery properties. Test-only Anvil rehearsals load recorded legacy bytecode
with synthetic actors; no actual Safe signatures or public-chain operations.
Their local transport is separate from the production read-only RPC client.
`make upgrade-test` runs the focused native/codec/LOSS suite;
`make upgrade-rehearsal` builds canonical artifacts, exports complete local
fixtures and runs six loopback-only Anvil cases. The local actor handoff model
is not a Safe signature or public-chain readiness proof.

Historical snapshot tools remain for reproducibility and `--check` comparisons.
Normal build/test does not refresh snapshots. Comparisons against the pre-V4 baseline
snapshots report the intended V4 ABI, storage and bytecode differences as an existing
FAIL. Missing historical creation, namespace and proxy/beacon provenance remains
INCOMPLETE. Real-state forks, actual Safe or explorer checks, broad fuzz or formal
campaigns and production readiness are not established by local checks.

## Repository rules

- Never read, print or commit `.env`, `.env.*`, keys, keystores or credentials.
  Foundry binaries (`forge`, `cast`, `anvil`) load `.env` from their working
  directory and every parent directory at startup, for every subcommand. Run them
  only through `tools/verify/run.sh` inside a fresh export.
- RPC use is read-only, against the public endpoints in `tools/lib/rpc.py`.
  Development never deploys, signs, broadcasts or upgrades; local forks may
  impersonate accounts.
- `deployments/` is append-only. Tests of deployed behavior use the recorded
  bytecode in `tests/fixtures/legacy/` through `tests/helpers/LegacyTopology.sol`,
  never current sources, and `tests/mocks/WPROS9.sol` wherever the native unwrap
  path matters.
- Snapshots are regenerated only by their tools and compared with `--check`. Never
  refresh a snapshot, golden or protected artifact hash, lower a bound, drop a test
  or widen an assumption to obtain a pass. Report unrun checks as NOT RUN and
  incomplete evidence as INCOMPLETE.
- Vendored dependency bytes, compiler source names, metadata and the 13 protected
  artifact identities stay fixed by `tools/ci/r4-reference.json`.
- Cleanup uses an explicit verified manifest; unknown ignored local material is
  never blanket-deleted. Agent-local configuration (`AGENTS.md`, `AGENT.md`,
  `.claude/`) is ignored by Git and never exported.

## Scope

Application Tasks, ordinary views, wallet actions and their unsigned-intent
replacements are retired. The four owner-retired operations have no replacement
CLI. Internal upgrade/LOSS observations remain; Solidity methods and storage are
unchanged. The former framework graph, plugin registrations and privileged routes
are retired together. No execution interface replaces them.

Ignored user-owned research and protected storage are excluded from exports.
Uncertain local material requires a documented provenance decision before cleanup.

## Current SLP fork integration

`slp/fork-tests/CurrentMainnet.t.sol` exercises existing mainnet proxies. It is opt-in and separate from the original
76 mock tests. Run only in an approved export with explicit public RPC, authenticated block number and `SLP_FORK_BLOCK`.
Use `FOUNDRY_TEST=fork-tests`, `--root slp --evm-version cancun` and the test-only forge-std remapping. The Cancun
execution override supports deployed core transient storage; neither frozen compiler profile is changed.
Cases labelled CONTROLLED use test cash, local time or local privileged setup; they never certify native credits.
NativeBoundary diagnostics preserve unsupported native exits/credit boundaries. See the Batch 3 evidence archive
for exact commands and exit codes. Production preparation has no SLP deployment or upgrade route.

The native toolchain regression anchors the reviewed contracts map at commit
`ab46ed7a2a76750e24d2a54cb23b2757d6cb3a85`; the earlier `0625e52` map remains historical provenance.
The independent test pin rejects reference tampering, while canonical compilation checks all 13 artifacts.
