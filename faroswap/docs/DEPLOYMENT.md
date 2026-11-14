# FaroSwap mainnet deployment runbook (preparation only — nothing here broadcasts)

The machine-readable package is regenerated on demand and never hand-edited:

```sh
python3 faroswap/scripts/prepare_deployment.py
# -> faroswap/deployments/pharos_pacific_mainnet/prepared/transactions.json (+ checksums)
```

The preparer re-validates the factory/template pins, reads the redemption rate and every other
state item at ONE recorded block, checks the deployer's latest AND pending nonce plus native /
stPROS / WPROS balances, and derives the predicted CREATE addresses from the COMPLETE ordered
transaction sequence below. **Regenerate it immediately before execution** — any deployer
transaction shifts every later predicted address, and the rate must be re-read right before the
pool creation signature (abort on material drift from the prepared value; there is no
oracle-bound drift threshold on-chain, the decision is the operator's).

Address prediction follows the deployer's own nonce order — Safe transactions interleave but
consume no deployer nonce:

| deployer tx | nonce offset | consumes |
|---|---|---|
| deploy implementation | +0 | CREATE → predicted address |
| deploy proxy | +1 | CREATE → predicted address |
| `createDODOGasSavingPool` | +2 | CALL (the step the original plan missed) |
| deploy bootstrap | **+3** | CREATE → predicted address (needs the pool address) |
| (optional) `WPROS.deposit{value}` + `stPROS.depositWithPROS{value}` | +4/+5 | only when the preparer finds the bootstrap tokens short |
| approve base / approve quote | +6/+7 (or +4/+5) | exact-amount approvals |
| `bootstrap.execute()` | +8 (or +6) | the atomic mint |

Safe steps 3 (`setExecutor`) and 5 (`bindPool`) ship with **explicitly INCOMPLETE calldata
templates** in the plan until the executor address is chosen and the pool exists; the preparer
marks them `calldataStatus: INCOMPLETE …` and only provides `calldataWhenKnown` patterns.

All commands below run inside the approved export only (see AGENTS.md). The executor signs with
their own tooling; this repository has no broadcast path.

## Ordering and transactions

| # | Actor | Action | Gate before proceeding |
|---|---|---|---|
| 1 | deployer | deploy `FaroSwapPriceManager` implementation (8 constructor args = PARAMS.md constants) | code at predicted address; `STPROS_VAULT()` etc. readable |
| 2 | deployer | deploy `ERC1967Proxy(impl, initialize(GOV_SAFE))` | `owner() == Safe`, `poolBound() == false`, `pendingOwner() == 0` |
| 3 | **Safe** | `setExecutor(bot, true)` — the explicit authorization step | `isExecutor(bot) == true`; `ExecutorUpdated` event in the receipt |
| 4 | deployer | `GSPFactory.createDODOGasSavingPool(admin=proxy, base=stPROS, quote=WPROS, 5e14, 0, liveRate, 2e15, 10000, false)` | rate re-read matches the signed tx; new pool is the registry tail; `NewGSP` event |
| 5 | **Safe** | `bindPool(pool)` — one-time, full validation (DESIGN.md) | `pool() == pool`, `poolBound() == true`, `PoolBound` event; a failure here aborts the runbook — do NOT create a second pool and continue |

Pre-binding checklist (off-chain evidence for the Safe signers; the contract independently
re-verifies everything it can on-chain):

1. **Deployment transaction identity** — the pool address comes from OUR creation receipt:
   `NewGSP(base=stPROS, quote=WPROS, creator=deployer, GSP=pool)` event, tx `from == deployer`,
   pool is the registry tail for the pair.
2. **Oracle price consistency** — `floor(|fresh stPROS.convertToAssets(1e18) - pool._I_()| *
   1e6 / pool._I_()) <= 10000`: measured exactly as the pool's own `adjustPrice` will measure
   the first synchronization (old-price denominator); enforced on-chain at bind
   (`BindPriceDeviation` otherwise → abandon the pool and re-create). Every bound pool is
   guaranteed one sync within its limit.
3. **Pool liquidity status** — `pool.totalSupply() == 0` and `pool.balanceOf(anyone) == 0`;
   enforced on-chain (`PoolNotEmpty` → abandon and re-create).
4. **Expected pool parameters** — admin == proxy address, base/quote direction, LP fee 5e14,
   MT fee 0, k 2e15, priceLimit 10000, clone code of the verified template; enforced on-chain.

Between binding (step 5) and the bootstrap execute (step 8), re-check `pool.totalSupply() == 0`
immediately before signing step 8: the bootstrap refuses a seeded pool atomically (no fund
loss), and governance then chooses — seed manually with a reviewed amount/shape, or leave the
pool LP-empty (price synchronization does not depend on LP state) — before anything else moves.
| 6 | deployer | deploy `FaroSwapPoolBootstrap(pool, stPROS, WPROS, deployer, Safe, 0.01e18, quoteAmount)` | quoteAmount recomputed from the rate actually used in step 4 |
| 7 | deployer | approve exact base and quote amounts to the bootstrap (2 txs) | allowances exactly equal the amounts |
| 6a/6b | deployer | (only if short) wrap native → WPROS, `depositWithPROS{value}` → stPROS | balances ≥ bootstrap legs; included automatically when the preparer finds the gap |
| 8 | deployer | `bootstrap.execute()` — atomic mint on a still-EMPTY pool | Safe `balanceOf == 0.01e18 − 1001`; allowances back to 0; `Bootstrapped` event; a pre-seeded or front-run pool reverts `PoolAlreadySeeded` before funds move |
| 9 | bot | `syncPrice()` — first on-chain sync verification | expected clean no-op (pool born at the live rate); stays a no-op until the next oracle yield report moves the rate |

The bootstrap quote leg MUST be `floor(0.01e18 × poolI / 1e18)` using the pool's own `i`
(what it was created with), not a fresh vault read — the contract enforces exact inputs and the
exact first-mint amount. Steps 3 and 5 are Safe transactions (import the calldata into the Safe
UI — both templates are INCOMPLETE until executor/pool are known); everything else is a plain
deployer transaction. Pool creation (step 4) is permissionless — anyone could race a
pool with our admin address, which is harmless: the Safe binds only the address from our own
creation receipt, and `bindPool` independently rejects any pool whose economics deviate.

## Contract verification commands (read-only)

```sh
cast call $PROXY 'owner()(address)'                 # GOV_SAFE
cast call $PROXY 'isExecutor(address)(bool)' $BOT   # true
cast call $PROXY 'pool()(address)'                  # pool
cast call $POOL '_ADMIN_()(address)'                # $PROXY  (never Safe/deployer/impl)
cast call $POOL '_MAINTAINER_()(address)'           # 0x4f36cdd2...ae7a (FaroSwap)
cast call $POOL '_BASE_TOKEN_()(address)'           # stPROS
cast call $POOL '_QUOTE_TOKEN_()(address)'          # WPROS
cast call $POOL '_I_()(uint256)'                    # == stPROS.convertToAssets(1e18)
cast call $POOL '_PRICE_LIMIT_()(uint256)'          # 10000
cast call $POOL '_K_()(uint256)'                    # 2e15
cast call $POOL 'balanceOf(address)(uint256)' $SAFE # 9999999999998999
cast code $POOL                                    # 45-byte EIP-1167 clone of $TEMPLATE
```

Source verification for the two new contracts: publish with the repository's reviewed release
flow (the exported Standard-JSON build), not ad-hoc compiler settings.

## Post-deployment assertions and monitoring

- Every gate in the table above, plus `previewSync()` sanity (`needed == false` immediately).
- First real `PriceSynced` after the next oracle yield report: old/new i must equal the vault
  rate before/after, and the pool's raw `_BASE_TARGET_`/`_QUOTE_TARGET_`/`_RState_`/reserves/k/
  fees must be unchanged (pure-i invariant).
- Scheduler (off-chain, ~4h default, configurable without upgrades): call `previewSync()`; send
  `syncPrice()` only when `needed == true`; alert on `EXCEED_PRICE_LIMIT` (accumulated drift —
  Safe decides whether to raise the limit and re-sync).

## Rollback and recovery limits

- Before `bindPool`: a wrong pool is abandoned (never bind it; create again). The manager proxy
  can be redeployed cheaply; nothing is bound yet.
- After `bindPool`: permanent (GSP has no admin transfer). `i` and `priceLimit` remain governable
  through the manager; the 0.01 LP exits via the Safe's `sellShares`.
- Upgrades: Safe-only `upgradeToAndCall`; the proxy address and pool-admin association never
  change; storage layout continuity is pinned by tests.
- The bootstrap is bounded to the fixed 0.01 budget; no transaction in this package can move
  any other funds.

## Manual migration of the old private pool's liquidity (separate, later operation)

Only after the new pool is created, bound, bootstrapped and sync-verified:
1. Safe revokes the old pool's spare approvals it no longer needs (optional hygiene).
2. Safe calls the old pool's `sellShares(shareAmount, Safe, minBase, minQuote, "", deadline)`.
3. Safe (as LP) mints into the new pool in balanced proportions (`buyShares`), or delegates to
   a reviewed helper; amounts and timing are a human decision (spec BR-11 — never automatic).
4. Do not mark the new pool as a production routing candidate from creation/seed alone; the
   frontend GSP integration is a separate `faroo-xyz/dapp` deliverable.
