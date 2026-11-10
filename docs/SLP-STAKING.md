# SLP staking

## Purpose

`slp/` is the authoritative current maintenance module for the deployed SlpPharosProxy and SlpPharosRewardsVault types.
The historical source repository and original deployment records remain provenance, not runtime dependencies.
`slp/contracts/pharos/`, `slp/DEPENDENCIES.json`, `slp/deployments/PROVENANCE.json` and `slp/build-reference.json`
bind exact sources, dependencies, original compiler source-unit names, complete ABI, metadata and runtime bytes.
The separate SLP build is Solidity 0.8.28 / OZ 5.3.0 / paris; the core remains unchanged.

## Deployment status

Read-only Pharos mainnet chain 1672, provider-finalized block **22640026** (2026-11-10),
hash `0x5701dc0edd4ce908f0e2c239e66062c1728b2a690511df5e04e583ba88a743bd`.
This is a dated state baseline, not automatic approval of future changes. Raw evidence and comparisons are archived in
`faroo-contracts-reserch/slp-consolidation/phase2-batch3-20261110/evidence/`.

| Instance | Proxy | Implementation |
|---|---|---|
| SLP0 / SlpPharosProxy | `0xec1d6163e05b3f5d0fb8f354881f6c8b793ad612` | `0xc3d088842dcf02c13699f936bb83dfbbc6f721ab` |
| SLP1 / SlpPharosProxy_1 | `0xc53d2c9dd1e488e23ff77e219ca68d8138f90c2c` | `0xd15e3eb96409ff2c5be569d132a4520e324a1124` |
| RewardsVault | `0xd0c44d3079e97ff285dd7a3e4e0246320a9487e5` | `0xe6985303e0c917bda24c4566bb2f9af99916c531` |

Both SLP implementations have identical full runtime bytes, but independent proxy state and counters.
Both types pass their separate canonical build. They are outside the `a110037` core audit scope; no completed external
SLP audit is established. Exact-byte compilation is not an audit.

All three EIP-1967 admin slots point to ProxyAdmin `0x480eb384681777bf21e72b8c8538987c2801b4b7`, whose owner is
Timelock `0xc30db0543412c6881a543390d2c21fbbff42805b`. Each proxy has that Timelock as its sole DEFAULT_ADMIN_ROLE
and Keeper `0x8736e68f37c1156e09445ac2d395a139e58fcab4` as its sole WHITELIST_ROLE.

Historical observations remain historical: at blocks 20815667/20817024 the SLP layer was inactive and externally
administered by `0x976cf43c894ba43cf096da8c1427f6a51b724bb9`; authority moved at block 21585015.
The fixed reconciliation checkpoint 21647956 and Phase 1 checkpoint 22633551 are unchanged.
The generated `deployments/registry.json` and `docs/DEPLOYED-STATE.md` retain their original block contexts;
neither is a substitute for this later SLP baseline. Historical four-destination/testnet fixtures remain immutable.

## Principal flow

StPROS (`0x6b0a44c64190279f7034b77c13a566e914fe5ec4`) forwards deposited native PROS to SlpDistributor
(`0xf772b375f93650f798bf724ce7a9a45c9e7a579f`). Keeper allocates to either SLP using `distribute`.
Registered pools are read independently from each instance; Keeper may add/remove pools. `bond` / `batchBond` delegate
to native Staking `0x4100000000000000000000000000000000000000`. `unbond` / `batchUnbond` request exit; `claim` /
`batchClaim` request settlement. Pool membership, maturity, native implementation and response validity remain checks.

`transferBack` sends cash to each SLP's configured `getBridgeVault()`: **SlpDistributor**, despite the historical getter
name. Keeper can then distribute to BridgeVault `0xf1df1cb080256f48e8702c24149136e4a6dec25e` for redemption liquidity.
`totalBonded` and `totalUnbonded` are cumulative counters, not TVL or withdrawable balances. Native returns may include
both principal and reward; these require off-chain classification. Claim events measure synchronous balance changes
and cannot certify delayed node credits or native economic formulas.

## Reward flow

1. Keeper calls `claimReward` / `batchClaimReward`. The SLP records the amount returned by native Staking in
   `totalRewardClaimed`; it does **not** forward rewards in this call.
2. Reconcile the native credit after the transaction. An ordinary EVM fork does not execute Pharos node credit hooks.
   A comment about credit by the next block is not an independently verified timing guarantee.
3. Keeper separately calls `transferToRewardsVault(reconciledRewardAmount)`. `totalRewardTransferred` increases.
   Principal uses `transferBack`. The transfer function does not enforce provenance or `transferred <= claimed`.
4. Keeper calls RewardsVault `notifyOracle()`. It reports its **whole native balance** to Oracle
   `0x051713fd66845a13bf23baca008c5c22c27ccb58` for WPROS `0x52c48d4213107b20bc583832b0d951fb9ca8f0b0` and forwards
   the same amount to **SLP0**, including when the rewards originated at SLP1. A rejected report or failed recipient
   reverts the transaction, including Oracle accounting and commission minting.
5. Reported cash can be restaked; neither claim/transfer events nor restaking are additional yield reports.
   `settleReward` refreshes native accounting; `compoundRewards` bypasses this reporting path and needs reconciliation.

At the dated pin Oracle reporter is RewardsVault, owner is Timelock, unpaused; maximum update is 80,000 PROS,
interval 12,600 seconds, max APR 500,000 ppm. APR allowance can be tighter than the absolute cap.

## Pool management

| Function | Authority |
|---|---|
| registerPoolId / unregisterPoolId; bond, unbond, claim and their batches | WHITELIST_ROLE / Keeper |
| claimReward / batchClaimReward; settleReward / batchSettleReward; compoundRewards | WHITELIST_ROLE / Keeper |
| transferBack / transferToRewardsVault; RewardsVault notifyOracle | WHITELIST_ROLE / Keeper |
| setBridgeVault; RewardsVault setSlpPharosProxy; role grants/revocations | DEFAULT_ADMIN_ROLE / Timelock |
| Upgrade any of the three SLP proxies | Separate ProxyAdmin, owned by Timelock |

Pool lists are dynamic. Removing a pool with remaining stake, rewards or pending exits must remain an alert.
There is no setter for the SLP rewards-vault address after initialization. The canonical implementation ABI preserves
both InsufficientBalance overloads; historical aggregate ABIs omitted one and must not replace it.

## Security and trust assumptions

### Keeper responsibilities

The current SlpDistributor whitelist contains exactly **SLP0, SLP1 and BridgeVault**. RewardsVault is not a direct
destination since block 22235165. Its historical fourth entry is preserved in original release records.
Keeper allocates principal or redemption liquidity, reconciles reward provenance, maintains pool membership and
operates all three SLP contracts. The same operator can move money into RewardsVault and report it.

### Changeable by governance or by the SLP administrator

SLP administrators are now Timelock-controlled, including operator grants and return/forward-address changes.
Keeper pool operations remain immediate. Separate ProxyAdmin ownership does not bypass the Timelock.
A source repository relocation cannot authorize an implementation, role, recipient or codehash change.

### Known risk: principal reported as yield

Any cash in RewardsVault, including donations, forced deposits or principal forwarded by Keeper, is treated as yield
when Oracle accepts the report. Removal from the distributor whitelist closes that direct route only. Misreported
principal raises T a second time and can mint commission. Neither amount/APR caps nor the interval identify provenance.
An over-cap whole balance blocks the only normal outflow; recovery requires separately reviewed governance, never
misclassification to force a report. This is inherited deployed behavior, not introduced by consolidation.

### Other residual risks

Native V5 reward formulas, delayed credits, maturity/settlement and penalty handling require node-level evidence.
A successful EVM claim call is not proof of credited cash. Multiple-pool partial progress across transactions requires
per-instance counters and exact cash reconciliation; a later failed transaction cannot undo an earlier success.
The five historical findings remain UNVERIFIED: asset backing, reward provenance, unexpected value movements,
governance authority, and historical external upgrade control. TVL risk budget remains UNSIZED and full solvency
certification INCOMPLETE. No audit or deployment authorization follows from these tests.
