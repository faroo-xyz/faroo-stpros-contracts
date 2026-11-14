// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {FaroSwapPriceManager} from "../contracts/FaroSwapPriceManager.sol";
import {FaroSwapPriceManagerV2} from "../tests/mocks/FaroSwapPriceManagerV2.sol";
import {IFaroSwapGSP} from "../contracts/interfaces/IFaroSwapGSP.sol";
import {IFaroSwapGSPFactory} from "../contracts/interfaces/IFaroSwapGSPFactory.sol";

interface IMainnetVault {
    function convertToAssets(uint256 shares) external view returns (uint256);
}

/// @title Safe-controlled upgrade rehearsal on a Pharos mainnet fork.
/// @notice Deploys the full manager + pool flow, then performs the owner-only proxy upgrade
///         to a storage-compatible V2 and revalidates the whole system afterwards (spec AC-16):
///         proxy address and pool-admin association unchanged, owner/executors/binding
///         preserved, sync still converges, and non-owners still cannot upgrade.
contract FaroSwapUpgradeRehearsalTest is Test {
    address internal constant STPROS = 0x6b0A44c64190279f7034b77c13a566E914FE5Ec4;
    address internal constant WPROS = 0x52C48d4213107b20bC583832b0d951FB9CA8F0B0;
    address internal constant GSP_FACTORY = 0xD15f2D50bbf777EFD529052686d0703e498F8380;
    address internal constant GSP_TEMPLATE = 0x81B04C62c53a716B7b5ce7fE8de42ab665BcA935;
    address internal constant GOV_SAFE = 0x3658e00f5DDb9Fa0c7e9820e8d16C3C17eaB73CA;
    address internal constant DEPLOYER = 0x8Ce84E9Fa0101D317D8956D73610ad3e0E219d41;

    uint256 internal constant LP_FEE = 500000000000000;
    uint256 internal constant MT_FEE = 0;
    uint256 internal constant K = 2000000000000000;
    uint256 internal constant PRICE_LIMIT = 10000;
    bytes32 internal constant IMPL_SLOT =
        0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc;
    bytes32 internal constant PM_STORAGE =
        0x1b7714590d5370353247b3299283b707765d01564ce2954820f2462bb8b85f00;

    /// @dev Recorded, reproducible fork pin (regression block for the remediation run,
    ///         2026-11-13). Override with FAROO_FORK_PIN for newer blocks.
    uint256 internal constant FORK_PIN = 22921747;

    string internal url;
    address internal executor = makeAddr("sync-executor");
    FaroSwapPriceManager internal impl;
    FaroSwapPriceManager internal pm;
    address internal pool;

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
        uint256 rate = IMainnetVault(STPROS).convertToAssets(10 ** 18);
        vm.prank(DEPLOYER);
        pool = IFaroSwapGSPFactory(GSP_FACTORY).createDODOGasSavingPool(
            address(pm), STPROS, WPROS, LP_FEE, MT_FEE, rate, K, PRICE_LIMIT, false
        );
        vm.prank(GOV_SAFE);
        pm.bindPool(pool);
        vm.prank(GOV_SAFE);
        pm.setExecutor(executor, true);
    }

    modifier forkOnly() {
        if (bytes(url).length == 0) vm.skip(true);
        _;
    }

    function test_upgradeFlow_preservesSystemAndReenablesSync() public forkOnly {
        FaroSwapPriceManagerV2 v2 = new FaroSwapPriceManagerV2(
            STPROS, WPROS, GSP_FACTORY, GSP_TEMPLATE, LP_FEE, MT_FEE, K, PRICE_LIMIT
        );

        // Neither the authorized executor nor a stranger may upgrade.
        vm.prank(executor);
        vm.expectRevert();
        pm.upgradeToAndCall(address(v2), "");
        vm.prank(address(0xBEEF));
        vm.expectRevert();
        pm.upgradeToAndCall(address(v2), "");

        vm.prank(GOV_SAFE);
        pm.upgradeToAndCall(address(v2), "");

        assertEq(uint256(vm.load(address(pm), IMPL_SLOT)), uint256(uint160(address(v2))), "impl slot");
        assertEq(FaroSwapPriceManagerV2(address(pm)).MARKER(), "faroswap-price-manager-v2");
        assertEq(pm.owner(), GOV_SAFE, "owner preserved");
        assertTrue(pm.isExecutor(executor), "executor preserved");
        assertTrue(pm.poolBound(), "binding preserved");
        assertEq(pm.pool(), pool, "pool preserved");
        assertEq(IFaroSwapGSP(pool)._ADMIN_(), address(pm), "pool admin association unchanged");

        // Storage layout continuity at the pinned namespace.
        bytes32 execSlot = keccak256(abi.encode(executor, PM_STORAGE));
        assertEq(uint256(vm.load(address(pm), execSlot)), 1, "executor flag slot");

        // Post-upgrade sync: same price -> no-op; the system still functions end to end.
        vm.prank(executor);
        pm.syncPrice();
        assertEq(IFaroSwapGSP(pool)._I_(), IMainnetVault(STPROS).convertToAssets(10 ** 18), "rate equality");

        // Price-limit administration still Safe-only after the upgrade.
        vm.prank(executor);
        vm.expectRevert();
        pm.setPriceLimit(500);
        vm.prank(GOV_SAFE);
        pm.setPriceLimit(500);
        assertEq(IFaroSwapGSP(pool)._PRICE_LIMIT_(), 500);
    }
}
