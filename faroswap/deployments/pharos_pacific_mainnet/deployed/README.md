# FaroSwap Deployed Mainnet Archive (Pharos Mainnet, chain 1672)

This directory is the **authoritative deployment archive** of the FaroSwap GSP delivery:
the canonical address registry (`addresses.json`), the RPC-receipt-verified transaction
list (`transactions.json`), and the backend-integration ABIs (`abi/`). The historical
unsigned plan in `../prepared/` is process evidence only and is NOT an address source.

## Addresses and roles (quick reference)

| Role | Address |
|---|---|
| **Price Manager operational address (ERC1967 proxy)** | `0x7732fca644a733490ee90d648196bed8bf5871a3` |
| Price Manager implementation | `0x167989ad7d2547f4477d53f01d4316b155ab4531` |
| GSP public pool (stPROS/WPROS) | `0xa4b6f4dbe9c9d2036bb51ca27768aeefabcb4942` |
| GSPFactory | `0xd15f2d50bbf777efd529052686d0703e498f8380` |
| GSP 1.0.1 template (the pool is its EIP-1167 clone) | `0x81b04c62c53a716b7b5ce7fe8de42ab665bca935` |
| Bootstrap helper (one-shot, already executed; historical) | `0x9d4c4455b12c23efe7169b317ed8af582a441dda` |
| stPROS / WPROS / Oracle | `0x6b0a44c64190279f7034b77c13a566e914fe5ec4` / `0x52c48d4213107b20bc583832b0d951fb9ca8f0b0` / `0x051713fd66845a13bf23baca008c5c22c27ccb58` |
| Governance Safe (business owner + upgrade authority + LP recipient) | `0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca` |
| Sync executor (syncPrice() trigger permission only) | `0x677933905a31f13228e026d204f75f50d461b4d5` |
| Deployment/funding account (no Price Manager authority) | `0x8ce84e9fa0101d317d8956d73610ad3e0e219d41` |

## Deployment provenance

- Frozen production source commit: **`dc8fbd8a8f3a1f5e3d1fa09a0d84aaf9c67a9a6d`**
  (cross-checked against Git history; the faroswap contracts are byte-identical to that commit).
- Transaction and final-state evidence: research repository
  `faroo-contracts-reserch/faroswap/release-gate-20261116/` — machine-readable
  `deployment-mainnet-manifest.json`, human-readable `MAINNET-DEPLOYMENT-RECORD.md`, and the
  per-step d1/d2/d3/d4d9-prep preparation and fork-rehearsal evidence.
- D1–D9 were sent by the deployment account (nonces 45–53); S1/S2 executed through the
  governance Safe via 3-of-5 `execTransaction`.

## Bytecode-identity verification ≠ public explorer source verification (important distinction)

- **Proven (independently verified by this repository):** the implementation and bootstrap
  runtimes are byte-identical to the frozen build after immutable masking (24 and 37 immutable
  sites, all carrying constructor values or `__self`); the proxy runtime is byte-identical to
  the frozen vendored OZ 5.6.1 ERC1967Proxy; the pool is a 45-byte clone of the verified
  template; the template runtime keccak `0xb0bf3be4…cd3e1` is byte-identical to the pinned
  public-source compile (Faroswap/contracts@41fefb37 + OZ 4.9.3 + solc 0.8.16 london).
- **Public source publication (a separate matter):** on SocialScan the implementation shows
  unverified, the other addresses were inconclusive (UNKNOWN) at last check; Sourcify supports
  chain 1672 but has no entries. Publishing sources is a pending owner action and is
  independent of the bytecode-identity proofs above.

## ABI notes (`abi/`)

| File | Source | Nature |
|---|---|---|
| `FaroSwapPriceManager.json` | compiled from frozen commit dc8fbd8 sources (solc 0.8.28, cancun) | **full compiler ABI** (66 entries incl. all custom errors/events). Use this ABI when calling the proxy address |
| `FaroSwapPoolBootstrap.json` | same | full compiler ABI (19 entries). The contract has already executed once; kept for decoding historical events |
| `GSP.json` | compiled from pinned Faroswap/contracts@41fefb37 sources (solc 0.8.16) | **full compiler ABI** (74 entries, same source and version as the deployed template; not an interface subset) |
| `GSPFactory.json` | same (solc 0.6.9) | full compiler ABI (23 entries). Note `NewGSP` has all four parameters **non-indexed**, carried entirely in event data |
| stPROS | reuses `deployments/mainnet/v4/StPROS.json` (123 entries, the authoritative V4 record) | NOT duplicated here, to avoid inconsistent copies |

Event indexing quick reference: `ExecutorUpdated(executor indexed, allowed)`;
`PoolBound(pool indexed, initialI)`; `PriceSynced(oldI, newI both non-indexed)`;
`PriceLimitUpdated(newLimit)`; `Upgraded(implementation indexed)`;
`Bootstrapped(pool, lpRecipient indexed; shares, baseInput, quoteInput)`; GSP `IChange(newI)`;
`DODOSwap(fromToken,toToken,fromAmount,toAmount,trader,receiver all non-indexed)`;
GSP ERC20 `Transfer(from,to indexed; amount)` / `Approval(owner,spender indexed; amount)`;
factory `NewGSP(all params in data)`.

## Backend integration

See `../../../docs/BACKEND-INTEGRATION.md` for the integration guide (network configuration,
PM/GSP interface semantics, event-indexing start blocks, error handling and TypeScript examples).
