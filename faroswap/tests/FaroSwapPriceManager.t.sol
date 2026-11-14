// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {ERC1967Utils} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Utils.sol";
import {Initializable} from "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import {IERC1967} from "@openzeppelin/contracts/interfaces/IERC1967.sol";

import {FaroSwapPriceManager} from "../contracts/FaroSwapPriceManager.sol";
import {FaroSwapPriceManagerV2} from "./mocks/FaroSwapPriceManagerV2.sol";
import {FaroSwapPriceManagerV3} from "./mocks/FaroSwapPriceManagerV3.sol";
import {IFaroSwapGSP} from "../contracts/interfaces/IFaroSwapGSP.sol";
import {MockGSP} from "./mocks/MockGSP.sol";
import {MockGSPFactory, MockCloneFactory} from "./mocks/MockGSPFactory.sol";
import {MockStPROS} from "./mocks/Mocks.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

/// @title FaroSwapPriceManager unit and security tests (mock pool topology).
/// @notice The mock pool/factory reproduce the pinned GSP 1.0.1 semantics byte-for-byte in
///         behavior (see MockGSP). Fork tests cover the deployed bytecode separately.
contract FaroSwapPriceManagerTest is Test {
    uint256 internal constant LP_FEE = 500000000000000; // 0.05%
    uint256 internal constant MT_FEE = 0;
    uint256 internal constant K = 2000000000000000; // 0.002
    uint256 internal constant PRICE_LIMIT = 10000; // ~1%
    bytes32 internal constant IMPL_SLOT =
        0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc;
    // erc7201:faroswap.price-manager.v1 (see FaroSwapPriceManager.STORAGE)
    bytes32 internal constant PM_STORAGE =
        0x1b7714590d5370353247b3299283b707765d01564ce2954820f2462bb8b85f00;

    address internal safe;
    address internal newSafe;
    address internal executor;
    address internal attacker;
    address internal maintainer;

    MockStPROS internal stpros; // base token + redemption-rate source
    MockERC20 internal wpros; // quote token
    MockGSP internal gspTemplate;
    MockCloneFactory internal cloneFactory;
    MockGSPFactory internal factory;
    FaroSwapPriceManager internal impl;
    FaroSwapPriceManager internal pm; // proxy
    address internal pool;

    function setUp() public {
        safe = makeAddr("governance-safe");
        newSafe = makeAddr("new-safe");
        executor = makeAddr("sync-executor");
        attacker = makeAddr("attacker");
        maintainer = makeAddr("faroswap-maintainer");

        stpros = new MockStPROS("stPROS", "stPROS");
        wpros = new MockERC20("WPROS", "WPROS");
        gspTemplate = new MockGSP();
        cloneFactory = new MockCloneFactory();
        factory = new MockGSPFactory(address(cloneFactory), address(gspTemplate), maintainer);

        impl = new FaroSwapPriceManager(
            address(stpros), address(wpros), address(factory), address(gspTemplate), LP_FEE, MT_FEE, K, PRICE_LIMIT
        );
        pm = FaroSwapPriceManager(
            address(new ERC1967Proxy(address(impl), abi.encodeCall(FaroSwapPriceManager.initialize, (safe))))
        );

        pool = factory.createDODOGasSavingPool(
            address(pm), address(stpros), address(wpros), LP_FEE, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        pm.bindPool(pool);
        vm.prank(safe);
        pm.setExecutor(executor, true);
    }

    // ---------------------------------------------------------------- helpers
    function _countPriceSyncedEvents() internal returns (uint256) {
        vm.recordLogs();
        vm.prank(executor);
        pm.syncPrice();
        Vm.Log[] memory entries = vm.getRecordedLogs();
        bytes32 topic = FaroSwapPriceManager.PriceSynced.selector;
        uint256 count;
        for (uint256 i = 0; i < entries.length; i++) {
            if (entries[i].topics.length > 0 && entries[i].topics[0] == topic) count += 1;
        }
        return count;
    }

    function _sync() internal {
        vm.prank(executor);
        pm.syncPrice();
    }

    // ---------------------------------------------------------------- initialization
    function test_initialize_setsSafeAsOwner() public view {
        assertEq(pm.owner(), safe);
        assertEq(pm.pendingOwner(), address(0));
        assertEq(pm.pool(), pool);
        assertTrue(pm.poolBound());
        assertTrue(pm.isExecutor(executor));
        assertFalse(pm.isExecutor(safe));
        assertEq(address(MockGSP(pool)._ADMIN_()), address(pm));
    }

    function test_implementationInitialize_isDisabled() public {
        vm.prank(attacker);
        vm.expectRevert(Initializable.InvalidInitialization.selector);
        impl.initialize(attacker);
    }

    function test_attackerProxy_cannotTouchOurStateOrPool() public {
        // A fresh ERC1967Proxy of the public implementation CAN run initialize once (the
        // disable in the constructor only guards the implementation's own storage), so an
        // attacker gets a private, fully owned manager instance. It must be powerless over
        // our instance: our owner and binding are untouched, and binding OUR pool from the
        // attacker's instance fails the admin check on-chain.
        FaroSwapPriceManager attackerPm = FaroSwapPriceManager(
            address(new ERC1967Proxy(address(impl), abi.encodeCall(FaroSwapPriceManager.initialize, (attacker))))
        );
        assertEq(attackerPm.owner(), attacker, "attacker owns only their own instance");
        assertEq(pm.owner(), safe, "our owner untouched");
        assertTrue(pm.poolBound(), "our binding untouched");

        vm.prank(attacker);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.PoolAdminMismatch.selector, address(pm), address(attackerPm)
            )
        );
        attackerPm.bindPool(pool);

        // Our implementation's own storage stays uninitialized: initialize reverts on it.
        vm.prank(attacker);
        vm.expectRevert(Initializable.InvalidInitialization.selector);
        impl.initialize(attacker);
    }

    // ---------------------------------------------------------------- executor authorization
    function test_unauthorizedSync_reverts() public {
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.NotExecutor.selector, attacker));
        pm.syncPrice();
    }

    function test_executorRevocation_blocksFurtherSync() public {
        vm.startPrank(safe);
        pm.setExecutor(executor, false);
        vm.stopPrank();
        assertFalse(pm.isExecutor(executor));
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.NotExecutor.selector, executor));
        pm.syncPrice();
    }

    function test_setExecutor_ownerOnlyAndZeroRejected() public {
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, executor));
        pm.setExecutor(attacker, true);

        vm.prank(safe);
        vm.expectRevert(FaroSwapPriceManager.ZeroAddress.selector);
        pm.setExecutor(address(0), true);
    }

    function test_setExecutor_emitsEvent() public {
        vm.expectEmit(true, true, true, true, address(pm));
        emit FaroSwapPriceManager.ExecutorUpdated(executor, false);
        vm.prank(safe);
        pm.setExecutor(executor, false);
    }

    event ExecutorUpdated(address indexed executor, bool allowed);

    // ---------------------------------------------------------------- synchronization
    function test_sync_movesPoolToTargetI() public {
        stpros.setRate(1005000000000000000); // +0.5%
        uint256 target = stpros.convertToAssets(1e18);
        vm.expectEmit(true, true, true, true, address(pm));
        emit FaroSwapPriceManager.PriceSynced(1e18, target);
        _sync();
        assertEq(MockGSP(pool)._I_(), target);
        assertEq(MockGSP(pool).adjustPriceCalls(), 1);
        assertEq(pm.targetI(), target);
    }

    function test_sync_isIdempotent_noopOnSamePrice() public {
        // rate == pool i (both 1e18): authorization and source validation still run, then a
        // clean return: no pool call, no state write, no event.
        assertEq(_countPriceSyncedEvents(), 0, "no PriceSynced event expected");
        assertEq(MockGSP(pool).adjustPriceCalls(), 0, "pool must not be called");
        assertEq(MockGSP(pool)._I_(), 1e18);
    }

    function test_noopStillRequiresAuthorization() public {
        // Same price on-chain, unauthorized caller: must revert, never silently no-op (AC-32).
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.NotExecutor.selector, attacker));
        pm.syncPrice();
    }

    function test_noopStillRequiresValidSource() public {
        // Source read fails with prices equal: the failure must propagate, not read as no-op.
        stpros.setRevertAlways(true);
        vm.prank(executor);
        vm.expectRevert("vault read failed");
        pm.syncPrice();
        stpros.setRevertAlways(false);
    }

    function test_sync_revertsWhenUnbound() public {
        FaroSwapPriceManager fresh = _deployManager();
        vm.prank(safe);
        fresh.setExecutor(executor, true);
        vm.prank(executor);
        vm.expectRevert(FaroSwapPriceManager.PoolNotBound.selector);
        fresh.syncPrice();
    }

    function test_sync_zeroRateRevertsCleanly() public {
        stpros.setRate(0);
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.InvalidTargetI.selector, 0));
        pm.syncPrice();
        assertEq(MockGSP(pool)._I_(), 1e18, "pool untouched");
        assertEq(MockGSP(pool).adjustPriceCalls(), 0, "pool not called");
    }

    function test_sync_oversizedRateRevertsCleanly() public {
        stpros.setRate(2 * 10 ** 36);
        uint256 target = stpros.convertToAssets(1e18); // 2e36 > MAX_I
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.InvalidTargetI.selector, target));
        pm.syncPrice();
    }

    function test_sync_exactLimitBoundary_passes() public {
        // exactly +1% with priceLimit 10000: floor(0.01e18 * 1e6 / 1e18) = 10000 <= 10000
        stpros.setRate(1010000000000000000);
        _sync();
        assertEq(MockGSP(pool)._I_(), 1010000000000000000);
    }

    function test_sync_aboveLimit_revertsAndLeavesNoPartialUpdate() public {
        stpros.setRate(1020000000000000000); // +2% > 1%
        vm.prank(executor);
        vm.expectRevert("EXCEED_PRICE_LIMIT");
        pm.syncPrice();
        assertEq(MockGSP(pool)._I_(), 1e18, "i unchanged");
        assertEq(MockGSP(pool).adjustPriceCalls(), 0, "revert rolled the attempt back");
    }

    function test_sync_largeJump_afterProlongedInterruption_AC24() public {
        stpros.setRate(1100000000000000000); // +10% accumulated drift
        vm.prank(executor);
        vm.expectRevert("EXCEED_PRICE_LIMIT");
        pm.syncPrice();

        // The Safe independently raises the ceiling, then the authorized executor retries.
        vm.prank(safe);
        pm.setPriceLimit(150000); // 15%
        _sync();
        assertEq(MockGSP(pool)._I_(), 1100000000000000000);
        assertEq(MockGSP(pool).adjustPriceCalls(), 1, "first attempt rolled back, second persisted");
    }

    function test_sync_matchesLiveMainnetRate_exactly() public {
        // The redemption rate observed at block 22918977 must flow through unchanged.
        stpros.setRate(1009531657654669604);
        uint256 target = stpros.convertToAssets(1e18);
        assertEq(target, 1009531657654669604);
        _sync();
        assertEq(MockGSP(pool)._I_(), target, "same economic definition as pool creation");
    }

    function test_sync_rstateNotOne_changesOnlyI() public {
        MockGSP(pool).setRState(2); // ABOVE_ONE
        (uint256 b0, uint256 q0, uint256 bT, uint256 qT, uint256 k, uint256 lpFee, uint256 mtFee) =
            _poolInvariantSnapshot();
        stpros.setRate(1004000000000000000); // +0.4%
        _sync();
        (uint256 b1, uint256 q1, uint256 bT1, uint256 qT1, uint256 k1, uint256 lpFee1, uint256 mtFee1) =
            _poolInvariantSnapshot();
        assertEq(MockGSP(pool)._I_(), 1004000000000000000, "i updated");
        assertEq(b0, b1, "base reserve unchanged");
        assertEq(q0, q1, "quote reserve unchanged");
        assertEq(bT, bT1, "stored base target unchanged");
        assertEq(qT, qT1, "stored quote target unchanged");
        assertEq(k, k1, "k unchanged");
        assertEq(lpFee, lpFee1, "lp fee unchanged");
        assertEq(mtFee, mtFee1, "mt fee unchanged");
        assertEq(MockGSP(pool)._RState_(), 2, "RState untouched");
    }

    function test_previewSync_reportsNeed() public {
        stpros.setRate(1005000000000000000);
        (uint256 target, uint256 current, bool needed) = pm.previewSync();
        assertEq(target, 1005000000000000000);
        assertEq(current, 1e18);
        assertTrue(needed);
        _sync();
        (,, bool neededAfter) = pm.previewSync();
        assertFalse(neededAfter);
    }

    function test_previewSync_unboundReverts() public {
        FaroSwapPriceManager fresh = _deployManager();
        vm.expectRevert(FaroSwapPriceManager.PoolNotBound.selector);
        fresh.previewSync();
    }

    // ---------------------------------------------------------------- price limit administration
    function test_setPriceLimit_updatesPool_andEmits() public {
        vm.expectEmit(true, true, true, true, address(pm));
        emit FaroSwapPriceManager.PriceLimitUpdated(500);
        vm.prank(safe);
        pm.setPriceLimit(500);
        assertEq(MockGSP(pool)._PRICE_LIMIT_(), 500);
        assertEq(MockGSP(pool).adjustPriceLimitCalls(), 1);
        // Only priceLimit changed (INV-7).
        assertEq(MockGSP(pool)._I_(), 1e18);
    }

    function test_setPriceLimit_acceptsBoundaries() public {
        vm.startPrank(safe);
        pm.setPriceLimit(0);
        assertEq(MockGSP(pool)._PRICE_LIMIT_(), 0);
        pm.setPriceLimit(1000000);
        assertEq(MockGSP(pool)._PRICE_LIMIT_(), 1000000);
        vm.stopPrank();
    }

    function test_setPriceLimit_aboveMax_revertsBeforeTouchingPool() public {
        vm.prank(safe);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.PriceLimitAboveMax.selector, 1000001));
        pm.setPriceLimit(1000001);
        assertEq(MockGSP(pool).adjustPriceLimitCalls(), 0, "pool never called");
        assertEq(MockGSP(pool)._PRICE_LIMIT_(), PRICE_LIMIT, "limit unchanged");
    }

    function test_setPriceLimit_rejectsExecutorAndStrangers() public {
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, executor));
        pm.setPriceLimit(1);
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, attacker));
        pm.setPriceLimit(1);
        assertEq(MockGSP(pool).adjustPriceLimitCalls(), 0);
    }

    function test_setPriceLimit_revertsWhenUnbound() public {
        FaroSwapPriceManager fresh = _deployManager();
        vm.prank(safe);
        vm.expectRevert(FaroSwapPriceManager.PoolNotBound.selector);
        fresh.setPriceLimit(1);
    }

    event PriceLimitUpdated(uint256 newLimit);
    event PriceSynced(uint256 oldI, uint256 newI);

    // ---------------------------------------------------------------- one-time pool binding
    function test_bindPool_isOneTime() public {
        address second = factory.createDODOGasSavingPool(
            address(pm), address(stpros), address(wpros), LP_FEE, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(FaroSwapPriceManager.PoolAlreadyBound.selector);
        pm.bindPool(second);
    }

    function test_bindPool_rejectsWrongAdmin() public {
        FaroSwapPriceManager fresh = _deployManager();
        address wrongAdmin = factory.createDODOGasSavingPool(
            attacker, address(stpros), address(wpros), LP_FEE, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.PoolAdminMismatch.selector, attacker, address(fresh)
            )
        );
        fresh.bindPool(wrongAdmin);
    }

    function test_bindPool_rejectsSwappedTokenDirection() public {
        FaroSwapPriceManager fresh = _deployManager();
        address swapped = factory.createDODOGasSavingPool(
            address(fresh), address(wpros), address(stpros), LP_FEE, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.PoolTokensMismatch.selector, address(wpros), address(stpros)
            )
        );
        fresh.bindPool(swapped);
    }

    function test_bindPool_rejectsNonCloneContract() public {
        FaroSwapPriceManager fresh = _deployManager();
        uint256 codeSize = address(stpros).code.length;
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPriceManager.PoolNotClone.selector, address(stpros), codeSize)
        );
        fresh.bindPool(address(stpros)); // a regular contract, not a 45-byte clone
    }

    function test_bindPool_rejectsCloneOfDifferentTemplate() public {
        FaroSwapPriceManager fresh = _deployManager();
        MockGSP otherTemplate = new MockGSP();
        address alien = cloneFactory.clone(address(otherTemplate));
        MockGSP(alien).init(
            maintainer, address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.PoolTemplateMismatch.selector, alien, address(otherTemplate), address(gspTemplate)
            )
        );
        fresh.bindPool(alien);
    }

    function test_bindPool_rejectsWhenFactoryTemplateRotated() public {
        FaroSwapPriceManager fresh = _deployManager();
        MockGSP otherTemplate = new MockGSP();
        factory.updateGSPTemplate(address(otherTemplate));
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.FactoryTemplateChanged.selector,
                address(otherTemplate),
                address(gspTemplate)
            )
        );
        fresh.bindPool(pool);
        factory.updateGSPTemplate(address(gspTemplate)); // restore for other tests
    }

    function test_bindPool_requiresFactoryRegistryMembership() public {
        FaroSwapPriceManager fresh = _deployManager();
        // A correct-template clone whose admin is us but which was NOT created through the
        // factory (raw clone + manual init) must not be bindable.
        address rogue = cloneFactory.clone(address(gspTemplate));
        MockGSP(rogue).init(
            maintainer, address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPriceManager.PoolNotInRegistry.selector, rogue));
        fresh.bindPool(rogue);
    }

    function test_bindPool_pinsCreationEconomics() public {
        FaroSwapPriceManager fresh = _deployManager();
        address wrongK = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 1e18, 3e15, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.PoolCreationParamsMismatch.selector, wrongK, LP_FEE, MT_FEE, 3e15, PRICE_LIMIT
            )
        );
        fresh.bindPool(wrongK);

        address wrongFee = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), 4e14, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.PoolCreationParamsMismatch.selector, wrongFee, 4e14, MT_FEE, K, PRICE_LIMIT
            )
        );
        fresh.bindPool(wrongFee);
    }

    function test_bindPool_rejectsAttackerPrecreatedHostilePool() public {
        FaroSwapPriceManager fresh = _deployManager();
        // Factory pool creation is permissionless: anyone can pre-create a stPROS/WPROS pool
        // with admin = our proxy and hostile economics, hoping we bind it. All creation
        // parameters are pinned at bind time, so the binding fails loudly.
        address hostile = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), 10 ** 18, 10 ** 18, 1, 10 ** 18, 1000000, false
        );
        assertEq(MockGSP(hostile)._ADMIN_(), address(fresh), "attacker did set our admin");
        vm.prank(safe);
        vm.expectRevert(); // PoolCreationParamsMismatch
        fresh.bindPool(hostile);
    }

    function test_bindPool_ownerOnly() public {
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, executor));
        pm.bindPool(pool); // even the correct pool: executor cannot bind
    }

    // ---------------------------------------------------------------- two-step ownership
    function test_twoStepOwnership_transferLifecycle() public {
        vm.prank(safe);
        pm.transferOwnership(newSafe);
        assertEq(pm.owner(), safe, "no early change");
        assertEq(pm.pendingOwner(), newSafe);

        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, attacker));
        pm.acceptOwnership();
        assertEq(pm.owner(), safe);

        vm.prank(newSafe);
        pm.acceptOwnership();
        assertEq(pm.owner(), newSafe);
        assertEq(pm.pendingOwner(), address(0));
    }

    function test_ownership_neverAccessibleToExecutor() public {
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, executor));
        pm.transferOwnership(attacker);
    }

    function test_renounceOwnership_disabled() public {
        vm.prank(safe);
        vm.expectRevert(FaroSwapPriceManager.OwnershipRenounceDisabled.selector);
        pm.renounceOwnership();
    }

    // ---------------------------------------------------------------- upgrades
    function test_upgrade_preservesAllState_andSyncStillWorks() public {
        FaroSwapPriceManagerV2 v2 = new FaroSwapPriceManagerV2(
            address(stpros), address(wpros), address(factory), address(gspTemplate), LP_FEE, MT_FEE, K, PRICE_LIMIT
        );
        vm.prank(executor);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, executor));
        pm.upgradeToAndCall(address(v2), "");

        vm.prank(safe);
        pm.upgradeToAndCall(address(v2), "");

        assertEq(
            uint256(vm.load(address(pm), IMPL_SLOT)), uint256(uint160(address(v2))), "implementation slot"
        );
        assertEq(pm.owner(), safe, "owner preserved");
        assertTrue(pm.isExecutor(executor), "executor preserved");
        assertTrue(pm.poolBound(), "binding preserved");
        assertEq(pm.pool(), pool, "pool preserved");
        assertEq(FaroSwapPriceManagerV2(address(pm)).MARKER(), "faroswap-price-manager-v2");
        assertEq(address(MockGSP(pool)._ADMIN_()), address(pm), "pool admin association unchanged");

        stpros.setRate(1003000000000000000); // +0.3%
        _sync();
        assertEq(MockGSP(pool)._I_(), 1003000000000000000, "sync works after upgrade");
    }

    function test_upgrade_rejectsEmptyImplementation() public {
        address eoa = makeAddr("not-a-contract");
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPriceManager.UnsupportedProxiableUUID.selector, eoa)
        );
        pm.upgradeToAndCall(eoa, "");
    }

    function test_upgrade_rejectsNonProxiableImplementation() public {
        // A contract without the ERC-1822 signal is not a UUPS target for this proxy.
        NotProxiable candidate = new NotProxiable();
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPriceManager.UnsupportedProxiableUUID.selector, address(candidate))
        );
        pm.upgradeToAndCall(address(candidate), "");
        assertEq(
            uint256(vm.load(address(pm), IMPL_SLOT)),
            uint256(uint160(address(impl))),
            "implementation slot untouched"
        );
    }

    function test_upgrade_rejectsWrongProxiableUUID() public {
        // A proxiableUUID that does not name the ERC-1967 implementation slot is rejected;
        // state and binding remain intact.
        WrongProxiableUUID candidate = new WrongProxiableUUID();
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPriceManager.UnsupportedProxiableUUID.selector, address(candidate))
        );
        pm.upgradeToAndCall(address(candidate), "");
        assertTrue(pm.poolBound(), "binding untouched");
        assertTrue(pm.isExecutor(executor), "executor untouched");
    }

    function test_upgrade_emitsUpgradedEvent() public {
        FaroSwapPriceManagerV2 v2 = new FaroSwapPriceManagerV2(
            address(stpros), address(wpros), address(factory), address(gspTemplate), LP_FEE, MT_FEE, K, PRICE_LIMIT
        );
        vm.expectEmit(true, true, true, true, address(pm));
        emit Upgraded(address(v2));
        vm.prank(safe);
        pm.upgradeToAndCall(address(v2), "");
    }

    event Upgraded(address indexed implementation);

    // ---------------------------------------------------------------- ERC-1822 signal and call-context
    function test_proxiableUUID_directOnImplementation_returnsSlot() public view {
        assertEq(
            impl.proxiableUUID(),
            0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc
        );
    }

    function test_proxiableUUID_revertsThroughAnyProxy() public {
        // The signal must not be callable via delegatecall: it certifies implementations,
        // not proxies answering on their behalf.
        vm.expectRevert(FaroSwapPriceManager.UUPSUnauthorizedCallContext.selector);
        pm.proxiableUUID();

        FaroSwapPriceManagerV2 v2 = _deployV2();
        address otherProxy = address(
            new ERC1967Proxy(address(v2), abi.encodeCall(FaroSwapPriceManager.initialize, (attacker)))
        );
        vm.expectRevert(FaroSwapPriceManager.UUPSUnauthorizedCallContext.selector);
        FaroSwapPriceManagerV2(otherProxy).proxiableUUID();
    }

    function test_upgradeToAndCall_revertsWhenCalledDirectlyOnImplementation() public {
        // Direct calls on the bare implementation can never move the proxy: the context guard
        // fires before ownership is even consulted.
        vm.prank(safe);
        vm.expectRevert(FaroSwapPriceManager.UUPSUnauthorizedCallContext.selector);
        impl.upgradeToAndCall(address(impl), "");
    }

    function test_upgrade_rejectsProxyAndCloneTargets() public {
        FaroSwapPriceManagerV2 v2 = _deployV2();
        // A full ERC-1967 proxy in front of a compliant implementation (it initializes on its
        // own storage) and an EIP-1167 clone of the same implementation: both must be refused
        // as upgrade targets because the delegated proxiableUUID reverts.
        address asProxy = address(
            new ERC1967Proxy(address(v2), abi.encodeCall(FaroSwapPriceManager.initialize, (attacker)))
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPriceManager.UnsupportedProxiableUUID.selector, asProxy)
        );
        pm.upgradeToAndCall(asProxy, "");

        address asClone = address(cloneFactory.clone(address(v2)));
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPriceManager.UnsupportedProxiableUUID.selector, asClone)
        );
        pm.upgradeToAndCall(asClone, "");

        // Nothing moved.
        assertEq(uint256(vm.load(address(pm), IMPL_SLOT)), uint256(uint160(address(impl))));
    }

    function test_upgrade_rejectsSelfReferentialProxyTarget() public {
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPriceManager.UpgradeToSelfProxy.selector, address(pm))
        );
        pm.upgradeToAndCall(address(pm), "");
    }

    function test_upgradeForwardChain_thenRollbackToV1() public {
        FaroSwapPriceManagerV2 v2 = _deployV2();
        FaroSwapPriceManagerV3 v3 = new FaroSwapPriceManagerV3(
            address(stpros), address(wpros), address(factory), address(gspTemplate), LP_FEE, MT_FEE, K, PRICE_LIMIT
        );

        vm.startPrank(safe);
        pm.upgradeToAndCall(address(v2), "");
        assertEq(FaroSwapPriceManagerV2(address(pm)).MARKER(), "faroswap-price-manager-v2");

        pm.upgradeToAndCall(address(v3), "");
        assertEq(FaroSwapPriceManagerV3(address(pm)).MARKER_V3(), "faroswap-price-manager-v3");
        assertEq(uint256(vm.load(address(pm), IMPL_SLOT)), uint256(uint160(address(v3))));

        // State survives the whole forward chain.
        assertEq(pm.owner(), safe);
        assertTrue(pm.isExecutor(executor));
        assertEq(pm.pool(), pool);
        stpros.setRate(1002000000000000000); // +0.2%
        vm.stopPrank();
        _sync();
        assertEq(MockGSP(pool)._I_(), 1002000000000000000, "sync works on V3");

        // Rollback to the original implementation: the guarded signal makes V1 a valid
        // candidate again, and every invariant survives the downgrade.
        vm.prank(safe);
        pm.upgradeToAndCall(address(impl), "");
        assertEq(uint256(vm.load(address(pm), IMPL_SLOT)), uint256(uint160(address(impl))));
        assertEq(pm.owner(), safe, "owner preserved across rollback");
        assertTrue(pm.isExecutor(executor), "executor preserved across rollback");
        assertTrue(pm.poolBound(), "binding preserved across rollback");
        assertEq(pm.pool(), pool, "pool preserved across rollback");
        vm.expectRevert(); // V2-only marker no longer present through the proxy
        FaroSwapPriceManagerV2(address(pm)).MARKER();
        stpros.setRate(1003000000000000000); // another +0.1% step
        _sync();
        assertEq(MockGSP(pool)._I_(), 1003000000000000000, "sync works after rollback");
    }

    // ---------------------------------------------------------------- pre-binding safety
    function test_bindPool_rejectsSeededPool() public {
        FaroSwapPriceManager fresh = _deployManager();
        address target = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        // A front-run first deposit seeds the pool before the Safe can bind.
        stpros.mint(attacker, 2 * 10 ** 15);
        wpros.mint(attacker, 2 * 10 ** 15);
        vm.startPrank(attacker);
        stpros.transfer(target, 2 * 10 ** 15);
        wpros.transfer(target, 2 * 10 ** 15);
        MockGSP(target).buyShares(attacker);
        vm.stopPrank();

        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPriceManager.PoolNotEmpty.selector, 2 * 10 ** 15)
        );
        fresh.bindPool(target);
        assertFalse(fresh.poolBound(), "nothing bound");

        // Recovery: abandon the seeded pool, create and bind a fresh one (runbook path).
        address replacement = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 1e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        fresh.bindPool(replacement);
        assertTrue(fresh.poolBound());
    }

    function test_bindPool_rejectsInitialIPriceDeviation() public {
        FaroSwapPriceManager fresh = _deployManager();
        // 5% below the live rate: the kind of stale or hostile initial i that would
        // permanently misprice the pool.
        address off5 = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 0.95e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.BindPriceDeviation.selector, 0.95e18, 1e18, PRICE_LIMIT
            )
        );
        fresh.bindPool(off5);

        // Just outside the pool's own 1% single-sync step.
        address off11 = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 1.011e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.BindPriceDeviation.selector, 1.011e18, 1e18, PRICE_LIMIT
            )
        );
        fresh.bindPool(off11);
        assertFalse(fresh.poolBound());
    }

    function test_bindPool_belowOracle_denominatorAsymmetry_rejected() public {
        // vaultRate = 1e18, poolI = 0.99e18: a vault-denominated check reads exactly 10000,
        // but the pool's first adjustPrice divides by the SMALLER old price (10101) — such a
        // pool could never synchronize. The bind must use the pool-price denominator.
        FaroSwapPriceManager fresh = _deployManager();
        address below = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 0.99e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPriceManager.BindPriceDeviation.selector, 0.99e18, 1e18, PRICE_LIMIT
            )
        );
        fresh.bindPool(below);
        assertFalse(fresh.poolBound());
    }

    function test_bindPool_acceptedBelowOraclePrice_syncsSuccessfully() public {
        // poolI = 0.991e18 (~0.9% below oracle, relative step 9081/1e6): binds, and the first
        // synchronization lifts i to the oracle rate inside the pool's own 1% limit.
        FaroSwapPriceManager fresh = _deployManager();
        address below = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 0.991e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        fresh.bindPool(below);
        assertTrue(fresh.poolBound());
        vm.prank(safe);
        fresh.setExecutor(executor, true);
        vm.prank(executor);
        fresh.syncPrice();
        assertEq(MockGSP(below)._I_(), 1e18, "synced up to the oracle rate");
    }

    function test_bindPool_acceptedAboveOraclePrice_syncsSuccessfully() public {
        // Above-oracle initial prices remain valid (relative step 8919/1e6) and synchronize
        // downward on the first call.
        FaroSwapPriceManager fresh = _deployManager();
        address above = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 1.009e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        fresh.bindPool(above);
        assertTrue(fresh.poolBound());
        vm.prank(safe);
        fresh.setExecutor(executor, true);
        vm.prank(executor);
        fresh.syncPrice();
        assertEq(MockGSP(above)._I_(), 1e18, "synced down to the oracle rate");
    }

    function test_bindPool_acceptsInitialIWithinOneSyncStep() public {
        FaroSwapPriceManager fresh = _deployManager();
        // 0.5% off (plausible oracle drift between creation and binding): accepted.
        address drift = factory.createDODOGasSavingPool(
            address(fresh), address(stpros), address(wpros), LP_FEE, MT_FEE, 1.005e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        fresh.bindPool(drift);
        assertTrue(fresh.poolBound());

        // Exact 1% boundary mirrors the GSP's own <= priceLimit semantics: accepted.
        FaroSwapPriceManager fresh2 = _deployManager();
        address edge = factory.createDODOGasSavingPool(
            address(fresh2), address(stpros), address(wpros), LP_FEE, MT_FEE, 1.01e18, K, PRICE_LIMIT, false
        );
        vm.prank(safe);
        fresh2.bindPool(edge);
        assertTrue(fresh2.poolBound());
    }

    // ---------------------------------------------------------------- storage layout pinning
    function test_storageLayout_pinnedAtErc7201Namespace() public {
        // isExecutor mapping at STORAGE, pool at +1, poolBound at +2. Any future
        // implementation must keep this layout; this test fails loudly if it drifts.
        bytes32 execSlot = keccak256(abi.encode(executor, PM_STORAGE));
        assertEq(uint256(vm.load(address(pm), execSlot)), 1, "executor flag slot");

        // address (20 bytes) and bool (1 byte) share slot STORAGE+1; the mapping occupies
        // STORAGE. Future implementations must keep this packed layout.
        uint256 packed = uint256(uint160(pool)) | (uint256(1) << 160);
        assertEq(uint256(vm.load(address(pm), bytes32(uint256(PM_STORAGE) + 1))), packed, "pool|poolBound slot");
    }

    // ---------------------------------------------------------------- helpers
    function _deployV2() internal returns (FaroSwapPriceManagerV2) {
        return new FaroSwapPriceManagerV2(
            address(stpros), address(wpros), address(factory), address(gspTemplate), LP_FEE, MT_FEE, K, PRICE_LIMIT
        );
    }

    function _deployManager() internal returns (FaroSwapPriceManager) {
        FaroSwapPriceManager i = new FaroSwapPriceManager(
            address(stpros), address(wpros), address(factory), address(gspTemplate), LP_FEE, MT_FEE, K, PRICE_LIMIT
        );
        return FaroSwapPriceManager(
            address(new ERC1967Proxy(address(i), abi.encodeCall(FaroSwapPriceManager.initialize, (safe))))
        );
    }

    function _poolInvariantSnapshot()
        internal
        view
        returns (uint256 bReserve, uint256 qReserve, uint256 bTarget, uint256 qTarget, uint256 k, uint256 lpFee, uint256 mtFee)
    {
        MockGSP p = MockGSP(pool);
        (bReserve, qReserve) = p.getVaultReserve();
        bTarget = p._BASE_TARGET_();
        qTarget = p._QUOTE_TARGET_();
        k = p._K_();
        lpFee = p._LP_FEE_RATE_();
        mtFee = p._MT_FEE_RATE_();
    }
}

contract NotProxiable {
    function somethingElse() external pure returns (uint256) {
        return 1;
    }
}

contract WrongProxiableUUID {
    function proxiableUUID() external pure returns (bytes32) {
        return bytes32(uint256(1));
    }
}
