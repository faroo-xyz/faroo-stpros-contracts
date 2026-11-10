// SPDX-License-Identifier: GPL-3.0
pragma solidity 0.8.28;

import "../contracts/pharos/SlpPharosProxy.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import "forge-std/Test.sol";

/// @notice Minimal mock of the Pharos Staking precompile.
/// @dev Deployed once, then etched at the constant STAKING address so the proxy's
///      hard-coded `STAKING` calls resolve to this mock. Configurable via setters.
contract MockStaking {
    uint256 public undelegateAmount;
    uint256 public undelegateUnlockEpoch;
    uint256 public claimStakeAmount;
    uint256 public claimRewardAmount;

    uint256 public lastDelegatedValue;
    bytes32 public lastPoolId;

    /// @dev Pools forced to a zero reward regardless of `claimRewardAmount`.
    mapping(bytes32 => bool) public zeroReward;

    function setZeroReward(bytes32 poolId, bool isZero) external {
        zeroReward[poolId] = isZero;
    }

    function _rewardOf(bytes32 poolId) internal view returns (uint256) {
        return zeroReward[poolId] ? 0 : claimRewardAmount;
    }

    /// @dev Mirrors Pharos' view of the claimable reward used by the proxy to skip zero-reward pools.
    function getDelegatorReward(bytes32 poolId, address) external view returns (uint256) {
        return _rewardOf(poolId);
    }

    function setUndelegateResult(uint256 amount, uint256 unlockEpoch) external {
        undelegateAmount = amount;
        undelegateUnlockEpoch = unlockEpoch;
    }

    function setClaimStakeAmount(uint256 amount) external {
        claimStakeAmount = amount;
    }

    function setClaimRewardAmount(uint256 amount) external {
        claimRewardAmount = amount;
    }

    function delegate(bytes32 poolId) external payable {
        lastPoolId = poolId;
        lastDelegatedValue = msg.value;
    }

    function undelegate(bytes32 poolId) external returns (uint256, uint256) {
        lastPoolId = poolId;
        return (undelegateAmount, undelegateUnlockEpoch);
    }

    function claimStake(bytes32 poolId) external {
        lastPoolId = poolId;
        if (claimStakeAmount > 0) {
            (bool ok,) = msg.sender.call{value: claimStakeAmount}("");
            require(ok, "claimStake transfer failed");
        }
    }

    /// @dev Mirrors Pharos: `claimReward` only returns the amount (and emits `CoinMinted` on chain). The minted
    ///      coins are credited by the node *after* the transaction, so no value is transferred here, and a zero
    ///      reward reverts exactly like the real contract does.
    function claimReward(bytes32 poolId) external returns (uint256) {
        lastPoolId = poolId;
        uint256 reward = _rewardOf(poolId);
        require(reward > 0, "No rewards to claim");
        return reward;
    }

    function compoundRewards(bytes32 poolId) external {
        lastPoolId = poolId;
    }

    function settleReward(bytes32 poolId) external {
        lastPoolId = poolId;
    }

    receive() external payable {}
}

contract SlpPharosProxyTest is Test {
    SlpPharosProxy public slpPharosProxy;

    address public owner;
    address public alice;
    address public whitelistedUser;
    address public nonWhitelistedUser;
    address public bridgeVault;
    address public rewardsVault;

    address[] public whitelists = new address[](1);

    bytes32 public constant WHITELIST_ROLE = keccak256("WHITELIST_ROLE");
    bytes32 public constant DEFAULT_ADMIN_ROLE = 0x00;

    bytes32 public poolId = keccak256("poolId");
    bytes32 public poolId2 = keccak256("poolId2");

    // Same hard-coded Pharos Staking address used by the proxy.
    address public constant STAKING_ADDR = 0x4100000000000000000000000000000000000000;

    function setUp() public {
        owner = makeAddr("owner");
        alice = makeAddr("alice");
        whitelistedUser = makeAddr("whitelistedUser");
        nonWhitelistedUser = makeAddr("nonWhitelistedUser");
        bridgeVault = makeAddr("bridgeVault");
        rewardsVault = makeAddr("rewardsVault");

        // Install the mock staking contract at the constant STAKING address.
        MockStaking mock = new MockStaking();
        vm.etch(STAKING_ADDR, address(mock).code);

        whitelists[0] = whitelistedUser;

        slpPharosProxy = _deployProxy();
        slpPharosProxy.initialize(owner, whitelists, bridgeVault, rewardsVault);

        vm.deal(owner, 1000 ether);
        vm.deal(whitelistedUser, 1000 ether);
        vm.deal(address(slpPharosProxy), 100 ether);

        // Register a pool id for the bonding/unbonding/claiming flows.
        vm.prank(whitelistedUser);
        slpPharosProxy.registerPoolId(poolId);
    }

    function _staking() internal pure returns (MockStaking) {
        return MockStaking(payable(STAKING_ADDR));
    }

    /// @dev The implementation disables initializers in its constructor, so tests
    ///      must interact through an ERC1967 proxy, mirroring production deployment.
    function _deployProxy() internal returns (SlpPharosProxy) {
        SlpPharosProxy implementation = new SlpPharosProxy();
        return SlpPharosProxy(payable(address(new ERC1967Proxy(address(implementation), ""))));
    }

    /// @dev Registers a second pool and returns [poolId, poolId2] for the batch flows.
    function _twoPools() internal returns (bytes32[] memory poolIds) {
        vm.prank(whitelistedUser);
        slpPharosProxy.registerPoolId(poolId2);

        poolIds = new bytes32[](2);
        poolIds[0] = poolId;
        poolIds[1] = poolId2;
    }

    /* -------------------------------------------------------------------------- */
    /*                                 initialize                                  */
    /* -------------------------------------------------------------------------- */

    function test_initializeSuccessfully() public {
        address[] memory newWhitelists = new address[](2);
        newWhitelists[0] = alice;
        newWhitelists[1] = whitelistedUser;

        SlpPharosProxy newProxy = _deployProxy();
        newProxy.initialize(owner, newWhitelists, bridgeVault, rewardsVault);

        assertTrue(newProxy.hasRole(DEFAULT_ADMIN_ROLE, owner));
        assertTrue(newProxy.hasRole(WHITELIST_ROLE, alice));
        assertTrue(newProxy.hasRole(WHITELIST_ROLE, whitelistedUser));
        assertEq(newProxy.getBridgeVault(), bridgeVault);
        assertEq(newProxy.getRewardsVault(), rewardsVault);
        assertEq(newProxy.totalBonded(), 0);
        assertEq(newProxy.totalUnbonded(), 0);
    }

    function test_initializeRevertsWithZeroOwner() public {
        SlpPharosProxy newProxy = _deployProxy();

        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.InvalidAddress.selector));
        newProxy.initialize(address(0), whitelists, bridgeVault, rewardsVault);
    }

    function test_initializeRevertsWithZeroBridgeVault() public {
        SlpPharosProxy newProxy = _deployProxy();

        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.InvalidAddress.selector));
        newProxy.initialize(owner, whitelists, address(0), rewardsVault);
    }

    function test_initializeRevertsWithZeroRewardsVault() public {
        SlpPharosProxy newProxy = _deployProxy();

        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.InvalidAddress.selector));
        newProxy.initialize(owner, whitelists, bridgeVault, address(0));
    }

    function test_initializeRevertsWithZeroWhitelistAddress() public {
        address[] memory invalidWhitelists = new address[](1);
        invalidWhitelists[0] = address(0);
        SlpPharosProxy newProxy = _deployProxy();

        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.InvalidAddress.selector));
        newProxy.initialize(owner, invalidWhitelists, bridgeVault, rewardsVault);
    }

    function test_cannotInitializeTwice() public {
        vm.expectRevert(abi.encodeWithSignature("InvalidInitialization()"));
        slpPharosProxy.initialize(owner, whitelists, bridgeVault, rewardsVault);
    }

    /* -------------------------------------------------------------------------- */
    /*                             register / unregister                          */
    /* -------------------------------------------------------------------------- */

    function test_registerPoolIdSuccessfully() public {
        bytes32 newPoolId = keccak256("newPoolId");

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, false);
        emit SlpPharosProxy.PoolIdRegistered(whitelistedUser, newPoolId);
        slpPharosProxy.registerPoolId(newPoolId);
        vm.stopPrank();

        assertTrue(slpPharosProxy.isPoolIdRegistered(newPoolId));
    }

    function test_registerPoolIdRevertsWhenAlreadyRegistered() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdAlreadyRegistered.selector));
        slpPharosProxy.registerPoolId(poolId);
        vm.stopPrank();
    }

    function test_registerPoolIdRevertsWhenNotWhitelisted() public {
        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.registerPoolId(keccak256("another"));
        vm.stopPrank();
    }

    function test_unregisterPoolIdSuccessfully() public {
        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, false);
        emit SlpPharosProxy.PoolIdUnRegistered(whitelistedUser, poolId);
        slpPharosProxy.unregisterPoolId(poolId);
        vm.stopPrank();

        assertFalse(slpPharosProxy.isPoolIdRegistered(poolId));
    }

    function test_unregisterPoolIdRevertsWhenNotRegistered() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.unregisterPoolId(keccak256("fake_pool"));
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                                    bond                                     */
    /* -------------------------------------------------------------------------- */

    function test_bondSuccessfully() public {
        uint256 amount = 10 ether;
        uint256 proxyBalanceBefore = address(slpPharosProxy).balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.Bonded(whitelistedUser, poolId, amount);
        slpPharosProxy.bond(poolId, amount);
        vm.stopPrank();

        assertEq(slpPharosProxy.totalBonded(), amount);
        assertEq(address(slpPharosProxy).balance, proxyBalanceBefore - amount);
        assertEq(_staking().lastDelegatedValue(), amount);
    }

    function test_bondRevertsWhenAmountExceedsBalance() public {
        uint256 amount = address(slpPharosProxy).balance + 1;

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.AmountExceeds.selector));
        slpPharosProxy.bond(poolId, amount);
        vm.stopPrank();
    }

    function test_bondRevertsWhenNotWhitelisted() public {
        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.bond(poolId, 10 ether);
        vm.stopPrank();
    }

    function test_bondRevertsWhenPoolIdNotRegistered() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.bond(keccak256("fake_pool"), 10 ether);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                                  batchBond                                  */
    /* -------------------------------------------------------------------------- */

    function test_batchBondSuccessfully() public {
        bytes32[] memory poolIds = _twoPools();
        uint256[] memory amounts = new uint256[](2);
        amounts[0] = 10 ether;
        amounts[1] = 25 ether;
        uint256 proxyBalanceBefore = address(slpPharosProxy).balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.Bonded(whitelistedUser, poolIds[0], amounts[0]);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.Bonded(whitelistedUser, poolIds[1], amounts[1]);
        slpPharosProxy.batchBond(poolIds, amounts);
        vm.stopPrank();

        assertEq(slpPharosProxy.totalBonded(), 35 ether);
        assertEq(address(slpPharosProxy).balance, proxyBalanceBefore - 35 ether);
        assertEq(_staking().lastPoolId(), poolIds[1]);
        assertEq(_staking().lastDelegatedValue(), amounts[1]);
    }

    function test_batchBondRevertsWhenLengthsMismatch() public {
        bytes32[] memory poolIds = _twoPools();
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 10 ether;

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.MismatchLength.selector));
        slpPharosProxy.batchBond(poolIds, amounts);
        vm.stopPrank();
    }

    function test_batchBondRevertsWhenAnyPoolIdNotRegistered() public {
        bytes32[] memory poolIds = new bytes32[](2);
        poolIds[0] = poolId;
        poolIds[1] = keccak256("fake_pool");
        uint256[] memory amounts = new uint256[](2);
        amounts[0] = 10 ether;
        amounts[1] = 10 ether;

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.batchBond(poolIds, amounts);
        vm.stopPrank();
    }

    function test_batchBondRevertsWhenTotalExceedsBalance() public {
        // Each amount alone fits in the 100 ether balance, but their sum does not.
        bytes32[] memory poolIds = _twoPools();
        uint256[] memory amounts = new uint256[](2);
        amounts[0] = 60 ether;
        amounts[1] = 60 ether;

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.AmountExceeds.selector));
        slpPharosProxy.batchBond(poolIds, amounts);
        vm.stopPrank();
    }

    function test_batchBondRevertsWhenNotWhitelisted() public {
        bytes32[] memory poolIds = new bytes32[](1);
        poolIds[0] = poolId;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 10 ether;

        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.batchBond(poolIds, amounts);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                                   unbond                                    */
    /* -------------------------------------------------------------------------- */

    function test_unbondSuccessfully() public {
        uint256 amount = 30 ether;
        uint256 unlockEpoch = 42;
        _staking().setUndelegateResult(amount, unlockEpoch);

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, true, true);
        emit SlpPharosProxy.Unbonded(whitelistedUser, poolId, amount, unlockEpoch);
        slpPharosProxy.unbond(poolId);
        vm.stopPrank();

        assertEq(slpPharosProxy.totalUnbonded(), amount);
    }

    function test_unbondRevertsWhenNotWhitelisted() public {
        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.unbond(poolId);
        vm.stopPrank();
    }

    function test_unbondRevertsWhenPoolIdNotRegistered() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.unbond(keccak256("fake_pool"));
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                                 batchUnbond                                 */
    /* -------------------------------------------------------------------------- */

    function test_batchUnbondSuccessfully() public {
        bytes32[] memory poolIds = _twoPools();
        uint256 amount = 30 ether;
        uint256 unlockEpoch = 42;
        _staking().setUndelegateResult(amount, unlockEpoch);
        // Guard the regression: the emitted unlockEpoch must come from staking, not block.number.
        assertNotEq(unlockEpoch, block.number);

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, true, true);
        emit SlpPharosProxy.Unbonded(whitelistedUser, poolIds[0], amount, unlockEpoch);
        vm.expectEmit(true, true, true, true);
        emit SlpPharosProxy.Unbonded(whitelistedUser, poolIds[1], amount, unlockEpoch);
        slpPharosProxy.batchUnbond(poolIds);
        vm.stopPrank();

        assertEq(slpPharosProxy.totalUnbonded(), amount * 2);
        assertEq(_staking().lastPoolId(), poolIds[1]);
    }

    function test_batchUnbondRevertsWhenAnyPoolIdNotRegistered() public {
        bytes32[] memory poolIds = new bytes32[](2);
        poolIds[0] = poolId;
        poolIds[1] = keccak256("fake_pool");

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.batchUnbond(poolIds);
        vm.stopPrank();
    }

    function test_batchUnbondRevertsWhenNotWhitelisted() public {
        bytes32[] memory poolIds = new bytes32[](1);
        poolIds[0] = poolId;

        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.batchUnbond(poolIds);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                             claim / claimReward                            */
    /* -------------------------------------------------------------------------- */

    function test_claimSuccessfully() public {
        uint256 claimAmount = 5 ether;
        _staking().setClaimStakeAmount(claimAmount);
        vm.deal(STAKING_ADDR, claimAmount);

        uint256 proxyBalanceBefore = address(slpPharosProxy).balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.Claimed(whitelistedUser, poolId, claimAmount);
        slpPharosProxy.claim(poolId);
        vm.stopPrank();

        assertEq(address(slpPharosProxy).balance, proxyBalanceBefore + claimAmount);
    }

    function test_claimRevertsWhenPoolIdNotRegistered() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.claim(keccak256("fake_pool"));
        vm.stopPrank();
    }

    function test_claimRewardSuccessfully() public {
        uint256 rewardAmount = 2 ether;
        _staking().setClaimRewardAmount(rewardAmount);

        // claimReward only records the amount: nothing is forwarded to the vault and,
        // like on Pharos, nothing lands in the proxy during the transaction either.
        uint256 proxyBalanceBefore = address(slpPharosProxy).balance;
        uint256 rewardsVaultBalanceBefore = rewardsVault.balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.RewardClaimed(whitelistedUser, poolId, rewardAmount);
        slpPharosProxy.claimReward(poolId);
        vm.stopPrank();

        assertEq(address(slpPharosProxy).balance, proxyBalanceBefore);
        assertEq(rewardsVault.balance, rewardsVaultBalanceBefore);
        assertEq(slpPharosProxy.totalRewardClaimed(), rewardAmount);
        assertEq(slpPharosProxy.totalRewardTransferred(), 0);
        assertEq(_staking().lastPoolId(), poolId);
    }

    function test_claimRewardAccumulatesTotalRewardClaimed() public {
        _staking().setClaimRewardAmount(2 ether);
        vm.prank(whitelistedUser);
        slpPharosProxy.claimReward(poolId);

        _staking().setClaimRewardAmount(3 ether);
        vm.prank(whitelistedUser);
        slpPharosProxy.claimReward(poolId);

        assertEq(slpPharosProxy.totalRewardClaimed(), 5 ether);
    }

    function test_claimRewardRevertsWhenNoRewards() public {
        // Pharos reverts when the pool has nothing claimable; the proxy must bubble it up.
        _staking().setClaimRewardAmount(0);

        vm.startPrank(whitelistedUser);
        vm.expectRevert(bytes("No rewards to claim"));
        slpPharosProxy.claimReward(poolId);
        vm.stopPrank();
    }

    function test_claimRewardRevertsWhenPoolIdNotRegistered() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.claimReward(keccak256("fake_pool"));
        vm.stopPrank();
    }

    function test_claimRewardRevertsWhenNotWhitelisted() public {
        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.claimReward(poolId);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                                  batchClaim                                 */
    /* -------------------------------------------------------------------------- */

    function test_batchClaimSuccessfully() public {
        bytes32[] memory poolIds = _twoPools();
        uint256 claimAmount = 5 ether;
        _staking().setClaimStakeAmount(claimAmount);
        vm.deal(STAKING_ADDR, claimAmount * 2);

        uint256 proxyBalanceBefore = address(slpPharosProxy).balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.Claimed(whitelistedUser, poolIds[0], claimAmount);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.Claimed(whitelistedUser, poolIds[1], claimAmount);
        slpPharosProxy.batchClaim(poolIds);
        vm.stopPrank();

        assertEq(address(slpPharosProxy).balance, proxyBalanceBefore + claimAmount * 2);
        assertEq(_staking().lastPoolId(), poolIds[1]);
    }

    function test_batchClaimRevertsWhenAnyPoolIdNotRegistered() public {
        bytes32[] memory poolIds = new bytes32[](2);
        poolIds[0] = poolId;
        poolIds[1] = keccak256("fake_pool");

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.batchClaim(poolIds);
        vm.stopPrank();
    }

    function test_batchClaimRevertsWhenNotWhitelisted() public {
        bytes32[] memory poolIds = new bytes32[](1);
        poolIds[0] = poolId;

        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.batchClaim(poolIds);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                               batchClaimReward                              */
    /* -------------------------------------------------------------------------- */

    function test_batchClaimRewardSuccessfully() public {
        bytes32[] memory poolIds = _twoPools();
        uint256 rewardAmount = 2 ether;
        _staking().setClaimRewardAmount(rewardAmount);

        uint256 proxyBalanceBefore = address(slpPharosProxy).balance;
        uint256 rewardsVaultBalanceBefore = rewardsVault.balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.RewardClaimed(whitelistedUser, poolIds[0], rewardAmount);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.RewardClaimed(whitelistedUser, poolIds[1], rewardAmount);
        slpPharosProxy.batchClaimReward(poolIds);
        vm.stopPrank();

        // Only accounting changes; no funds move in this transaction.
        assertEq(address(slpPharosProxy).balance, proxyBalanceBefore);
        assertEq(rewardsVault.balance, rewardsVaultBalanceBefore);
        assertEq(slpPharosProxy.totalRewardClaimed(), rewardAmount * 2);
        assertEq(_staking().lastPoolId(), poolIds[1]);
    }

    function test_batchClaimRewardSkipsPoolsWithZeroReward() public {
        // Pharos reverts on a zero reward; the batch must skip such pools instead of reverting.
        bytes32[] memory poolIds = _twoPools();
        uint256 rewardAmount = 2 ether;
        _staking().setClaimRewardAmount(rewardAmount);
        _staking().setZeroReward(poolIds[1], true);

        vm.recordLogs();
        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, true, false, true);
        emit SlpPharosProxy.RewardClaimed(whitelistedUser, poolIds[0], rewardAmount);
        slpPharosProxy.batchClaimReward(poolIds);
        vm.stopPrank();

        // Exactly one RewardClaimed from the proxy: the zero-reward pool emits nothing and is never claimed.
        // (The expectEmit above also emits a RewardClaimed from this test contract; filter by emitter.)
        Vm.Log[] memory logs = vm.getRecordedLogs();
        uint256 rewardClaimedCount = 0;
        for (uint256 i = 0; i < logs.length; ++i) {
            if (logs[i].emitter == address(slpPharosProxy) && logs[i].topics[0] == SlpPharosProxy.RewardClaimed.selector) {
                ++rewardClaimedCount;
            }
        }
        assertEq(rewardClaimedCount, 1);
        assertEq(_staking().lastPoolId(), poolIds[0]);
        assertEq(slpPharosProxy.totalRewardClaimed(), rewardAmount);
    }

    function test_batchClaimRewardIsNoopWhenAllPoolsHaveZeroReward() public {
        bytes32[] memory poolIds = _twoPools();
        _staking().setClaimRewardAmount(0);

        vm.recordLogs();
        vm.prank(whitelistedUser);
        slpPharosProxy.batchClaimReward(poolIds);

        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(slpPharosProxy.totalRewardClaimed(), 0);
        // claimReward was never reached on the mock.
        assertEq(_staking().lastPoolId(), bytes32(0));
    }

    function test_batchClaimRewardRevertsWhenAnyPoolIdNotRegistered() public {
        // Give the registered pool a reward so the revert comes from the unregistered one.
        _staking().setClaimRewardAmount(1 ether);
        bytes32[] memory poolIds = new bytes32[](2);
        poolIds[0] = poolId;
        poolIds[1] = keccak256("fake_pool");

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.batchClaimReward(poolIds);
        vm.stopPrank();
    }

    function test_batchClaimRewardRevertsWhenNotWhitelisted() public {
        bytes32[] memory poolIds = new bytes32[](1);
        poolIds[0] = poolId;

        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.batchClaimReward(poolIds);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                            transferToRewardsVault                           */
    /* -------------------------------------------------------------------------- */

    function test_transferToRewardsVaultSuccessfully() public {
        uint256 amount = 3 ether;
        uint256 proxyBalanceBefore = address(slpPharosProxy).balance;
        uint256 rewardsVaultBalanceBefore = rewardsVault.balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, false, false, true);
        emit SlpPharosProxy.TransferredToRewardsVault(whitelistedUser, amount);
        slpPharosProxy.transferToRewardsVault(amount);
        vm.stopPrank();

        assertEq(address(slpPharosProxy).balance, proxyBalanceBefore - amount);
        assertEq(rewardsVault.balance, rewardsVaultBalanceBefore + amount);
        assertEq(slpPharosProxy.totalRewardTransferred(), amount);
    }

    function test_claimRewardThenTransferToRewardsVault() public {
        // Two-step flow: claim (accounting only) now, forward once the minted reward has landed.
        uint256 rewardAmount = 2 ether;
        _staking().setClaimRewardAmount(rewardAmount);

        vm.prank(whitelistedUser);
        slpPharosProxy.claimReward(poolId);
        assertEq(slpPharosProxy.totalRewardClaimed() - slpPharosProxy.totalRewardTransferred(), rewardAmount);

        // Simulate the node crediting the minted reward after the claim transaction.
        vm.deal(address(slpPharosProxy), address(slpPharosProxy).balance + rewardAmount);

        uint256 rewardsVaultBalanceBefore = rewardsVault.balance;
        vm.prank(whitelistedUser);
        slpPharosProxy.transferToRewardsVault(rewardAmount);

        assertEq(rewardsVault.balance, rewardsVaultBalanceBefore + rewardAmount);
        assertEq(slpPharosProxy.totalRewardClaimed(), slpPharosProxy.totalRewardTransferred());
    }

    function test_transferToRewardsVaultRevertsWhenAmountExceedsBalance() public {
        uint256 amount = address(slpPharosProxy).balance + 1;

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.InsufficientBalance.selector));
        slpPharosProxy.transferToRewardsVault(amount);
        vm.stopPrank();
    }

    function test_transferToRewardsVaultRevertsWhenNotWhitelisted() public {
        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.transferToRewardsVault(1 ether);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                        compoundRewards / settleReward                      */
    /* -------------------------------------------------------------------------- */

    function test_compoundRewardsSuccessfully() public {
        vm.prank(whitelistedUser);
        slpPharosProxy.compoundRewards(poolId);
        assertEq(_staking().lastPoolId(), poolId);
    }

    function test_compoundRewardsRevertsWhenNotWhitelisted() public {
        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.compoundRewards(poolId);
        vm.stopPrank();
    }

    function test_settleRewardSuccessfully() public {
        vm.prank(whitelistedUser);
        slpPharosProxy.settleReward(poolId);
        assertEq(_staking().lastPoolId(), poolId);
    }

    /* -------------------------------------------------------------------------- */
    /*                              batchSettleReward                              */
    /* -------------------------------------------------------------------------- */

    function test_batchSettleRewardSuccessfully() public {
        bytes32[] memory poolIds = _twoPools();

        vm.prank(whitelistedUser);
        slpPharosProxy.batchSettleReward(poolIds);

        assertEq(_staking().lastPoolId(), poolIds[1]);
    }

    function test_batchSettleRewardRevertsWhenAnyPoolIdNotRegistered() public {
        bytes32[] memory poolIds = new bytes32[](2);
        poolIds[0] = poolId;
        poolIds[1] = keccak256("fake_pool");

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.PoolIdNotRegistered.selector));
        slpPharosProxy.batchSettleReward(poolIds);
        vm.stopPrank();
    }

    function test_batchSettleRewardRevertsWhenNotWhitelisted() public {
        bytes32[] memory poolIds = new bytes32[](1);
        poolIds[0] = poolId;

        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.batchSettleReward(poolIds);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                                transferBack                                 */
    /* -------------------------------------------------------------------------- */

    function test_transferBackSuccessfully() public {
        uint256 transferAmount = 20 ether;

        uint256 vaultBalanceBefore = bridgeVault.balance;
        uint256 proxyBalanceBefore = address(slpPharosProxy).balance;

        vm.startPrank(whitelistedUser);
        vm.expectEmit(true, false, false, true);
        emit SlpPharosProxy.TransferredBack(whitelistedUser, transferAmount);
        slpPharosProxy.transferBack(transferAmount);
        vm.stopPrank();

        assertEq(bridgeVault.balance, vaultBalanceBefore + transferAmount);
        assertEq(address(slpPharosProxy).balance, proxyBalanceBefore - transferAmount);
    }

    function test_transferBackRevertsWhenAmountExceedsBalance() public {
        uint256 transferAmount = address(slpPharosProxy).balance + 1;

        vm.startPrank(whitelistedUser);
        vm.expectRevert(abi.encodeWithSelector(SlpPharosProxy.InsufficientBalance.selector));
        slpPharosProxy.transferBack(transferAmount);
        vm.stopPrank();
    }

    function test_transferBackRevertsWhenNotWhitelisted() public {
        vm.startPrank(nonWhitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", nonWhitelistedUser, WHITELIST_ROLE
            )
        );
        slpPharosProxy.transferBack(5 ether);
        vm.stopPrank();
    }

    /* -------------------------------------------------------------------------- */
    /*                            views / roles / receive                         */
    /* -------------------------------------------------------------------------- */

    function test_receiveEth() public {
        uint256 amount = 10 ether;
        address sender = makeAddr("sender");
        vm.deal(sender, amount);

        uint256 balanceBefore = address(slpPharosProxy).balance;

        vm.startPrank(sender);
        vm.expectEmit(true, false, false, true);
        emit SlpPharosProxy.EthDeposited(sender, amount);
        (bool success,) = address(slpPharosProxy).call{value: amount}("");
        require(success, "ETH transfer failed");
        vm.stopPrank();

        assertEq(address(slpPharosProxy).balance, balanceBefore + amount);
    }

    function test_viewFunctions() public view {
        assertEq(slpPharosProxy.getBridgeVault(), bridgeVault);
        assertEq(slpPharosProxy.getRewardsVault(), rewardsVault);
        assertEq(slpPharosProxy.totalBonded(), 0);
        assertEq(slpPharosProxy.totalUnbonded(), 0);
        assertEq(slpPharosProxy.totalRewardClaimed(), 0);
        assertEq(slpPharosProxy.totalRewardTransferred(), 0);

        bytes32[] memory poolIds = slpPharosProxy.registeredPools();
        assertEq(poolIds.length, 1);
        assertEq(poolIds[0], poolId);

        assertTrue(slpPharosProxy.isPoolIdRegistered(poolId));
        assertFalse(slpPharosProxy.isPoolIdRegistered(keccak256("fake_pool")));
    }

    function test_grantWhitelistRole() public {
        vm.startPrank(owner);
        slpPharosProxy.grantRole(WHITELIST_ROLE, alice);
        assertTrue(slpPharosProxy.hasRole(WHITELIST_ROLE, alice));
        vm.stopPrank();
    }

    function test_revokeWhitelistRole() public {
        vm.startPrank(owner);
        slpPharosProxy.revokeRole(WHITELIST_ROLE, whitelistedUser);
        assertFalse(slpPharosProxy.hasRole(WHITELIST_ROLE, whitelistedUser));
        vm.stopPrank();
    }

    function test_grantRoleRevertsWhenNotAdmin() public {
        vm.startPrank(whitelistedUser);
        vm.expectRevert(
            abi.encodeWithSignature(
                "AccessControlUnauthorizedAccount(address,bytes32)", whitelistedUser, DEFAULT_ADMIN_ROLE
            )
        );
        slpPharosProxy.grantRole(WHITELIST_ROLE, alice);
        vm.stopPrank();
    }
}
