# SLP deployed-source module

This module maintains the two deployed Pharos SLP contract types without changing their behavior or on-chain state. It is independent of the frozen stPROS core compiler inputs and OpenZeppelin 5.6.1 vendor closure.

- Production logic: Solidity 0.8.28, OpenZeppelin 5.3.0, optimizer 200, **paris**.
- Core stPROS remains **cancun** with its existing 13 protected artifact identities.
- `DEPENDENCIES.json` pins the four original project sources and nineteen required OZ source files, their virtual source-unit names, hashes and upstream commits.
- `deployments/PROVENANCE.json` identifies three distinct proxies and implementations and thirteen append-only historical records. Constructor administrators are historical facts; they are not the current role baseline.
- `build-reference.json` pins compiler bytes, exact original Standard JSON inputs, complete target compiler outputs, selectors, and five namespaces per target. It has no automatic refresh command.

The canonical builder reads source bytes from this module's disk files, reconstructs the original `contracts/pharos/...` and `@openzeppelin/...` virtual paths, and checks equality with the preserved original solcInputs. It compiles each target separately with no import callback and an empty environment. No runtime read from the vETH or Research repositories, package manager, compiler download, network call, signing or transaction preparation is used.

## Build in a safe export

Run the existing repository exporter/runner. Never run a compiler or Foundry from the production checkout. Supply the already-installed compiler whose SHA256 is pinned in `build-reference.json`; an executable claiming the correct version is insufficient.

```sh
dest=$(tools/verify/export.sh | tail -1)
tools/verify/run.sh "$dest" python3 -B slp/tools/build.py --solc '/absolute/path/to/approved/solc-0.8.28'
tools/verify/run.sh "$dest" python3 -B tools/ci/canonical.py
```

The currently reviewed native binary is `0.8.28+commit.7893614a.Darwin.appleclang`, SHA256 `81515b0e53deaa266d549545ccaac0a5a96e6d4e8201c77f673b2c710976d9ea`. Other platforms need a separately reviewed compiler identity; there is no fallback or download.

Outputs are written only inside the export at `slp/out-native/<contract>/`: exact `input.json`, `output.json`, canonical `artifact.json`, `build-info.json`, and separate storage-probe input/output. `out-native/manifest.json` is written only after every check succeeds. A failed build clears an older success manifest; old artifact files alone never establish a completed build. Storage probes are analysis artifacts, not deployable contract candidates.

Full runtime bytes, metadata, ABI, selectors, creation bytecode and compiler output fields must match the historical artifacts. Namespace evidence covers both the business struct and inherited Initializable, AccessControl, AccessControlEnumerable and ReentrancyGuard structs, including nested mappings/sets and packed offsets. Empty ordinary `storageLayout.storage` does not mean these contracts have no storage.

| Contract type | Full runtime Keccak256 |
|---|---|
| SlpPharosProxy | `0x48686658ca6bf1f01e3f47ddadc34c1531b4104c2c5ed12ca785fcf235fc0aa3` |
| SlpPharosRewardsVault | `0x08c89ff66bb36f6ca361f89c06a45cbd79b480e5c5201b3cc93a4833318c50eb` |

Both SLP instances share those implementation bytes, but their proxy and implementation addresses remain distinct. No metadata stripping, immutable masking or state merging is allowed.

## Historical ABI and proxy boundary

The implementation records contain the canonical business ABI. Original Hardhat-deploy aggregate records combine proxy and implementation entries, replace the constructor, and the SLP aggregate omits the no-argument `InsufficientBalance()` overload. Keep those original record bytes unchanged; do not use an aggregate ABI as the canonical implementation ABI. The native artifact preserves both overloads and checks all business functions/events against the aggregate.

The separate SLP ProxyAdmin is `0x480eb384681777bf21e72b8c8538987c2801b4b7`. Its original hardhat-deploy OZ 4.x proxy records were compiled with Solidity 0.8.10. They are preserved and hash-checked, not rebuilt using the 0.8.28/OZ5 logic profile. They must not be routed through core V4 release assertions or migration calldata.

Phase 1 observed at block 22633551 that the three administrators and ProxyAdmin owner were Timelock `0xc30db0543412c6881a543390d2c21fbbff42805b`; the sole operator was Keeper `0x8736e68f37c1156e09445ac2d395a139e58fcab4`. These are dated carried observations, not fresh RPC results from this offline build.

## Test scope and remaining integration

`tests/test_build.py` exercises deterministic full builds and rejection of changed/missing sources, dependencies, compiler inputs, compiler bytes, configuration, deployment identities, namespace offsets and selectors. Run inside the export:

```sh
tools/verify/run.sh "$dest" python3 -B -m unittest discover -s slp/tests -p test_build.py -v
```

The Python tests locate only installed SVM candidates whose bytes match the approved compiler hash.
`foundry.toml` remains frozen; test-only remappings are provided by `tools/check.sh`.
The original 76 Solidity tests are in `tests/`, with a separate hashed test dependency
closure. They use MockStaking/MockOracle and ERC1967Proxy, not a mainnet fork.
Foundry artifacts are never canonical release references.

From the repository root, `make slp-build`, `make slp-test`, and `make slp-verify`
create fresh exports and run the independent gates. `slp-verify` also runs the
reconciliation, native build, core artifact and toolchain regressions. It preserves
failure exit codes; it does not refresh references. See `docs/TOOLCHAIN.md`.

Reconciliation defaults to the local manifest-backed artifact provider. Original Git
origins remain historical facts, not commits claimed to exist in this repository.
The fixed historical snapshot and current incremental runtime/upgrade guards remain
separate. Current mainnet fork tests are opt-in in `fork-tests/`; see `docs/SLP-STAKING.md` and the
Batch 3 evidence archive for the authenticated pin and execution qualifications. The original 76 mocks remain separate.

The deployed two-step reward flow, principal/donation mixing risk, full-balance RewardsVault reporting and five historical UNVERIFIED economic/governance findings are unchanged. Build parity is not an audit or upgrade approval.
