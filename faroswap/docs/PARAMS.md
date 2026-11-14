# Confirmed FaroSwap deployment parameters and their evidence

Everything below is the frozen requirement baseline for this module. A change to any value is a
reviewed decision recorded in the OpenSpec change, not a local edit.

## Network and identities (Pharos Mainnet, chain 1672)

| Item | Value | Evidence |
|---|---|---|
| stPROS vault (base token AND rate source) | `0x6b0a44c64190279f7034b77c13a566e914fe5ec4` | approved spec BR-3; `asset() == WPROS` verified on-chain |
| WPROS (quote) | `0x52c48d4213107b20bc583832b0d951fb9ca8f0b0` | approved spec |
| Oracle (cross-check) | `0x051713fd66845a13bf23baca008c5c22c27ccb58` | `getTokenAmountByVToken(WPROS,1e18,Floor)` equals `convertToAssets(1e18)` exactly at every checked block |
| Governance Safe | `0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca` | approved spec |
| Deployer / bootstrap funder | `0x8ce84e9fa0101d317d8956d73610ad3e0e219d41` | approved spec; holds no PM ownership or upgrade authority |
| GSPFactory | `0xd15f2d50bbf777efd529052686d0703e498f8380` | discovered via deployer `0xc6f4…3a0c` nonce scan (nonce 92); owner/maintainer = FaroSwap `0x4f36cdd2…ae7a`; behavior verified on-chain and exercised on a fork |
| GSP template | `0x81b04c62c53a716b7b5ce7fe8de42ab665bca935` | `version() == "GSP 1.0.1"`; runtime keccak256 `0xb0bf3be474fb6a7bdfe8c2ee8e7d5f98fd46a44153f8c7b5aa8b8a86ce7cd3e1` — **byte-identical** to the pinned source compile (Faroswap/contracts@`41fefb37…` + OZ 4.9.3, solc 0.8.16, london, optimizer 200) |
| FaroSwap maintainer (pool maintainer, not us) | `0x4f36cdd2180b2f9b17745d8f7ff380440262ae7a` | factory `_DEFAULT_MAINTAINER_`; forced into every pool at creation |
| Reference private pool (fee/k baseline source) | `0x5b8f17cbd707a27bfb1b7c44bd96c7ef3298c4bd` | spec BR-10 baseline read at block 22731868 |

The GSPFactory's exact-source compile (solc 0.6.9) matches the deployed runtime in size
(4506 B) and behavior but not byte-identically (build-config drift); the byte-identical
template — which is the code pools actually execute — is the load-bearing provenance result.

## Pool creation parameters (factory `createDODOGasSavingPool`)

| Parameter | Value | Source |
|---|---|---|
| admin | Price Manager **proxy** address | spec BR-7; never Safe/deployer/implementation |
| base / quote | stPROS / WPROS | confirmed direction |
| lpFeeRate | `500000000000000` (0.05%) | private-pool baseline (block 22731868) |
| mtFeeRate | `0` | private-pool baseline (effective value via model) |
| k | `2000000000000000` (0.002) | private-pool baseline |
| i (initial) | live `stPROS.convertToAssets(1e18)` at creation, re-read immediately before signing | task §6; e.g. `1009531657654669604` at block 22918977, unchanged at 22921747 |
| priceLimit | `10000` (~1% single-update) | confirmed |
| isOpenTWAP | `false` | confirmed |

Sample rate history: `1008246000000000000` (baked into pre-existing FaroSwap test pools) →
`1009531657654669604` (2026-11-13 reads). Never hardcode; always re-read.

## Price limit rationale

GSP enforces `floor(|newI − oldI| · 1e6 / oldI) <= priceLimit` per `adjustPrice` call — a
per-update relative bound, not a daily or deviation-from-redemption bound. 10000 ≈ 1% per sync
keeps any single executor-triggered move inside one oracle reporting interval's plausible
drift while capping the blast radius of a compromised executor to a 1% step; recovery from a
longer interruption (accumulated drift) is a Safe decision: temporarily raise the limit, sync,
restore. The executor can neither read nor relax the limit.

## Bootstrap budget (fixed maximum)

Base `0.01e18` stPROS; quote `floor(0.01e18 · rate / 1e18)`; funded by the deployer; LP 100%
to the Safe (`0.01e18 − 1001` shares after the GSP first-mint lock). Atomic via
`FaroSwapPoolBootstrap`; approvals are exact-amount and consumed in the same transaction.
This 0.01 ceiling is a confirmed budget — never increase it to make a failure disappear.

## Executor authorization

`setExecutor(address, bool)` on the manager, Safe-only, emits `ExecutorUpdated`; verification
is `isExecutor(address)`. The deployment account never receives this permission implicitly.
