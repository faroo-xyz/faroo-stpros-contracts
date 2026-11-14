# FaroSwap Price Manager — design and threat model

Status: IMPLEMENTED, unit + fork tested (see README for evidence). Not deployed.
Requirement baseline: openspec `add-faroswap-owner-price-manager` @
`faroo-spec@ee08f0c1d1e1bcecc855136359591a08aa27bb21`, plus the GSP admin-permissions
research (`faroo-spec/wiki/faroswap-gsp-admin-permissions-2026-11-11.md`).

## Components

| Contract | Role |
|---|---|
| `FaroSwapPriceManager` (UUPS-style, ERC-1967 proxy) | Pool admin. Exactly two GSP admin capabilities: price sync (`adjustPrice`) and the Safe-gated limit (`adjustPriceLimit`). Business owner and upgrade authority: governance Safe, independently verifiable. |
| `FaroSwapPoolBootstrap` (one-shot, immutable) | Atomic 0.01 stPROS + matching WPROS first LP mint, LP to the Safe. No custody, no residual approvals. |

The proxy is `ERC1967Proxy` from the frozen root vendor closure (OZ 5.6.1 bytes). The
implementation carries its own minimal UUPS surface (`upgradeToAndCall`, owner-only) built on
the vendored `ERC1967Utils`, so no new vendored dependency is introduced.

## The fixed price pipeline

`i` is defined once and used identically at pool creation and at every sync:

```
targetI = stPROS.convertToAssets(1e18)          // WPROS per one stPROS share, floored, 1e18-scale
       = Oracle.getTokenAmountByVToken(WPROS, 1e18, Floor)   // identical by construction
```

Base = stPROS, quote = WPROS, so `targetI` is quote-per-base — GSP's own `i` convention, no
reciprocal inversion. Validity bounds mirror GSP `init`: `0 < targetI <= 1e36`; violations
revert before any pool call.

`syncPrice()` ordering (spec AC-29/AC-30/AC-32):

1. executor whitelist check;
2. bound-pool check;
3. read the vault and validate the rate (a failing read propagates — never read as "same price");
4. compare with `pool._I_()`;
5. equal → clean `return`: no pool call, no state write, no event;
6. different → `pool.adjustPrice(targetI)`; pool reverts (e.g. `EXCEED_PRICE_LIMIT`) propagate
   untouched — nothing partial can persist.

The scheduler cadence (default ~4h) is off-chain only; there is deliberately no on-chain
cooldown. `previewSync()` gives the scheduler its gas-saving pre-check; the contract never
trusts it.

## One-time pool binding (`bindPool`)

Creation through `GSPFactory.createDODOGasSavingPool` is permissionless, so binding validates
everything it can on-chain, exactly once, before the association becomes permanent (GSP has no
admin transfer — a wrong binding is unfixable):

1. pool has code and is exactly the 45-byte EIP-1167 clone runtime; the embedded implementation
   equals the factory's current `_GSP_TEMPLATE_()`;
2. the factory template still equals the constructor-pinned `VERIFIED_GSP_TEMPLATE`
   (`0x81b04c62…`, runtime keccak-verified byte-identical to the pinned source — see PARAMS.md);
3. `pool._ADMIN_() == address(this)` (the proxy, never the Safe/deployer/implementation);
4. `pool._BASE_TOKEN_() == stPROS` and `pool._QUOTE_TOKEN_() == WPROS` (direction pinned);
5. `pool._I_() > 0` (initialized);
6. creation economics equal the confirmed constants: LP fee 5e14, MT fee 0, k 2e15,
   priceLimit 10000;
7. the pool is in the factory's `_REGISTRY_[stPROS][WPROS]` (creation provenance).

Check 6 closes the main pre-creation race: anyone can pre-create a pool whose admin is our
(forecastable) proxy address with hostile economics (e.g. 100% LP fee, k=1, priceLimit=1e6).
All such pools fail binding loudly. A hostile pool that copies every parameter is economically
indistinguishable from ours, and the runbook pins the exact pool address from the creation
transaction's `NewGSP` event before the Safe signs the binding.

Two further on-chain conditions close the pre-binding value risks (both one-shot at bind; the
fixed vault source is reused read-only and no price control is introduced):

8. the pool must still be empty (`totalSupply == 0`): binding is permanent, so a pool that was
   already seeded — a front-run first deposit or an accident — would freeze a third party's LP
   shape into the pool we then bootstrap; such a pool is refused (`PoolNotEmpty`) and the
   runbook abandons it for a freshly created one;
9. the pool's initial `i` must satisfy the pool's own first-sync acceptance, computed exactly
   as the verified GSP `adjustPrice` will compute it: `floor(|vaultRate - poolI| * 1e6 / poolI)
   <= priceLimit` (same offset, same old-price denominator, same floor division; the pool-price
   denominator matters because a below-oracle birth price makes the relative step LARGER, e.g.
   0.99e18 vs 1e18 reads 10101/1e6 — permanently unsyncable and refused). A pool born at a
   stale or hostile price can never be bound, and every accepted pool is guaranteed at least
   one synchronization under its confirmed 1% limit. Normal creation-to-binding drift (minutes
   of oracle time) is far below the tolerance, and a legitimate pool drifting past it is
   simply re-created.

Seeding between binding and bootstrap needs no new on-chain rule: the bootstrap itself refuses
a non-empty pool before moving funds (`PoolAlreadySeeded`), so nothing can be lost; the runbook
defines the detection step and the governance decision paths (manual reviewed seeding, or
leaving the pool LP-empty — price sync is independent of LP state).

## Permissions

| Capability | Holder |
|---|---|
| Trigger `syncPrice` | whitelisted executors (`setExecutor`, Safe-only, explicit on-chain event — the independently verifiable authorization step) |
| `setPriceLimit` (0..1e6, validated locally before any pool call) | governance Safe only |
| `bindPool` (one-time) | governance Safe only |
| `transferOwnership`/`acceptOwnership` (two-step) | governance Safe → nominated Safe |
| `upgradeToAndCall` | governance Safe only |
| GSP `adjustK`/fees/`withdrawMtFeeTotal` | FaroSwap maintainer (factory default) — never us |
| Bootstrap `execute` | the funder only, once |

Executors hold no upgrade, pool-management, limit or funds authority; renunciation of PM
ownership is disabled (a GSP pool admin cannot be re-pointed, so renouncing would strand the
pool's price forever — the same posture as the core repo's `GuardedOwnableUpgradeable`).

## Storage and upgrades

Namespaced at ERC-7201 `faroswap.price-manager.v1`:
`mapping isExecutor` at the slot, `address pool` and `bool poolBound` packed in the next slot.
The unit suite pins the raw slots, so layout drift in a future implementation fails loudly.
Upgrade rehearsals (unit + fork) show owner/executor/binding preservation, unchanged proxy
address and pool-admin association, and a working sync afterwards.

The implementation itself carries the full standard UUPS surface, mirroring OpenZeppelin's
UUPSUpgradeable semantics on the vendored `ERC1967Utils`:

- `proxiableUUID()` returns the ERC-1967 implementation slot, guarded by `notDelegateCall`
  (`address(this) != __self` with an immutable `__self`): the signal certifies implementations
  and must only answer to direct probes, never through a proxy.
- `upgradeToAndCall` is `onlyProxy` (rejects calls landing directly on the bare implementation
  and the ERC-1967 transparent-proxy admin as caller) and still Safe-only.
- The candidate check staticcalls `proxiableUUID()` and requires the ERC-1967 slot. Because
  compliant implementations answer only to direct calls, an ERC-1967 proxy or an EIP-1167 clone
  in front of one reverts the probe and is rejected as a target (`UnsupportedProxiableUUID`); an
  explicit `UpgradeToSelfProxy` check refuses the proxy's own address with a precise error.
- Storage-layout compatibility remains a release gate verified by the pinned-slot tests and the
  upgrade rehearsals (forward chain V1→V2→V3 and rollback V3→V1 both preserve owner, executors,
  binding and sync), not a runtime property.

An attacker's own `ERC1967Proxy` over the public implementation initializes fine (expected for
a fresh proxy) but is a separate, powerless instance: our binding is untouched, and binding our
pool from it fails the admin check. The bare implementation cannot be initialized
(`_disableInitializers` in its constructor).

## Reentrancy posture

No guard is installed, by design: every external callee is either the bound pool — proven at
bind time to be a byte-exact clone of the verified GSP 1.0.1 template, whose `adjustPrice` /
`adjustPriceLimit` / `buyShares` make no calls out — or the fixed view `convertToAssets` on the
frozen stPROS vault. There is no untrusted callback surface; the clone-code proof is the
load-bearing control. A unit test documents that even a hypothetical reentrant pool cannot
corrupt state (the recomputed target is idempotent).

## Bootstrap security

GSP requires tokens in the pool before `buyShares`; two separate public transfers would expose
the seed to LP-mint interception. `FaroSwapPoolBootstrap` therefore pulls the exact amounts
from the funder via `transferFrom` and mints LP to the Safe inside one transaction: any failure
reverts everything, allowances are consumed exactly, the helper never holds balances and can do
nothing else (one-shot, funder-only). LP-value protection is strict: the pool must still be
EMPTY (`totalSupply == 0`) when execution starts — a pre-seeded, front-run or otherwise
initialized pool is rejected before any funds move; after the mint, the base/quote inputs must
equal exactly the configured amounts (GSP counts every balance delta as input, so pre-existing
donations surface there and abort the run) and the minted amount must equal exactly
`0.01e18 − 1001` (with exact inputs on an empty pool the pinned template's first-mint formula
is deterministic: `quoteBalance == mulFloor(baseBalance, i)` selects the base side, minus the
1001-wei lock at `0x0`). Every failure reverts atomically — the funder's tokens never leave a
failed run. The quote leg is `floor(0.01e18 * poolI / 1e18)` using the pool's own creation `i`
— the same rate, rounding and direction as pool creation.

## Explicitly out of scope (per the approved spec)

Arbitrary price input; mutable price sources; arbitrary external calls; pause/resume of sync;
manual admin sync; maintainer controls; automatic limit relaxation or step-around; automatic
liquidity migration (the old private pool's liquidity moves manually by the Safe after the new
pool is verified); frontend routing (separate `faroo-xyz/dapp` scope).
