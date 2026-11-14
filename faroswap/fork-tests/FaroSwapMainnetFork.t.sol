// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {FaroSwapPriceManager} from "../contracts/FaroSwapPriceManager.sol";
import {FaroSwapPoolBootstrap} from "../contracts/FaroSwapPoolBootstrap.sol";
import {IFaroSwapGSP} from "../contracts/interfaces/IFaroSwapGSP.sol";
import {IFaroSwapGSPFactory} from "../contracts/interfaces/IFaroSwapGSPFactory.sol";

interface IMainnetVault {
    function convertToAssets(uint256 shares) external view returns (uint256);
    function deposit(uint256 assets, address receiver) external returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function balanceOf(address a) external view returns (uint256);
    function totalSupply() external view returns (uint256);
}

interface IMainnetWPROS is IERC20 {
    function deposit() external payable;
}

interface IMainnetOracle {
    function slp() external view returns (address);
    function update(address token, uint256 tokenAmount) external;
    function lastUpdateAt(address token) external view returns (uint256);
    function updateInterval() external view returns (uint256);
    function maxUpdateAmount() external view returns (uint256);
    function maxAprPpm() external view returns (uint256);
    function poolInfo(address token) external view returns (uint256 tokenAmount, uint256 vTokenAmount);
    function canUpdate(address token, uint256 tokenAmount) external view returns (bool);
}

/// @title Pharos mainnet fork rehearsal of the complete FaroSwap GSP delivery.
/// @notice Skipped unless FAROO_FORK_RPC_MAINNET is set; run only inside the approved export
///         (make faroswap-fork). Everything happens on a local fork of the public read-only
///         endpoint; nothing is ever broadcast. The rehearsal reproduces the production
///         workflow end to end against the real deployed Factory, template, stPROS vault and
///         Oracle: verify -> deploy manager -> create pool -> bind -> bootstrap -> sync ->
///         negative paths -> price-limit administration.
contract FaroSwapMainnetForkTest is Test {
    // ---- pinned Pharos mainnet identities (chain 1672), verified 2026-11-13 ----
    address internal constant STPROS = 0x6b0A44c64190279f7034b77c13a566E914FE5Ec4;
    address internal constant WPROS = 0x52C48d4213107b20bC583832b0d951FB9CA8F0B0;
    address internal constant ORACLE = 0x051713fD66845a13BF23BACa008C5C22C27Ccb58;
    address internal constant GSP_FACTORY = 0xD15f2D50bbf777EFD529052686d0703e498F8380;
    address internal constant GSP_TEMPLATE = 0x81B04C62c53a716B7b5ce7fE8de42ab665BcA935;
    address internal constant FAROSWAP_MAINTAINER = 0x4F36CdD2180b2F9b17745d8f7FF380440262Ae7A;
    address internal constant GOV_SAFE = 0x3658e00f5DDb9Fa0c7e9820e8d16C3C17eaB73CA;
    address internal constant DEPLOYER = 0x8Ce84E9Fa0101D317D8956D73610ad3e0E219d41;
    // Runtime keccak256 of the deployed GSP 1.0.1 template (read at block 22918977).
    bytes32 internal constant TEMPLATE_RUNTIME_KECCAK =
        0xb0bf3be474fb6a7bdfe8c2ee8e7d5f98fd46a44153f8c7b5aa8b8a86ce7cd3e1;

    uint256 internal constant LP_FEE = 500000000000000;
    uint256 internal constant MT_FEE = 0;
    uint256 internal constant K = 2000000000000000;
    uint256 internal constant PRICE_LIMIT = 10000;
    uint256 internal constant BOOTSTRAP_BASE = 10 ** 16; // 0.01 stPROS, fixed budget

    /// @dev Recorded, reproducible fork pin (regression block for the remediation run,
    ///         2026-11-13). Override with FAROO_FORK_PIN for newer blocks.
    uint256 internal constant FORK_PIN = 22921747;

    string internal url;
    address internal executor = makeAddr("sync-executor");
    address internal stranger = makeAddr("stranger");
    FaroSwapPriceManager internal impl;
    FaroSwapPriceManager internal pm;
    address internal pool;
    uint256 internal creationI;

    function setUp() public {
        url = vm.envOr("FAROO_FORK_RPC_MAINNET", string(""));
        if (bytes(url).length == 0) return;
        uint256 pin = vm.envOr("FAROO_FORK_PIN", uint256(0));
        vm.createSelectFork(url, pin == 0 ? FORK_PIN : pin);

        impl = new FaroSwapPriceManager(
            STPROS, WPROS, GSP_FACTORY, GSP_TEMPLATE, LP_FEE, MT_FEE, K, PRICE_LIMIT
        );
        pm = FaroSwapPriceManager(
            address(new ERC1967Proxy(address(impl), abi.encodeCall(FaroSwapPriceManager.initialize, (GOV_SAFE))))
        );
        creationI = IMainnetVault(STPROS).convertToAssets(10 ** 18);
    }

    modifier forkOnly() {
        if (bytes(url).length == 0) vm.skip(true);
        _;
    }

    // ---------------------------------------------------------------- step 1: verify deployed system
    function test_01_verifyDeployedFactoryTemplateAndVault() public forkOnly {
        assertEq(
            keccak256(GSP_TEMPLATE.code),
            TEMPLATE_RUNTIME_KECCAK,
            "GSP template runtime hash must match the recorded pin"
        );
        assertEq(GSP_TEMPLATE.code.length, 19253, "template runtime size");

        IFaroSwapGSPFactory factory = IFaroSwapGSPFactory(GSP_FACTORY);
        assertEq(factory._GSP_TEMPLATE_(), GSP_TEMPLATE, "factory template");
        assertEq(factory._DEFAULT_MAINTAINER_(), FAROSWAP_MAINTAINER, "factory default maintainer");

        assertEq(IMainnetVault(STPROS).convertToAssets(10 ** 18), creationI, "stable rate read");
        assertTrue(creationI > 10 ** 18 && creationI < 11 * 10 ** 17, "rate in sane range");

        // The three pre-existing stPROS/WPROS GSP pools prove the factory pair works.
        address[] memory existing = factory.getDODOPool(STPROS, WPROS);
        assertGe(existing.length, 3, "existing stPROS/WPROS GSP pools");
    }

    // ---------------------------------------------------------------- steps 2-5: create + bind
    function test_02_createPoolViaRealFactory_andBind() public forkOnly {
        vm.prank(DEPLOYER);
        pool = IFaroSwapGSPFactory(GSP_FACTORY).createDODOGasSavingPool(
            address(pm), STPROS, WPROS, LP_FEE, MT_FEE, creationI, K, PRICE_LIMIT, false
        );

        IFaroSwapGSP gsp = IFaroSwapGSP(pool);
        assertEq(gsp._ADMIN_(), address(pm), "pool admin is the Price Manager proxy");
        assertEq(gsp._MAINTAINER_(), FAROSWAP_MAINTAINER, "maintainer is the factory default, not us");
        assertEq(gsp._BASE_TOKEN_(), STPROS, "base = stPROS");
        assertEq(gsp._QUOTE_TOKEN_(), WPROS, "quote = WPROS");
        assertEq(gsp._I_(), creationI, "pool born at the live redemption rate");
        assertEq(gsp._PRICE_LIMIT_(), PRICE_LIMIT, "initial priceLimit 10000");
        assertEq(gsp._K_(), K, "k from the private-pool baseline");
        assertEq(gsp._LP_FEE_RATE_(), LP_FEE, "LP fee from the private-pool baseline");
        assertEq(gsp._MT_FEE_RATE_(), MT_FEE, "protocol fee 0");

        vm.prank(GOV_SAFE);
        pm.bindPool(pool);
        assertTrue(pm.poolBound());
        assertEq(pm.pool(), pool);

        // An impostor pool with different economics must not be bindable: the bound manager
        // refuses a second binding outright, and a fresh manager — targeted by a hostile pool
        // that correctly names it as admin — still rejects the wrong creation parameters.
        vm.prank(GOV_SAFE);
        vm.expectRevert(FaroSwapPriceManager.PoolAlreadyBound.selector);
        pm.bindPool(pool); // rebinding the legit pool already fails

        FaroSwapPriceManager freshImpl = new FaroSwapPriceManager(
            STPROS, WPROS, GSP_FACTORY, GSP_TEMPLATE, LP_FEE, MT_FEE, K, PRICE_LIMIT
        );
        FaroSwapPriceManager fresh = FaroSwapPriceManager(
            address(new ERC1967Proxy(address(freshImpl), abi.encodeCall(FaroSwapPriceManager.initialize, (GOV_SAFE))))
        );
        vm.prank(stranger);
        address hostile = IFaroSwapGSPFactory(GSP_FACTORY).createDODOGasSavingPool(
            address(fresh), STPROS, WPROS, 10 ** 18, 0, creationI, 10 ** 18, 1000000, false
        );
        assertEq(IFaroSwapGSP(hostile)._ADMIN_(), address(fresh), "impostor targets the fresh proxy");
        vm.prank(GOV_SAFE);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.PoolCreationParamsMismatch.selector,
                hostile,
                10 ** 18, // lpFeeRate
                0, // mtFeeRate
                10 ** 18, // k
                1000000 // priceLimit
            )
        );
        fresh.bindPool(hostile);
    }

    function _createAndBind() internal returns (address p) {
        vm.prank(DEPLOYER);
        p = IFaroSwapGSPFactory(GSP_FACTORY).createDODOGasSavingPool(
            address(pm), STPROS, WPROS, LP_FEE, MT_FEE, creationI, K, PRICE_LIMIT, false
        );
        vm.prank(GOV_SAFE);
        pm.bindPool(p);
    }

    // ---------------------------------------------------------------- steps 6-8: atomic bootstrap
    function test_03_bootstrap_atomicAndSafeHoldsLP() public forkOnly {
        pool = _createAndBind();
        _bootstrap(pool, creationI);

        IFaroSwapGSP gsp = IFaroSwapGSP(pool);
        assertGt(gsp.balanceOf(GOV_SAFE), 0, "Safe holds the LP shares");
        assertEq(gsp.balanceOf(GOV_SAFE), BOOTSTRAP_BASE - 1001, "first mint minus the 1001 lock");
        assertEq(gsp.balanceOf(address(0)), 1001, "GSP first-mint lock at 0x0");
        assertEq(gsp.totalSupply(), BOOTSTRAP_BASE, "totalSupply includes the lock");
        assertEq(gsp._BASE_TARGET_(), BOOTSTRAP_BASE, "base target");
        assertEq(gsp._QUOTE_TARGET_(), BOOTSTRAP_BASE * gsp._I_() / 10 ** 18, "quote target");

        (uint256 bR, uint256 qR) = gsp.getVaultReserve();
        assertEq(bR, BOOTSTRAP_BASE, "base reserve");
        assertEq(qR, BOOTSTRAP_BASE * creationI / 10 ** 18, "quote reserve");
    }

    function _bootstrap(address p, uint256 rate) internal returns (FaroSwapPoolBootstrap b) {
        uint256 quoteAmount = BOOTSTRAP_BASE * rate / 10 ** 18;
        // Fund the deployer on the fork only: wrap native PROS, then deposit WPROS for stPROS.
        vm.deal(DEPLOYER, 1 ether);
        vm.startPrank(DEPLOYER);
        IMainnetWPROS(WPROS).deposit{value: 1 ether}();
        IMainnetWPROS(WPROS).approve(STPROS, 10 ** 17);
        IMainnetVault(STPROS).deposit(10 ** 17, DEPLOYER); // ~0.1 WPROS worth of stPROS
        vm.stopPrank();
        assertGe(IMainnetVault(STPROS).balanceOf(DEPLOYER), BOOTSTRAP_BASE, "funder has base");
        assertGe(IMainnetWPROS(WPROS).balanceOf(DEPLOYER), quoteAmount, "funder has quote");

        b = new FaroSwapPoolBootstrap(p, STPROS, WPROS, DEPLOYER, GOV_SAFE, BOOTSTRAP_BASE, quoteAmount);
        vm.startPrank(DEPLOYER);
        IERC20(STPROS).approve(address(b), BOOTSTRAP_BASE);
        IERC20(WPROS).approve(address(b), quoteAmount);
        b.execute();
        vm.stopPrank();

        assertEq(IERC20(STPROS).allowance(DEPLOYER, address(b)), 0, "no residual base approval");
        assertEq(IERC20(WPROS).allowance(DEPLOYER, address(b)), 0, "no residual quote approval");
    }

    // ---------------------------------------------------------------- steps 9-11: sync paths
    function test_04_executorAuthorizationAndSyncNoopThenUpdate() public forkOnly {
        pool = _createAndBind();

        // The explicit, independently verifiable authorization step.
        vm.prank(GOV_SAFE);
        pm.setExecutor(executor, true);
        assertTrue(pm.isExecutor(executor));

        // Unauthorized and stranger syncs are rejected (auth precedes the no-op decision).
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.NotExecutor.selector, stranger));
        pm.syncPrice();

        // Pool was created at the current rate: sync is a clean no-op.
        vm.prank(executor);
        pm.syncPrice();
        assertEq(IFaroSwapGSP(pool)._I_(), creationI, "no-op left i untouched");

        // A real Oracle yield report moves the redemption rate; sync follows it exactly.
        uint256 newTarget = _applySmallOracleYield();
        (uint256 preview, uint256 current, bool needed) = pm.previewSync();
        assertTrue(needed);
        assertEq(preview, newTarget);
        assertEq(current, creationI);

        vm.prank(executor);
        pm.syncPrice();
        assertEq(IFaroSwapGSP(pool)._I_(), newTarget, "pool i equals the recomputed target");
        assertEq(IFaroSwapGSP(pool)._I_(), IMainnetVault(STPROS).convertToAssets(10 ** 18));

        (,, bool neededAfter) = pm.previewSync();
        assertFalse(neededAfter, "converged");

        // Revocation blocks further syncs.
        vm.prank(GOV_SAFE);
        pm.setExecutor(executor, false);
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.NotExecutor.selector, executor));
        pm.syncPrice();
    }

    /// @dev Simulates the production yield-report path: the Oracle's registered SLP reporter
    ///      calls update with a small amount, moving tokenAmount/vTokenAmount. The amount is
    ///      sized inside the APR-derived rate allowance (warping one interval past the last
    ///      update) and below the per-update cap. Returns the new redemption target i read
    ///      from the vault after the report.
    function _applySmallOracleYield() internal returns (uint256) {
        IMainnetOracle oracle = IMainnetOracle(ORACLE);
        uint256 interval = oracle.updateInterval();
        uint256 ready = oracle.lastUpdateAt(WPROS) + interval + 1;
        if (block.timestamp < ready) vm.warp(ready);
        uint256 elapsed = block.timestamp - oracle.lastUpdateAt(WPROS);

        (uint256 tokenAmount,) = oracle.poolInfo(WPROS);
        uint256 allowance = tokenAmount * oracle.maxAprPpm() * elapsed / (365 days * 10 ** 6);
        uint256 amount = allowance * 3 / 4;
        if (amount == 0) revert("zero oracle update allowance on fork");
        if (amount > oracle.maxUpdateAmount()) amount = oracle.maxUpdateAmount();

        vm.prank(oracle.slp());
        oracle.update(WPROS, amount);

        uint256 newTarget = IMainnetVault(STPROS).convertToAssets(10 ** 18);
        uint256 rel = newTarget > creationI ? newTarget - creationI : creationI - newTarget;
        assertLt(rel * 10 ** 6 / creationI, 9000, "simulated yield must stay under the 1% limit");
        return newTarget;
    }

    // ---------------------------------------------------------------- step 12: pure-i invariant
    function test_05_syncPreservesAllOtherPoolState() public forkOnly {
        pool = _createAndBind();
        vm.prank(GOV_SAFE);
        pm.setExecutor(executor, true);

        // Push the pool off RState=ONE with a one-sided bootstrap + swap so the invariant is
        // exercised in a non-ONE state (stored targets must not move).
        _bootstrap(pool, creationI);
        vm.startPrank(DEPLOYER);
        IERC20(STPROS).transfer(pool, 10 ** 12);
        IFaroSwapGSP(pool).sellBase(DEPLOYER);
        vm.stopPrank();

        uint256 bT = IFaroSwapGSP(pool)._BASE_TARGET_();
        uint256 qT = IFaroSwapGSP(pool)._QUOTE_TARGET_();
        uint256 rState = IFaroSwapGSP(pool)._RState_();
        (uint256 bR, uint256 qR) = IFaroSwapGSP(pool).getVaultReserve();

        uint256 newTarget = _applySmallOracleYield();
        vm.prank(executor);
        pm.syncPrice();

        assertEq(IFaroSwapGSP(pool)._I_(), newTarget, "only i moved");
        assertEq(IFaroSwapGSP(pool)._BASE_TARGET_(), bT, "stored base target unchanged");
        assertEq(IFaroSwapGSP(pool)._QUOTE_TARGET_(), qT, "stored quote target unchanged");
        assertEq(IFaroSwapGSP(pool)._RState_(), rState, "RState unchanged");
        (uint256 bR2, uint256 qR2) = IFaroSwapGSP(pool).getVaultReserve();
        assertEq(bR2, bR, "base reserve unchanged");
        assertEq(qR2, qR, "quote reserve unchanged");
        assertEq(IFaroSwapGSP(pool)._K_(), K, "k unchanged");
        assertEq(IFaroSwapGSP(pool)._LP_FEE_RATE_(), LP_FEE, "lp fee unchanged");
        assertEq(IFaroSwapGSP(pool)._MT_FEE_RATE_(), MT_FEE, "mt fee unchanged");
    }

    // ---------------------------------------------------------------- step 13: extreme small swaps
    function test_06_extremelySmallBidirectionalSwaps() public forkOnly {
        pool = _createAndBind();
        _bootstrap(pool, creationI);
        vm.startPrank(DEPLOYER);
        uint256 quoteBefore = IMainnetWPROS(WPROS).balanceOf(DEPLOYER);

        IERC20(STPROS).transfer(pool, 10 ** 12); // 1e-6 stPROS
        uint256 gainedQuote = IFaroSwapGSP(pool).sellBase(DEPLOYER);
        assertGt(gainedQuote, 0);
        assertGt(IMainnetWPROS(WPROS).balanceOf(DEPLOYER), quoteBefore, "quote received");

        uint256 baseBefore = IMainnetVault(STPROS).balanceOf(DEPLOYER);
        IERC20(WPROS).transfer(pool, 10 ** 12); // 1e-6 WPROS
        uint256 gainedBase = IFaroSwapGSP(pool).sellQuote(DEPLOYER);
        assertGt(gainedBase, 0);
        assertGt(IMainnetVault(STPROS).balanceOf(DEPLOYER), baseBefore, "base received");
        vm.stopPrank();

        // Sync still converges after trading has moved reserves.
        vm.prank(GOV_SAFE);
        pm.setExecutor(executor, true);
        uint256 newTarget = _applySmallOracleYield();
        vm.prank(executor);
        pm.syncPrice();
        assertEq(IFaroSwapGSP(pool)._I_(), newTarget);
    }

    // ---------------------------------------------------------------- bootstrap hostile paths (real bytecode)
    function test_03b_bootstrap_rejectsPreSeededPool_onRealGSP() public forkOnly {
        pool = _createAndBind();
        // An attacker front-runs with their own first LP mint; the bootstrap must refuse the
        // now-non-empty pool before moving any funder funds.
        vm.deal(DEPLOYER, 1 ether);
        vm.startPrank(DEPLOYER);
        IMainnetWPROS(WPROS).deposit{value: 1 ether}();
        IMainnetWPROS(WPROS).approve(STPROS, 10 ** 17);
        IMainnetVault(STPROS).deposit(10 ** 17, DEPLOYER);
        vm.stopPrank();

        uint256 seedBase = 2 * 10 ** 15;
        uint256 seedQuote = seedBase * creationI / 10 ** 18;
        vm.startPrank(DEPLOYER); // any holder works; the funder holds tokens on this fork
        IERC20(STPROS).transfer(pool, seedBase);
        IERC20(WPROS).transfer(pool, seedQuote);
        IFaroSwapGSP(pool).buyShares(DEPLOYER);
        vm.stopPrank();
        assertGt(IFaroSwapGSP(pool).totalSupply(), 0, "pool seeded");

        FaroSwapPoolBootstrap b =
            new FaroSwapPoolBootstrap(pool, STPROS, WPROS, DEPLOYER, GOV_SAFE, BOOTSTRAP_BASE, BOOTSTRAP_BASE * creationI / 10 ** 18);
        uint256 baseBefore = IERC20(STPROS).balanceOf(DEPLOYER);
        vm.prank(DEPLOYER);
        vm.expectRevert();
        b.execute();
        assertEq(IERC20(STPROS).balanceOf(DEPLOYER), baseBefore, "no funds moved");
        assertEq(IFaroSwapGSP(pool).balanceOf(GOV_SAFE), 0, "no LP to the Safe");
    }

    // ---------------------------------------------------------------- step 11: price limit admin
    function test_07_priceLimitAdjustmentAndRejection() public forkOnly {
        pool = _createAndBind();

        vm.prank(executor);
        vm.expectRevert(); // executor holds no limit authority (OwnableUnauthorizedAccount)
        pm.setPriceLimit(500);

        vm.prank(GOV_SAFE);
        pm.setPriceLimit(500);
        assertEq(IFaroSwapGSP(pool)._PRICE_LIMIT_(), 500);

        vm.prank(GOV_SAFE);
        pm.setPriceLimit(1000000); // protocol maximum boundary
        assertEq(IFaroSwapGSP(pool)._PRICE_LIMIT_(), 1000000);

        vm.prank(GOV_SAFE);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.PriceLimitAboveMax.selector, 1000001));
        pm.setPriceLimit(1000001);
        assertEq(IFaroSwapGSP(pool)._PRICE_LIMIT_(), 1000000, "pool untouched by the rejected call");

        vm.prank(GOV_SAFE);
        pm.setPriceLimit(PRICE_LIMIT); // restore the confirmed 1% for the record
    }

    // ---------------------------------------------------------------- over-limit interruption case
    function test_08_largeAccumulatedJumpBlockedUntilSafeRaisesLimit() public forkOnly {
        // Binding now enforces price consistency, so the interruption scenario is simulated
        // the way it happens in production: the pool is born AT the live rate, then the
        // oracle rate drifts >1% while synchronization is interrupted (multiple spaced
        // reports). The pool's own protection rejects the catch-up jump; only a Safe limit
        // raise, then a fresh sync, converges.
        pool = _createAndBind();
        vm.prank(GOV_SAFE);
        pm.setExecutor(executor, true);

        uint256 newTarget = _applyOracleYieldIterated(12000); // >= 1.2% accumulated drift
        assertGt(newTarget, creationI);

        vm.prank(executor);
        vm.expectRevert(); // EXCEED_PRICE_LIMIT from the pool
        pm.syncPrice();
        assertEq(IFaroSwapGSP(pool)._I_(), creationI, "i untouched by the rejected catch-up");

        vm.prank(GOV_SAFE);
        pm.setPriceLimit(1000000); // governance decision, one-time window
        vm.prank(executor);
        pm.syncPrice();
        assertEq(IFaroSwapGSP(pool)._I_(), IMainnetVault(STPROS).convertToAssets(10 ** 18), "converged");
    }

    /// @dev Accumulates oracle yield reports (each spaced past the update interval; sizes
    ///      derived from the oracle's own `canUpdate` advisory so every internal bound —
    ///      per-update cap, cooldown and the APR catch-up allowance — is respected) until
    ///      the redemption rate has moved at least `minPpm` relative to its fork-start
    ///      value. Returns the new target i.
    function _applyOracleYieldIterated(uint256 minPpm) internal returns (uint256) {
        IMainnetOracle oracle = IMainnetOracle(ORACLE);
        uint256 baseRate = IMainnetVault(STPROS).convertToAssets(10 ** 18);
        uint256 current = baseRate;
        for (uint256 round = 0; round < 40; round++) {
            uint256 moved = current > baseRate
                ? (current - baseRate) * 10 ** 6 / baseRate
                : (baseRate - current) * 10 ** 6 / baseRate;
            if (moved >= minPpm) return current;

            vm.warp(oracle.lastUpdateAt(WPROS) + oracle.updateInterval() + 7 days);
            (uint256 tokenAmount,) = oracle.poolInfo(WPROS);
            uint256 amount = oracle.maxUpdateAmount();
            for (uint256 shrink = 0; !oracle.canUpdate(WPROS, amount); shrink++) {
                amount = amount * 9 / 10;
                if (amount == 0 || shrink > 200) revert("no feasible oracle update size on fork");
            }
            vm.prank(oracle.slp());
            oracle.update(WPROS, amount);
            current = IMainnetVault(STPROS).convertToAssets(10 ** 18);
        }
        revert("fork yield iteration could not reach the requested drift");
    }

    // ---------------------------------------------------------------- pre-binding safety on real bytecode
    function test_09_bindRejectsStaleInitialI_andSeededPool() public forkOnly {
        // A pool born 5% off the live rate is refused at binding (BindPriceDeviation) and
        // nothing gets bound; the runbook then abandons it and creates a correct pool.
        vm.prank(DEPLOYER);
        address stale = IFaroSwapGSPFactory(GSP_FACTORY).createDODOGasSavingPool(
            address(pm), STPROS, WPROS, LP_FEE, MT_FEE, creationI * 95 / 100, K, PRICE_LIMIT, false
        );
        vm.prank(GOV_SAFE);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.BindPriceDeviation.selector,
                creationI * 95 / 100,
                creationI,
                PRICE_LIMIT
            )
        );
        pm.bindPool(stale);
        assertFalse(pm.poolBound());

        // A correct pool that someone seeded before binding is refused as well.
        vm.prank(DEPLOYER);
        address seeded = IFaroSwapGSPFactory(GSP_FACTORY).createDODOGasSavingPool(
            address(pm), STPROS, WPROS, LP_FEE, MT_FEE, creationI, K, PRICE_LIMIT, false
        );
        vm.deal(DEPLOYER, 1 ether);
        vm.startPrank(DEPLOYER);
        IMainnetWPROS(WPROS).deposit{value: 1 ether}();
        IMainnetWPROS(WPROS).approve(STPROS, 10 ** 17);
        IMainnetVault(STPROS).deposit(10 ** 17, DEPLOYER);
        IERC20(STPROS).transfer(seeded, 2 * 10 ** 15);
        IERC20(WPROS).transfer(seeded, 2 * 10 ** 15 * creationI / 10 ** 18);
        IFaroSwapGSP(seeded).buyShares(DEPLOYER);
        vm.stopPrank();
        assertGt(IFaroSwapGSP(seeded).totalSupply(), 0, "pool seeded");

        vm.prank(GOV_SAFE);
        vm.expectRevert(); // PoolNotEmpty with the seeded supply
        pm.bindPool(seeded);
        assertFalse(pm.poolBound());

        // The intended workflow still completes: fresh correct pool binds cleanly.
        pool = _createAndBind();
        assertTrue(pm.poolBound());
        assertEq(pm.pool(), pool);
    }
}
