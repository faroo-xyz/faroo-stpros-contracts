# FaroSwap Backend Integration Guide (Pharos Mainnet)

Audience: backend engineers integrating the FaroSwap GSP public pool (stPROS/WPROS) and the
FaroSwap Price Manager. All addresses below are live mainnet identities; the authoritative
machine-readable registry is `deployments/pharos_pacific_mainnet/deployed/addresses.json`.

## A. Network and contract configuration

- **Chain**: Pharos Mainnet, `chainId = 1672`. Public read-only RPC: `https://rpc.pharos.xyz`
  (rejects `eth_getLogs` ranges above 1,000 blocks; rate-limited per IP — batch accordingly).
- **Address registry** (excerpt; full version in `deployed/addresses.json`):

| Role | Address |
|---|---|
| Price Manager **proxy** (operational — call this) | `0x7732fcA644A733490Ee90d648196bED8BF5871a3` |
| Price Manager implementation (do not call directly) | `0x167989aD7d2547F4477d53f01D4316b155ab4531` |
| GSP pool (base=stPROS, quote=WPROS) | `0xa4b6F4DbE9C9d2036Bb51CA27768AEefabCb4942` |
| GSPFactory | `0xD15f2D50bbf777EFD529052686d0703e498F8380` |
| stPROS (ERC-4626 vault, 18 decimals) | `0x6b0A44c64190279f7034b77c13a566E914FE5Ec4` |
| WPROS (wrapped native, 18 decimals) | `0x52C48d4213107b20bC583832b0d951FB9CA8F0B0` |
| Oracle (redemption-rate source) | `0x051713fD66845a13BF23BACa008C5C22C27Ccb58` |
| Governance Safe (owner/upgrades/LP recipient) | `0x3658e00f5DDb9Fa0c7e9820e8d16C3C17eaB73CA` |
| Sync executor | `0x677933905A31f13228e026d204f75f50D461B4D5` |

- **Proxy vs implementation**: the proxy is a standard ERC-1967 proxy with zero admin. All
  business calls — reads and writes — go to the **proxy** using the implementation ABI
  (`deployed/abi/FaroSwapPriceManager.json`). The implementation address only matters for
  upgrade verification (slot `0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc`).
- **ABI locations**: `faroswap/deployments/pharos_pacific_mainnet/deployed/abi/`
  (`FaroSwapPriceManager.json`, `GSP.json`, `GSPFactory.json`, `FaroSwapPoolBootstrap.json`);
  stPROS ABI: `deployments/mainnet/v4/StPROS.json` (authoritative V4 record, 123 entries).
- **Event-indexing start blocks** (deployment blocks; scan from these, not from genesis):
  - Price Manager proxy events: **23,194,410** (D2, proxy creation)
  - `ExecutorUpdated`: 23,195,126 (S1)
  - `PoolBound`: 23,196,596 (S2)
  - GSP pool (`IChange`, `DODOSwap`, `BuyShares`, `SellShares`, LP `Transfer`): **23,195,832** (D3)
  - Factory `NewGSP`: any range covering 23,195,832 (our pool); the factory predates it
  - `Bootstrapped`: 23,197,739 (D9, one-shot — no further events expected)

## B. Price Manager integration (proxy address, implementation ABI)

Read-only (anyone):

- `owner() → address` — governance Safe.
- `pool() → address`, `poolBound() → bool` — the bound GSP pool; `false` only pre-binding.
- `isExecutor(address) → bool` — whether an account may call `syncPrice()`.
- `targetI() → uint256` — the current target price: `stPROS.convertToAssets(1e18)` (WPROS per
  1 stPROS share, floored, 18 decimals). Read-only derivation of the same value sync uses.
- `previewSync() → (uint256 newTargetI, uint256 currentI, bool needed)` — scheduler helper;
  `needed == false` means a broadcast `syncPrice()` would be a no-op this block.

Executor-only (reverts `NotExecutor(address)` otherwise):

- `syncPrice()` — **no price parameter exists**. The contract reads
  `stPROS.convertToAssets(1e18)` itself and calls the pool's `adjustPrice(target)`. If the
  target equals the pool `_I_()`, the call returns successfully **without** writing, calling
  the pool, or emitting `PriceSynced`. If the relative change exceeds the pool price limit,
  the **transaction reverts** (`EXCEED_PRICE_LIMIT` from the pool) — there is no automatic
  threshold relaxation; only the Safe can `setPriceLimit` and retry.

Safe-only (reverts `OwnableUnauthorizedAccount(address)` otherwise):

- `setExecutor(address,bool)` — grant/revoke the sync trigger permission.
- `setPriceLimit(uint256)` — 0..1,000,000 (1e6 precision; 10,000 ≈ 1% per update).
- `upgradeToAndCall(address,bytes)` — proxy upgrade; candidates must expose ERC-1822
  `proxiableUUID()` returning the ERC-1967 slot (incompatible candidates revert
  `UnsupportedProxiableUUID`). Executors hold no upgrade authority.

## C. GSP pool integration (pool address, `abi/GSP.json`)

All amounts are 18-decimal uints; `i` is quote-per-base scaled 1e18; fee rates and `k` are
scaled 1e18 (LP fee `5e14` = 0.05%, MT fee `0`, k `2e15`); `priceLimit` is 1e6-scaled
(`10,000` ≈ 1% maximum relative change per `adjustPrice`).

Views: `_ADMIN_()` (the Price Manager proxy), `_MAINTAINER_()` (FaroSwap
`0x4F36CdD2…Ae7A` — NOT us), `_BASE_TOKEN_()`, `_QUOTE_TOKEN_()`, `_I_()`, `_K_()`,
`_LP_FEE_RATE_()`, `_MT_FEE_RATE_()`, `_PRICE_LIMIT_()`, `getVaultReserve()`,
`totalSupply()`, `balanceOf(address)`, `getPMMState()` (adjustedTarget view).

Swaps: `sellBase(to)` (pay stPROS, receive WPROS) and `sellQuote(to)` (pay WPROS, receive
stPROS) consume tokens already transferred to the pool; both return the payout and emit
`DODOSwap`. LP: `buyShares(to)` consumes both tokens already in the pool; exit via
`sellShares(shares, to, minBase, minQuote, "", deadline)`. The pool's LP token implements
`transfer`/`approve`/`balanceOf`/`totalSupply` (first-mint locked 1001 shares at `address(0)`).

**Atomicity requirement (production):** these functions pull nothing and take no allowance —
the pool absorbs the token-balance delta since the last reserve sync. The token transfer(s)
and the `sellBase`/`sellQuote`/`buyShares` call must therefore execute in **one transaction**
(e.g. a minimal router doing `transferFrom` + pool call, as the deployment's bootstrap helper
demonstrates). Separate unprotected transfer transactions leave the funds claimable by anyone
and are not production-safe.

Read-only quotes (both `view`):
`querySellBase(address trader, uint256 payBaseAmount) returns (uint256 receiveQuoteAmount,
uint256 mtFee, uint8 newRState, uint256 newBaseTarget)` — preview of selling stPROS for WPROS;
`querySellQuote(address trader, uint256 payQuoteAmount) returns (uint256 receiveBaseAmount,
uint256 mtFee, uint8 newRState, uint256 newQuoteTarget)` — preview of selling WPROS for stPROS.
The fourth outputs (`newBaseTarget` / `newQuoteTarget`) are the post-trade **target inventory
values** the pool's PMM math would settle at — they are NOT a new oracle price (`_I_` is
unchanged by trades).

Role distinction: the factory `_USER_REGISTRY_` creator record (D3: the deployer EOA) is a
registry annotation only; the pool `_ADMIN_` (price + priceLimit) is the Price Manager proxy;
the `_MAINTAINER_` (fees/k/MT-fee withdrawal) is FaroSwap's default maintainer and is not
controlled by this project.

## D. Events and indexing

| Contract | Event (full signature) | Indexed | Meaning | Start block |
|---|---|---|---|---|
| PM proxy | `ExecutorUpdated(address indexed executor, bool allowed)` | executor | sync permission granted/revoked | 23195126 |
| PM proxy | `PoolBound(address indexed pool, uint256 initialI)` | pool | one-time binding done | 23196596 |
| PM proxy | `PriceSynced(uint256 oldI, uint256 newI)` | — | a real price update (no-op syncs emit nothing) | 23197739+ (none yet) |
| PM proxy | `PriceLimitUpdated(uint256 newLimit)` | — | Safe changed the pool limit | none yet |
| PM proxy | `OwnershipTransferStarted(address indexed previousOwner, address indexed newOwner)` / `OwnershipTransferred(…)` | both | two-step ownership lifecycle | — |
| PM proxy | `Upgraded(address indexed implementation)` | implementation | proxy upgrade (23194410 = initial) | 23194410 |
| GSP pool | `IChange(uint256 newI)` | — | emitted by `adjustPrice()` only (i.e. Price-Manager syncs); GSP `init()` assigns `_I_` directly and does **not** emit this event | 23195832 |
| GSP pool | `DODOSwap(address fromToken, address toToken, uint256 fromAmount, uint256 toAmount, address trader, address receiver)` | — | every swap (both directions) | 23195832 |
| GSP pool | `BuyShares(address to, uint256 increaseShares, uint256 totalShares)` | — | LP mint | 23195832 |
| GSP pool | `SellShares(address payer, address to, uint256 decreaseShares, uint256 totalShares)` | — | LP burn | 23195832 |
| GSP pool | `Transfer(address indexed from, address indexed to, uint256 amount)` / `Approval(address indexed owner, address indexed spender, uint256 amount)` | from,to / owner,spender | LP-token ERC20 events | 23195832 |
| GSPFactory | `NewGSP(address baseToken, address quoteToken, address creator, address GSP)` | **none — all four params are in event data** | pool creation record | covers 23195832 |
| Bootstrap | `Bootstrapped(address indexed pool, address indexed lpRecipient, uint256 shares, uint256 baseInput, uint256 quoteInput)` | pool, lpRecipient | one-shot seed completed | 23197739 (terminal) |

Operational guidance: poll with ≥1-confirmation depth and re-check after reorgs (re-read state
rather than trusting a stored log when a block hash changes); key events by
`(blockNumber, logIndex, txHash)` to keep deduplication idempotent. The public RPC caps
`eth_getLogs` at 1,000 blocks per call.

## E. Error handling and integration constraints

| Condition | On-chain behavior | Backend handling |
|---|---|---|
| Unauthorized `syncPrice` | custom error `NotExecutor(address)` | check `isExecutor()` before sending |
| Sync before binding | `PoolNotBound()` | binding is done (since 23196596); treat as invariant break |
| Safe-only calls by others | `OwnableUnauthorizedAccount(address)` (OZ) | never route admin calls from service keys |
| Price move beyond pool limit | pool reverts `EXCEED_PRICE_LIMIT` (string revert); nothing partial persists | surface for manual Safe decision (`setPriceLimit` then retry); **no auto-relaxation exists** |
| Invalid source rate | `InvalidTargetI(uint256)` (zero or > 1e36) | alarm — indicates stPROS/oracle anomaly |
| Upgrade to incompatible impl | `UnsupportedProxiableUUID(address)` / `UUPSUnauthorizedCallContext()` (direct-call or wrong context) | verify `proxiableUUID()` before proposing upgrades |
| RPC failure / unknown tx status | — | retry by hash idempotently; all PM writes are one-shot state changes, never auto-rebroadcast blind |
| Proxy upgrade | address unchanged, ABI may extend | re-export ABI, re-run integration tests; `poolBound`/executor state persists by design |

## F. TypeScript / ethers examples

```ts
import { readFileSync } from "fs";
import { ethers } from "ethers";

const provider  = new ethers.JsonRpcProvider("https://rpc.pharos.xyz");

// 1) load addresses + ABIs from the deployed archive (single source of truth;
//    addresses.json stores lowercase, which ethers accepts as-is)
const addresses = JSON.parse(readFileSync("faroswap/deployments/pharos_pacific_mainnet/deployed/addresses.json", "utf8"));
const pmAbi  = JSON.parse(readFileSync("faroswap/deployments/pharos_pacific_mainnet/deployed/abi/FaroSwapPriceManager.json", "utf8"));
const gspAbi = JSON.parse(readFileSync("faroswap/deployments/pharos_pacific_mainnet/deployed/abi/GSP.json", "utf8"));
const PM_PROXY = addresses.priceManager.operational;   // the ERC1967 proxy
const GSP_POOL = addresses.pool.gspPool;
const pm  = new ethers.Contract(PM_PROXY, pmAbi, provider);   // implementation ABI at proxy address
const gsp = new ethers.Contract(GSP_POOL, gspAbi, provider);

// 2) scheduler pre-check: only broadcast when needed
// the compiler ABI names the returns (newTargetI, currentI, needed):
const { newTargetI, currentI, needed } = await pm.previewSync();
if (!needed) { /* skip: a sync would be an on-chain no-op */ }

// 3) read pool parameters
const [i, k, lpFee, limit] = await Promise.all([gsp._I_(), gsp._K_(), gsp._LP_FEE_RATE_(), gsp._PRICE_LIMIT_()]);
const [baseReserve, quoteReserve] = await gsp.getVaultReserve();

// 4) parse PriceSynced (topic0 0xb47d671b96a3d6a057b251e2219ffe6a56aa1f0afdfc3515642530ead332ca3d)
//    and DODOSwap. Historical scans MUST batch: the public RPC rejects eth_getLogs ranges
//    above 1,000 blocks per request. ethers v6 queryFilter returns (EventLog | Log)[]:
//    only EventLog entries carry a decoded .args — narrow before accessing.
async function queryBatched(contract: ethers.Contract, event: ethers.ContractEventName,
                            fromBlock: number, toBlock: number): Promise<(ethers.EventLog | ethers.Log)[]> {
  const STEP = 1000; // max eth_getLogs range on rpc.pharos.xyz
  const out: (ethers.EventLog | ethers.Log)[] = [];
  for (let start = fromBlock; start <= toBlock; start += STEP) {
    const end = Math.min(start + STEP - 1, toBlock);
    out.push(...(await contract.queryFilter(event, start, end)));
  }
  return out;
}
const syncedLogs = await queryBatched(pm, "PriceSynced", 23197739, await provider.getBlockNumber());
for (const log of syncedLogs) {
  if (!(log instanceof ethers.EventLog)) continue; // not decoded for this ABI
  console.log(log.args.oldI, "->", log.args.newI);
}

// parse DODOSwap on the pool (all fields non-indexed, in data)
const swaps = await queryBatched(gsp, "DODOSwap", 23195832, await provider.getBlockNumber());
for (const sw of swaps) {
  if (!(sw instanceof ethers.EventLog)) continue;
  console.log(sw.args.fromToken, sw.args.fromAmount, "->", sw.args.toAmount);
}
```

(Example only; no secrets are involved; signer setup is out of scope.)

## G. Deployment status and outstanding work

- **Deployed and bound**: implementation, proxy, GSP pool, one-time binding — all
  receipt-verified (see `deployed/transactions.json`).
- **Bootstrap completed**: the Safe holds exactly `9999999999998999` LP; residual approvals
  and bootstrap holdings are zero.
- **Deployer holds no Price Manager authority** (ownership, executors and upgrades are
  Safe-controlled; the deployer funded and created only).
- **Public source publication is pending** and is independent of the bytecode-identity
  proofs documented in `deployed/README.md`.
- **First authorized on-chain sync (`PriceSynced`) is still pending** — no confirmed
  transaction evidence exists yet; indexers must not assume it happened.
- Out of scope for this handoff: monitoring/scheduling implementation, the old DPP liquidity
  migration (a separate manual operation; a fork simulation exists in the research repo), and
  Faroo DApp GSP routing (a separate frontend deliverable).
