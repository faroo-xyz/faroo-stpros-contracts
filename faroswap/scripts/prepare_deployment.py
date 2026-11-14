#!/usr/bin/env python3
"""Prepare the (unsigned, non-broadcasting) FaroSwap mainnet deployment package.

Read-only: queries the public Pharos mainnet endpoint through tools/lib/rpc.py,
revalidates the pinned Factory/template/vault identities, derives the current
redemption rate i, predicts CREATE addresses from the deployer nonce across the
COMPLETE ordered transaction sequence, and emits a human-reviewable
transactions.json plus a checksums file into
faroswap/deployments/pharos_pacific_mainnet/prepared/.

All chain reads are pinned to one recorded block. Nothing here signs,
broadcasts or writes chain state. Run from the repository root (works
identically inside the approved export):

    python3 faroswap/scripts/prepare_deployment.py
"""
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.lib.keccak import keccak256_bytes, selector  # noqa: E402
from tools.lib.rpc import Client  # noqa: E402

# ---- pinned Pharos mainnet identities (chain 1672) ----
CHAIN = "mainnet"
STPROS = "0x6b0a44c64190279f7034b77c13a566e914fe5ec4"
WPROS = "0x52c48d4213107b20bc583832b0d951fb9ca8f0b0"
ORACLE = "0x051713fd66845a13bf23baca008c5c22c27ccb58"
GSP_FACTORY = "0xd15f2d50bbf777efd529052686d0703e498f8380"
GSP_TEMPLATE = "0x81b04c62c53a716b7b5ce7fe8de42ab665bca935"
TEMPLATE_RUNTIME_KECCAK = "0xb0bf3be474fb6a7bdfe8c2ee8e7d5f98fd46a44153f8c7b5aa8b8a86ce7cd3e1"
GOV_SAFE = "0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca"
DEPLOYER = "0x8ce84e9fa0101d317d8956d73610ad3e0e219d41"

LP_FEE = 500000000000000
MT_FEE = 0
K = 2000000000000000
PRICE_LIMIT = 10000
BOOTSTRAP_BASE = 10 ** 16  # 0.01 stPROS, fixed maximum bootstrap budget
FIRST_MINT_LOCK = 1001  # GSP 1.0.1 first-mint locked shares

# The deployer's own transactions in execution order. Safe transactions (setExecutor,
# bindPool) are interleaved operationally but do NOT consume the deployer nonce; they are
# excluded here on purpose and annotated in the emitted plan. The bootstrap deploys AFTER
# the factory pool creation because its constructor pins the pool address.
DEPLOYER_SEQUENCE = [
    {"key": "implementation", "action": "deploy FaroSwapPriceManager implementation", "creates": True},
    {"key": "proxy", "action": "deploy ERC1967Proxy (Price Manager proxy)", "creates": True},
    {"key": "pool", "action": "GSPFactory.createDODOGasSavingPool (consumes a deployer nonce)", "creates": False},
    {"key": "bootstrap", "action": "deploy FaroSwapPoolBootstrap (needs the pool address)", "creates": True},
    # conditional funding steps, included only when the balance check finds them missing
    {"key": "fund-wpros", "action": "WPROS.deposit{value}() - wrap native for the quote leg", "creates": False, "conditional": True},
    {"key": "fund-stpros", "action": "stPROS.depositWithPROS{value}() - mint stPROS for the base leg", "creates": False, "conditional": True},
    {"key": "approve-base", "action": "funder approves exact base amount", "creates": False},
    {"key": "approve-quote", "action": "funder approves exact quote amount", "creates": False},
    {"key": "execute", "action": "funder executes the atomic bootstrap", "creates": False},
]


def rlp(item):
    if isinstance(item, list):
        payload = b"".join(rlp(x) for x in item)
        if len(payload) <= 55:
            return bytes([0xC0 + len(payload)]) + payload
        n = (len(payload).bit_length() + 7) // 8
        return bytes([0xF7 + n]) + len(payload).to_bytes(n, "big") + payload
    if isinstance(item, int):
        if item == 0:
            return b"\x80"
        return rlp(item.to_bytes((item.bit_length() + 7) // 8, "big"))
    if isinstance(item, bytes):
        if len(item) == 1 and item[0] < 0x80:
            return item
        if len(item) <= 55:
            return bytes([0x80 + len(item)]) + item
        n = (len(item).bit_length() + 7) // 8
        return bytes([0xB7 + n]) + len(item).to_bytes(n, "big") + item
    raise TypeError(item)


def create_address(sender, nonce):
    return "0x" + keccak256_bytes(rlp([bytes.fromhex(sender[2:]), nonce]))[-20:].hex()


def derive_sequence(sender, base_nonce, include_funding=False):
    """Predicted outcome of the deployer's ordered transaction sequence.

    Pure function (network-free): regression-tested in faroswap/tests/test_deployment_plan.py.
    Returns a list of steps, each with the consuming nonce and, for CREATE steps, the
    predicted address. `include_funding` drops in the two wrap/mint steps used when the
    deployer lacks the bootstrap tokens; every later nonce shifts accordingly.
    """
    steps = []
    nonce = base_nonce
    for spec in DEPLOYER_SEQUENCE:
        if spec.get("conditional") and not include_funding:
            continue
        step = dict(spec, nonce=nonce)
        if spec["creates"]:
            step["predictedAddress"] = create_address(sender, nonce)
        steps.append(step)
        nonce += 1
    return steps


def decode_address_array(data):
    """Decode (address[]) returned data: offset word, length word, then items."""
    body = data[2:] if data.startswith("0x") else data
    if len(body) < 128:
        raise ValueError("address[] return too short")
    offset = int(body[0:64], 16)
    if offset != 32:
        raise ValueError(f"unexpected dynamic offset {offset}")
    length = int(body[64:128], 16)
    items = []
    for i in range(length):
        word = body[128 + 64 * i: 128 + 64 * (i + 1)]
        if len(word) != 64:
            raise ValueError("truncated address[] body")
        items.append("0x" + word[24:])
    return items


def pad_addr(a):
    return "0" * 24 + a[2:].lower()


def pad_uint(n):
    return f"{n:064x}"


def enc(selector_hex, *args):
    """Minimal ABI encoder for static (address, uint256, bool) tuples."""
    head = []
    for a in args:
        if isinstance(a, bool):
            head.append(pad_uint(1 if a else 0))
        elif isinstance(a, int):
            head.append(pad_uint(a))
        elif isinstance(a, str) and a.startswith("0x") and len(a) == 42:
            head.append(pad_addr(a))
        else:
            raise TypeError(a)
    return selector_hex + "".join(head)


def main():
    c = Client(CHAIN, verbose=False)
    block = int(c.call("eth_blockNumber", []), 16)
    pin = hex(block)  # every state read below is pinned to this block

    def call(to, data):
        return c.call("eth_call", [{"to": to, "data": data}, pin])

    problems = []
    warnings = []

    # ---- revalidate pinned identities (recorded block) ----
    template_code = c.call("eth_getCode", [GSP_TEMPLATE, pin])
    template_keccak = "0x" + keccak256_bytes(bytes.fromhex(template_code[2:])).hex()
    if template_keccak != TEMPLATE_RUNTIME_KECCAK:
        problems.append(f"template runtime keccak {template_keccak} != pin {TEMPLATE_RUNTIME_KECCAK}")
    if call(GSP_FACTORY, selector("_GSP_TEMPLATE_()"))[2:][-40:] != GSP_TEMPLATE[2:]:
        problems.append("factory template pointer changed")

    rate = int(call(STPROS, selector("convertToAssets(uint256)") + pad_uint(10 ** 18)), 16)
    if rate == 0 or rate > 10 ** 36:
        problems.append(f"redemption rate out of bounds: {rate}")
    oracle_rate = int(
        call(ORACLE,
             selector("getTokenAmountByVToken(address,uint256,uint8)") + pad_addr(WPROS)
             + pad_uint(10 ** 18) + pad_uint(0)), 16)
    if oracle_rate != rate:
        problems.append(f"vault/oracle rate divergence: {rate} vs {oracle_rate}")

    registry = decode_address_array(
        call(GSP_FACTORY,
             selector("getDODOPool(address,address)") + pad_addr(STPROS) + pad_addr(WPROS)))

    # ---- deployer nonce (latest + pending) and balances ----
    proof = c.call("eth_getProof", [DEPLOYER, [], pin])
    deployer_nonce = int(proof["nonce"], 16)
    deployer_native = int(proof["balance"], 16)
    pending_nonce = None
    try:
        pending_nonce = int(c.call("eth_getProof", [DEPLOYER, [], "pending"])["nonce"], 16)
        if pending_nonce != deployer_nonce:
            warnings.append(
                f"deployer has in-flight transactions (latest nonce {deployer_nonce}, "
                f"pending {pending_nonce}); predictions use the PENDING nonce and can still shift")
    except Exception as exc:  # pending getProof unsupported: record, do not guess
        warnings.append(f"pending-nonce check unavailable ({exc}); predictions use latest nonce")
    base_nonce = pending_nonce if pending_nonce is not None else deployer_nonce

    quote_amount = BOOTSTRAP_BASE * rate // 10 ** 18
    deployer_stpros = int(call(STPROS, selector("balanceOf(address)") + pad_addr(DEPLOYER)), 16)
    deployer_wpros = int(call(WPROS, selector("balanceOf(address)") + pad_addr(DEPLOYER)), 16)
    balances_sufficient = deployer_stpros >= BOOTSTRAP_BASE and deployer_wpros >= quote_amount

    # ---- predicted outcome of the ordered deployer sequence ----
    include_funding = not balances_sufficient

    # funding sizing: 10% headroom over the exact bootstrap legs, rounded up
    fund_wpros_value = (quote_amount * 110 + 99) // 100
    fund_stpros_value = (BOOTSTRAP_BASE * rate * 110 + 10 ** 18 - 1) // 10 ** 18 // 100

    if not balances_sufficient:
        warnings.append(
            f"deployer token balances short (stPROS {deployer_stpros}, WPROS {deployer_wpros}); "
            "funding steps 6a/6b are included in the sequence and resolve this at execution")
    native_needed = 0 if balances_sufficient else fund_wpros_value + fund_stpros_value
    if deployer_native < native_needed + 10 ** 18:
        problems.append(
            f"deployer native balance {deployer_native} below required {native_needed + 10**18} "
            "(funding legs + 1 PROS gas floor)")
    seq = derive_sequence(DEPLOYER, base_nonce, include_funding=include_funding)
    by_key = {s["key"]: s for s in seq}
    impl_at = by_key["implementation"]["predictedAddress"]
    proxy_at = by_key["proxy"]["predictedAddress"]
    bootstrap_at = by_key["bootstrap"]["predictedAddress"]
    pool_tx_nonce = by_key["pool"]["nonce"]

    init_calldata = enc(selector("initialize(address)"), GOV_SAFE)
    create_pool_calldata = enc(
        selector("createDODOGasSavingPool(address,address,address,uint256,uint256,uint256,uint256,uint256,bool)"),
        proxy_at, STPROS, WPROS, LP_FEE, MT_FEE, rate, K, PRICE_LIMIT, False,
    )

    plan = {
        "schema": "faroswap-deployment-plan-v2",
        "network": "pharos-pacific-mainnet-1672",
        "preparedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "preparedBlock": block,
        "readOnly": True,
        "validation": {
            "gspFactory": GSP_FACTORY,
            "gspTemplate": GSP_TEMPLATE,
            "templateRuntimeKeccak": template_keccak,
            "templateMatchesPinnedSource": template_keccak == TEMPLATE_RUNTIME_KECCAK,
            "sourcePin": "Faroswap/contracts@41fefb37ae11ca6be98b6cb17977ef9f6375cf6a + OZ 4.9.3 + solc 0.8.16 (london, optimizer 200)",
            "vaultRateAtBlock": {"block": block, "convertToAssets1e18": rate, "oracleCrossCheck": oracle_rate},
            "stprosWprosRegistryPoolsBeforeDeployment": len(registry),
            "registryPools": registry,
            "deployerState": {
                "nonceLatest": deployer_nonce,
                "noncePending": pending_nonce,
                "baseNonceUsedForPrediction": base_nonce,
                "nativeProSWei": deployer_native,
                "stprosWei": deployer_stpros,
                "wprosWei": deployer_wpros,
                "bootstrapQuoteNeededWei": quote_amount,
                "balancesSufficient": balances_sufficient,
                "fundingStepsIncludedInSequence": include_funding,
                "nativeRequiredWei": native_needed + 10 ** 18,
            },
            "problems": problems,
            "warnings": warnings,
        },
        "protocolConstants": {
            "stprosVault": STPROS,
            "wpros": WPROS,
            "govSafe": GOV_SAFE,
            "deployerAndFunder": DEPLOYER,
            "lpFeeRate": LP_FEE,
            "mtFeeRate": MT_FEE,
            "k": K,
            "priceLimit": PRICE_LIMIT,
            "isOpenTWAP": False,
            "bootstrapBaseAmount": BOOTSTRAP_BASE,
            "bootstrapQuoteAmountAtPreparedRate": quote_amount,
            "bootstrapExpectedFirstMintShares": BOOTSTRAP_BASE - FIRST_MINT_LOCK,
            "bootstrapQuoteDerivation": (
                "quote = floor(0.01e18 * i / 1e18) using the SAME rate the pool is created "
                "with (the pool's own i, not a fresh vault read): the bootstrap enforces exact "
                "inputs and shares == 0.01e18 - 1001"),
        },
        "deployerSequence": seq,
        "interleavedSafeTransactions": [
            "setExecutor(executor, true) after the proxy exists — no deployer nonce consumed",
            "bindPool(pool) after the factory creation — no deployer nonce consumed",
        ],
        "predictedAddresses": {
            "note": ("CREATE predictions follow the ordered DEPLOYER_SEQUENCE from the "
                     "pending/latest nonce; ANY other deployer transaction shifts every "
                     "subsequent address - regenerate before broadcasting"),
            "priceManagerImplementation": impl_at,
            "priceManagerProxy": proxy_at,
            "poolBootstrap": bootstrap_at,
            "poolCreationTxNonce": pool_tx_nonce,
            "gspPool": "non-deterministic (EIP-1167 create via factory; read from the NewGSP event / registry tail after tx)",
        },
        "transactions": [
            {
                "step": 1,
                "title": "deploy FaroSwapPriceManager implementation",
                "from": DEPLOYER,
                "kind": "CREATE",
                "consumesDeployerNonce": by_key["implementation"]["nonce"],
                "constructorArgs": [STPROS, WPROS, GSP_FACTORY, GSP_TEMPLATE, str(LP_FEE), str(MT_FEE), str(K), str(PRICE_LIMIT)],
                "expectedAddress": impl_at,
            },
            {
                "step": 2,
                "title": "deploy ERC1967Proxy (Price Manager proxy, initialize -> Safe)",
                "from": DEPLOYER,
                "kind": "CREATE",
                "consumesDeployerNonce": by_key["proxy"]["nonce"],
                "constructorArgs": ["<implementation = step 1 address>", init_calldata],
                "expectedAddress": proxy_at,
                "note": "ERC1967Proxy from the frozen OZ 5.6.1 vendor closure; proxy admin stays zero (UUPS style)",
            },
            {
                "step": 3,
                "title": "Safe: explicitly authorize the sync executor",
                "via": GOV_SAFE,
                "to": proxy_at,
                "calldataStatus": "INCOMPLETE - executor address not yet chosen; regenerate this package once known",
                "calldataWhenKnown": enc(selector("setExecutor(address,bool)"), "0x" + "00" * 20, 1)[:10] + "<executor-pad32><0001>",
                "verification": f"cast call {proxy_at} 'isExecutor(address)(bool)' <executor> -> true",
            },
            {
                "step": 4,
                "title": "create the public GSP pool through the real Factory",
                "from": DEPLOYER,
                "to": GSP_FACTORY,
                "kind": "CALL",
                "consumesDeployerNonce": pool_tx_nonce,
                "calldata": create_pool_calldata,
                "note": ("permissionless; admin = PM proxy; maintainer forced to the factory "
                         "default; re-read the rate immediately before signing and abort on "
                         "drift; if the rate changed, regenerate steps 4+ (pool i, quote "
                         "amount and every later address move with the nonce)"),
                "post": f"cast call {GSP_FACTORY} 'getDODOPool(address,address)(address[])' {STPROS} {WPROS} -> last element is the new pool",
            },
            {
                "step": 5,
                "title": "Safe: one-time bindPool (full validation, permanent)",
                "via": GOV_SAFE,
                "to": proxy_at,
                "calldataStatus": "INCOMPLETE - pool address exists only after step 4 lands",
                "calldataWhenKnown": enc(selector("bindPool(address)"), "0x" + "00" * 20)[:10] + "<pool-pad32>",
                "noteOnChainChecks": ("bindPool itself enforces: clone-of-verified-template, admin==proxy, "
                                      "token direction, i>0, exact creation economics, EMPTY pool (totalSupply==0), "
                                      "initial i within the pool's own 1% priceLimit of the live vault rate, and "
                                      "factory registry membership"),
                "verification": f"cast call {proxy_at} 'pool()(address)' -> <pool>; 'poolBound()(bool)' -> true",
            },
            {
                "step": 6,
                "title": "deploy FaroSwapPoolBootstrap",
                "from": DEPLOYER,
                "kind": "CREATE",
                "consumesDeployerNonce": by_key["bootstrap"]["nonce"],
                "constructorArgs": ["<pool = step 4 result>", STPROS, WPROS, DEPLOYER, GOV_SAFE, str(BOOTSTRAP_BASE), "<quoteAmount = floor(0.01e18 * poolI / 1e18)>"],
                "expectedAddress": bootstrap_at,
            },
            *([] if not include_funding else [
                {
                    "step": "6a",
                    "title": "funder wraps native PROS for the quote leg (only if WPROS was short)",
                    "from": DEPLOYER,
                    "to": WPROS,
                    "kind": "CALL",
                    "payableValueWei": fund_wpros_value,
                    "consumesDeployerNonce": by_key["fund-wpros"]["nonce"],
                    "calldata": enc(selector("deposit()")),
                    "post": f"cast call {WPROS} 'balanceOf(address)(uint256)' {DEPLOYER} -> >= {quote_amount}",
                },
                {
                    "step": "6b",
                    "title": "funder mints stPROS with native PROS (only if stPROS was short)",
                    "from": DEPLOYER,
                    "to": STPROS,
                    "kind": "CALL",
                    "payableValueWei": fund_stpros_value,
                    "consumesDeployerNonce": by_key["fund-stpros"]["nonce"],
                    "calldata": enc(selector("depositWithPROS()")),
                    "note": "mints at the current oracle rate; 10% headroom covers rounding",
                    "post": f"cast call {STPROS} 'balanceOf(address)(uint256)' {DEPLOYER} -> >= {BOOTSTRAP_BASE}",
                },
            ]),
            {
                "step": 7,
                "title": "funder approves exact amounts to the bootstrap (two txs)",
                "from": DEPLOYER,
                "to": STPROS,
                "kind": "CALL",
                "consumesDeployerNonce": by_key["approve-base"]["nonce"],
                "calldata": enc(selector("approve(address,uint256)"), bootstrap_at, BOOTSTRAP_BASE),
                "plus": {"to": WPROS, "consumesDeployerNonce": by_key["approve-quote"]["nonce"],
                         "calldata": enc(selector("approve(address,uint256)"), bootstrap_at, quote_amount)},
            },
            {
                "step": 8,
                "title": "funder executes the atomic bootstrap",
                "from": DEPLOYER,
                "to": bootstrap_at,
                "kind": "CALL",
                "consumesDeployerNonce": by_key["execute"]["nonce"],
                "calldata": enc(selector("execute()")),
                "note": "the pool must still be empty; pre-seeded/front-run pools are rejected before funds move",
                "post": f"cast call <pool> 'balanceOf(address)(uint256)' {GOV_SAFE} -> {BOOTSTRAP_BASE - FIRST_MINT_LOCK}",
            },
            {
                "step": 9,
                "title": "authorized executor: first on-chain sync verification (AC-20)",
                "from": "<executor>",
                "to": proxy_at,
                "calldata": enc(selector("syncPrice()")),
                "note": "expected no-op right after creation (pool i == live rate); the PriceSynced path is exercised after the next oracle yield report",
            },
        ],
        "rollback": (
            "No single-step rollback exists once the pool is created and bound (GSP has no admin "
            "transfer). Before binding, a wrong pool can simply be abandoned (do not bind; recreate). "
            "After binding, i and priceLimit remain governable; funds are LP-owned by the Safe and "
            "exit via sellShares. The 0.01 bootstrap is the maximum exposure at risk in this package."
        ),
    }

    out_dir = ROOT / "faroswap/deployments/pharos_pacific_mainnet/prepared"
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / "transactions.json"
    text = json.dumps(plan, indent=2, sort_keys=False) + "\n"
    plan_path.write_text(text)
    (out_dir / "checksums.sha256").write_text(
        hashlib.sha256(text.encode()).hexdigest() + "  transactions.json\n")
    print(f"plan written to {out_dir}")
    print(json.dumps(plan["validation"], indent=2))
    print(json.dumps(plan["predictedAddresses"], indent=2))
    if problems:
        print("PROBLEMS - DO NOT PROCEED", problems)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
