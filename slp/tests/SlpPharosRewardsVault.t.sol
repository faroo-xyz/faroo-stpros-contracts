// SPDX-License-Identifier: GPL-3.0
pragma solidity 0.8.28;

import "../contracts/pharos/SlpPharosRewardsVault.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import "forge-std/Test.sol";

/// @notice Minimal mock of the vToken oracle that records `update` calls.
contract MockOracle {
    address public lastToken;
    uint256 public lastAmount;
    uint256 public totalAmount;
    uint256 public updateCallCount;

    function update(address token, uint256 tokenAmount) external {
        lastToken = token;
        lastAmount = tokenAmount;
        totalAmount += tokenAmount;
        ++updateCallCount;
    }
}

contract SlpPharosRewardsVaultTest is Test {
    SlpPharosRewardsVault public vault;
    MockOracle public oracle;

    address public owner;
    address public alice;
    address public whitelistedUser;
    address public nonWhitelistedUser;
    address public wpros;
    address public slpPharosProxy;

    address[] public whitelists = new address[](1);

    bytes32 public constant WHITELIST_ROLE = keccak256("WHITELIST_ROLE");
    bytes32 public constant DEFAULT_ADMIN_ROLE = 0x00;

    function setUp() public {
        owner = makeAddr("owner");
        alice = makeAddr("alice");
        whitelistedUser = makeAddr("whitelistedUser");
        nonWhitelistedUser = makeAddr("nonWhitelistedUser");
        wpros = makeAddr("wpros");
        slpPharosProxy = makeAddr("slpPharosProxy");

        oracle = new MockOracle();
        whitelists[0] = whitelistedUser;

        vault = _deployVault();
        vault.initialize(owner, whitelists, wpros, address(oracle));

        // initialize leaves slpPharosProxy unset; wire it as the deploy scripts do.
        vm.prank(owner);
        vault.setSlpPharosProxy(slpPharosProxy);

        vm.deal(address(vault), 100 ether);
    }

    /// @dev The implementation disables initializers in its constructor, so tests
    ///      must interact through an ERC1967 proxy, mirroring production deployment.
    function _deployVault() internal returns (SlpPharosRewardsVault) {
        SlpPharosRewardsVault implementation = new SlpPharosRewardsVault();
        return SlpPharosRewardsVault(payable(address(new ERC1967Proxy(address(implementation), ""))));
    }

    /* -------------------------------------------------------------------------- */
    /*                                 initialize                                  */
    /* -------------------------------------------------------------------------- */

    function test_initializeSuccessfully() public {
        address[] memory newWhitelists = new address[](2);
        newWhitelists[0] = alice;
        newWhitelists[1] = whitelistedUser;

        SlpPharosRewardsVault newVault = _deployVault();
        newVault.initialize(owner, newWhitelists, wpros, address(oracle));

        assertTrue(newVault.hasRole(DEFAULT_ADMIN_ROLE, owner));
        assertTrue(newVault.hasRole(WHITELIST_ROLE, alice));
        assertTrue(newVault.hasRole(WHITELIST_ROLE, whitelistedUser));
        assertEq(newVault.getWPROS(), wpros);
        assertEq(newVault.getVTokenOracle(), address(oracle));
        // slpPharosProxy is not part of initialize; it stays unset until wired.
        assertEq(newVault.getSlpPharosProxy(), address(0));
    }

    function test_initializeRevertsWithZeroOwner() public {
        SlpPharosRewardsVault newVault = _deployVault();
        vm.expectRevert(abi.encodeWithSelector(SlpPharosRewardsVault.InvalidAddress.selector));
        newVault.initialize(address(0), whitelists, wpros, address(oracle));
    }

    function test_initializeRevertsWithZeroWPROS() public {
        SlpPharosRewardsVault newVault = _deployVault();
        vm.expectRevert(abi.encodeWithSelector(SlpPharosRewardsVault.InvalidAddress.selector));
        newVault.initialize(owner, whitelists, address(0), address(oracle));
    }

    function test_initializeRevertsWithZeroOracle() public {
        SlpPharosRewardsVault newVault = _deployVault();
        vm.expectRevert(abi.encodeWithSelector(SlpPharosRewardsVault.InvalidAddress.selector));
        newVault.initialize(owner, whitelists, wpros, address(0));
    }

    function test_initializeRevertsWithZeroWhitelistAddress() public {
        address[] memory invalidWhitelists = new address[](1);
        invalidWhitelists[0] = address(0);
        SlpPharosRewardsVault newVault = _deployVault();
        vm.expectRevert(abi.encodeWithSelector(SlpPharosRewardsVault.InvalidAddress.selector));
        newVault.initialize(owner, invalidWhitelists, wpros, address(oracle));
    }

    function test_cannotInitializeTwice() public {
        vm.expectRevert(abi.encodeWithSignature("InvalidInitialization()"));
        vault.initialize(owner, whitelists, wpros, address(oracle));
    }

    /* -------------------------------------------------------------------------- */
    /*                                notifyOracle                                */
    /* -------------------------------------------------------------------------- */

    function test_notifyOracleSuccessfully() public {
        uint256 amount = address(vault).balance;
        uint256 proxyBalanceBefore = slpPharosProxy.balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, false, false, true);
        emit SlpPharosRewardsVault.OracleNotified(whitelistedUser, amount);
        vault.notifyOracle();
        vm.stopPrank();

        // Balance is reported to the oracle and forwarded in full to the proxy.
        assertEq(oracle.updateCallCount(), 1);
        assertEq(oracle.lastToken(), wpros);
        assertEq(oracle.lastAmount(), amount);
        assertEq(slpPharosProxy.balance, proxyBalanceBefore + amount);
        assertEq(address(vault).balance, 0);
    }

    /// @dev Sweeping the balance on every call keeps `IOracle.update` delta semantics:
    ///      each notification reports only rewards received since the previous one.
    function test_notifyOracleReportsOnlyNewRewards() public {
        uint256 firstRewards = address(vault).balance;

        vm.prank(whitelistedUser);
        vault.notifyOracle();

        // A second notification without new rewards reports zero.
        vm.prank(whitelistedUser);
        vault.notifyOracle();
        assertEq(oracle.lastAmount(), 0);

        // New rewards arrive; only they are reported.
        uint256 newRewards = 7 ether;
        vm.deal(address(vault), newRewards);
        vm.prank(whitelistedUser);
        vault.notifyOracle();

        assertEq(oracle.lastAmount(), newRewards);
        assertEq(oracle.totalAmount(), firstRewards + newRewards);
        assertEq(slpPharosProxy.balance, firstRewards + newRewards);
    }

    function test_notifyOracleRevertsWhenSlpPharosProxyNotSet() public {
        // A freshly initialized vault has no proxy wired yet; forwarding must not
        // burn the balance to the zero address nor touch the oracle.
        SlpPharosRewardsVault newVault = _deployVault();
        newVault.initialize(owner, whitelists, wpros, address(oracle));
        vm.deal(address(newVault), 5 ether);

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosRewardsVault.InvalidAddress.selector));
        newVault.notifyOracle();
        vm.stopPrank();

        assertEq(address(newVault).balance, 5 ether);
        assertEq(address(0).balance, 0);
        assertEq(oracle.updateCallCount(), 0);
    }

    function test_notifyOracleRevertsWhenNotWhitelisted() public {
        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        vault.notifyOracle();
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                              setSlpPharosProxy                             */
    /* -------------------------------------------------------------------------- */

    function test_setSlpPharosProxySuccessfully() public {
        address newProxy = makeAddr("newProxy");

        vm.startPrank(owner);
        vm.expectEmit(true, true, false, false);
        emit SlpPharosRewardsVault.SlpPharosProxySet(owner, newProxy);
        vault.setSlpPharosProxy(newProxy);
        vm.stopPrank();

        assertEq(vault.getSlpPharosProxy(), newProxy);

        // Rewards notified after the update flow to the new proxy address.
        uint256 amount = address(vault).balance;
        vm.prank(whitelistedUser);
        vault.notifyOracle();
        assertEq(newProxy.balance, amount);
        assertEq(slpPharosProxy.balance, 0);
    }

    function test_setSlpPharosProxyRevertsWithZeroAddress() public {
        vm.startPrank(owner);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosRewardsVault.InvalidAddress.selector));
        vault.setSlpPharosProxy(address(0));
        vm.stopPrank();

        // The previously wired proxy is untouched.
        assertEq(vault.getSlpPharosProxy(), slpPharosProxy);
    }

    function test_setSlpPharosProxyRevertsWhenNotAdmin() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", whitelistedUser, DEFAULT_ADMIN_ROLE
            )
        );
        vault.setSlpPharosProxy(makeAddr("newProxy"));
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                            receive / roles                                 */
    /* -------------------------------------------------------------------------- */

    function test_receiveEth() public {
        uint256 amount = 10 ether;
        address sender = makeAddr("sender");
        vm.deal(sender, amount);

        uint256 balanceBefore = address(vault).balance;

        vm.startPrank(sender);
        vm.expectEmit(true, false, false, true);
        emit SlpPharosRewardsVault.EthDeposited(sender, amount);
        (bool success,) = address(vault).call{value: amount}("");
        require(success, "ETH transfer failed");
        vm.stopPrank();

        assertEq(address(vault).balance, balanceBefore + amount);
    }

    function test_grantWhitelistRole() public {
        vm.startPrank(owner);
        vault.grantRole(WHITELIST_ROLE, alice);
        assertTrue(vault.hasRole(WHITELIST_ROLE, alice));
        vm.stopPrank();
    }

    function test_revokeWhitelistRole() public {
        vm.startPrank(owner);
        vault.revokeRole(WHITELIST_ROLE, whitelistedUser);
        assertFalse(vault.hasRole(WHITELIST_ROLE, whitelistedUser));
        vm.stopPrank();
    }

    function test_grantRoleRevertsWhenNotAdmin() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", whitelistedUser, DEFAULT_ADMIN_ROLE
            )
        );
        vault.grantRole(WHITELIST_ROLE, alice);
        vm.stopPrank();
    }
}
