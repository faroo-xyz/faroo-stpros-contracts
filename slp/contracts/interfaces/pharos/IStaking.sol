// https://silken-muskox-24e.notion.site/Pharos-Delegation-Documentation-3078ec314f7580319e13d4b623e59548
// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.0;

interface IStaking {
    struct Validator {
        string description;
        string publicKey;
        string publicKeyPop;
        string blsPublicKey;
        string blsPublicKeyPop;
        string endpoint;
        uint8 status;
        bytes32 poolId;
        uint256 totalStake;
        address owner;
        uint256 stakeSnapshot;
        uint256 pendingWithdrawStake;
        uint8 pendingWithdrawWindow;
    }

    struct PendingUndelegation {
        address delegator;
        bytes32 poolId;
        uint256 amount;
        uint256 unlockEpoch;
        uint256 pendingWithdrawWindow;
        bool processed;
        uint256 principalAmount; // Amount backed by real contract balance
        uint256 rewardAmount; // Amount that needs to be minted by client
    }

    struct Delegator {
        uint256 principalStake; // Tracks actual deposits (excluding compounded rewards)
        uint256 stake;
        uint256 accumulatedRewardPerShare;
        uint256 rewards;
        uint256 pendingStake;
        uint256 pendingWithdrawStake;
        uint256 pendingWithdrawWindow;
        uint256 totalRewardsClaimed;
        bool isPendingUndelegate;
    }

    /**
     * @notice Emitted when a delegator stakes tokens to a validator's pool
     * @param delegator Address of the delegator who initiated the stake
     * @param poolId Unique identifier of the validator's pool
     * @param amount Amount of tokens delegated
     * @param effectiveEpoch Epoch number when the delegation becomes effective
     * @dev The event uses indexed parameters for efficient filtering by delegator and poolId
     */
    event StakeDelegated(address indexed delegator, bytes32 indexed poolId, uint256 amount, uint256 effectiveEpoch);

    /**
     * @notice Emitted when a delegator initiates an undelegation from a validator's pool
     * @param delegator Address of the delegator who initiated the undelegation
     * @param poolId Unique identifier of the validator's pool
     * @param amount Total amount to withdraw (stake + unclaimed rewards)
     * @param unlockEpoch Epoch number when the undelegated funds become withdrawable
     * @dev The event uses indexed parameters for efficient filtering by delegator and poolId
     */
    event StakeUndelegated(address indexed delegator, bytes32 indexed poolId, uint256 amount, uint256 unlockEpoch);

    /**
     * @notice Emitted when a delegator claims their accumulated rewards from a validator's pool
     * @param delegator Address of the delegator who claimed rewards
     * @param poolId Unique identifier of the validator's pool
     * @param amount Amount of rewards claimed
     * @dev The event uses indexed parameters for efficient filtering by delegator and poolId
     */
    event RewardClaimed(address indexed delegator, bytes32 indexed poolId, uint256 amount);

    /**
     * @notice Emitted when a validator's commission rate is updated
     * @param poolId Unique identifier of the validator's pool
     * @param oldRate Previous commission rate (scaled by 10000, e.g., 1000 = 10%)
     * @param newRate New commission rate (scaled by 10000, e.g., 1500 = 15%)
     * @dev The event uses indexed parameter for efficient filtering by poolId
     */
    event CommissionRateChanged(bytes32 indexed poolId, uint256 oldRate, uint256 newRate);

    /**
     * @notice Emitted when a delegator compounds their unclaimed rewards into additional stake
     * @param delegator Address of the delegator who compounded rewards
     * @param poolId Unique identifier of the validator's pool
     * @param amount Amount of rewards compounded into stake
     * @dev The event uses indexed parameters for efficient filtering by delegator and poolId
     */
    event RewardsCompounded(address indexed delegator, bytes32 indexed poolId, uint256 amount);

    /**
     * @notice Emitted when new coins are minted (for rewards) and credited to a beneficiary
     * @param beneficiary Address of the account receiving the minted coins
     * @param reason Code indicating the reason for minting (1: delegator rewards, 2: validator commissions, etc.)
     * @param amount Amount of coins minted
     * @dev The event uses indexed parameters for efficient filtering by beneficiary and reason
     */
    event CoinMinted(address indexed beneficiary, uint8 indexed reason, uint256 amount);

    // ==================== Validator Interfaces ====================

    function isValidatorActive(bytes32 _poolId) external view returns (bool);
    function isValidatorPendingAdd(bytes32 _poolId) external view returns (bool);
    function isValidatorPendingUpdate(bytes32 _poolId) external view returns (bool);
    function isValidatorPendingExit(bytes32 _poolId) external view returns (bool);
    function registerValidator(
        string memory _description,
        string memory _publicKey,
        string memory _publicKeyPop,
        string memory _blsPublicKey,
        string memory _blsPublicKeyPop,
        string memory _endpoint
    ) external payable returns (bytes32);
    function updateValidator(bytes32 _poolId, string memory _description, string memory _endpoint) external;
    function updateValidatorOwner(bytes32 _poolId, address _newOwner) external;
    function exitValidator(bytes32 _poolId) external;
    function addStake(bytes32 _poolId) external payable;
    function advanceEpoch() external;
    function advanceEpoch(bytes32[] memory _poolIds, uint256[] memory _priorityFees) external;
    function getActiveValidators() external view returns (bytes32[] memory);
    function getPendingAddValidators() external view returns (bytes32[] memory);
    function getPendingExitValidators() external view returns (bytes32[] memory);
    function getPendingUpdateValidators() external view returns (bytes32[] memory);
    function slashValidator(bytes32 _poolId) external;
    function withdrawStake(bytes32 _poolId, uint256 _withdrawStake) external;
    function claimStake(bytes32 _poolId) external;
    function getImplAddress() external view returns (address);
    function updateImplAddress(address _newImplementationAddress) external;

    // ==================== Delegation Interfaces ====================

    /**
     * @notice Delegates tokens to a validator's pool (for external delegators)
     * @param _poolId Unique identifier of the validator's pool
     * @dev The function is payable (msg.value is the amount of tokens to delegate)
     * @dev Requires the validator to have delegation enabled and the delegator to be in the whitelist (if enabled)
     * @dev The delegation takes effect at the next epoch
     * emit StakeDelegated Event with delegator address, poolId, amount, and effective epoch
     */
    function delegate(bytes32 _poolId) external payable;

    /**
     * @notice Initiates full undelegation from a validator's pool (withdraws stake + rewards)
     * @param _poolId Unique identifier of the validator's pool
     * @return amount Total amount to withdraw (stake + unclaimed rewards)
     * @return unlockEpoch Epoch number when the funds become withdrawable
     * @dev Only the delegator can call this function for their own stake
     * @dev Does not support partial undelegation (full stake only)
     * @dev Adds the delegator to the pending undelegation queue
     * emit StakeUndelegated Event with delegator address, poolId, amount, and unlock epoch
     */
    function undelegate(bytes32 _poolId) external returns (uint256 amount, uint256 unlockEpoch);

    /**
     * @notice Claims unclaimed rewards from a validator's pool for the caller
     * @param _poolId Unique identifier of the validator's pool
     * @return amount Amount of rewards claimed (and minted to the delegator)
     * @dev Only the delegator can claim their own rewards
     * @dev Triggers CoinMinted event for reward distribution (platform-side minting)
     * emit RewardClaimed Event with delegator address, poolId, and claimed amount
     * emit CoinMinted Event with delegator address, reason code (1 for delegator rewards), and amount
     */
    function claimReward(bytes32 _poolId) external returns (uint256);

    /**
     * @notice Compounds unclaimed rewards into additional stake in a validator's pool
     * @param _poolId Unique identifier of the validator's pool
     * @dev Only the delegator can compound their own rewards
     * @dev Converts unclaimed rewards into additional delegated stake
     * emit RewardsCompounded Event with delegator address, poolId, and compounded amount
     */
    function compoundRewards(bytes32 _poolId) external;

    /**
     * @notice Settles (calculates) unclaimed rewards for the caller from a validator's pool
     * @param _poolId Unique identifier of the validator's pool
     * @dev Updates the delegator's unclaimed rewards balance (lazy accounting)
     * @dev No events emitted (purely a state update for the delegator)
     */
    function settleReward(bytes32 _poolId) external;

    /**
     * @notice Updates the commission rate for a validator's pool (fee taken from delegator rewards)
     * @param _poolId Unique identifier of the validator's pool
     * @param _newRate New commission rate (scaled by 10000, e.g., 1000 = 10%, 10000 = 100%)
     * @dev Only the validator owner can call this function
     * @dev The new rate cannot exceed 100% (10000 in scaled value)
     * emit CommissionRateChanged Event with poolId, old rate, and new rate
     */
    function setCommissionRate(bytes32 _poolId, uint256 _newRate) external;

    /**
     * @notice Enables or disables delegation for a validator's pool
     * @param _poolId Unique identifier of the validator's pool
     * @param _enabled Boolean indicating if delegation should be enabled (true) or disabled (false)
     * @dev Only the validator owner can call this function
     * @dev Disabling delegation prevents new delegations but does not affect existing ones
     */
    function setDelegationEnabled(bytes32 _poolId, bool _enabled) external;

    /**
     * @notice Adds a delegator to the validator's whitelist (if whitelist is enabled)
     * @param _poolId Unique identifier of the validator's pool
     * @param _delegator Address of the delegator to add to the whitelist
     * @dev Only the validator owner can call this function
     * @dev Whitelisted delegators can delegate even if the pool has restricted delegation
     */
    function addDelegatorToWhitelist(bytes32 _poolId, address _delegator) external;

    /**
     * @notice Removes a delegator from the validator's whitelist (if whitelist is enabled)
     * @param _poolId Unique identifier of the validator's pool
     * @param _delegator Address of the delegator to remove from the whitelist
     * @dev Only the validator owner can call this function
     * @dev Does not affect existing delegations from the removed address
     */
    function removeDelegatorFromWhitelist(bytes32 _poolId, address _delegator) external;

    /**
     * @notice Returns detailed information about a delegator's stake in a validator's pool
     * @param _poolId Unique identifier of the validator's pool
     * @param _delegator Address of the delegator to query
     */
    function getDelegator(bytes32 _poolId, address _delegator) external view returns (Delegator memory delegator);

    /**
     * @notice Calculates the current unclaimed rewards for a delegator in a validator's pool
     * @param _poolId Unique identifier of the validator's pool
     * @param _delegator Address of the delegator to query
     * @return Amount of unclaimed rewards (including both settled and unsettled rewards)
     * @dev Uses lazy accounting to calculate rewards based on current reward-per-share
     */
    function getDelegatorReward(bytes32 _poolId, address _delegator) external view returns (uint256);

    /**
     * @notice Returns the current commission rate for a validator's pool
     * @param _poolId Unique identifier of the validator's pool
     * @return Current commission rate (scaled by 10000, e.g., 1000 = 10%, 10000 = 100%)
     */
    function getCommissionRate(bytes32 _poolId) external view returns (uint256);

    /**
     * @notice Returns the total number of active validators
     * @return count Number of active validators
     */
    function getActiveValidatorCount() external view returns (uint256);

    /**
     * @notice Returns the total number of delegators across all active validator pools
     * @return count Total number of delegators
     */
    function getTotalDelegatorCount() external view returns (uint256);

    // ==================== Extended Query Interfaces ====================

    /**
     * @notice Returns detailed information about a validator
     * @param _poolId Unique identifier of the validator's pool
     * @return validator Validator struct containing all validator details
     */
    function getValidator(bytes32 _poolId) external view returns (Validator memory validator);

    /**
     * @notice Returns all validator pool IDs (active and inactive)
     * @return poolIds Array of all validator pool IDs
     */
    function getAllValidators() external view returns (bytes32[] memory poolIds);

    /**
     * @notice Returns the count of validators by status
     * @return activeCount Number of active validators
     * @return inactiveCount Number of inactive validators (pending add but not yet active)
     * @return pendingExitCount Number of validators pending exit
     */
    function getValidatorCounts()
        external
        view
        returns (uint256 activeCount, uint256 inactiveCount, uint256 pendingExitCount);

    /**
     * @notice Returns the current blockchain information
     * @return currentEpoch Current epoch number
     * @return currentBlock Current block height
     * @return totalStake Total staked across all validators
     */
    function getBlockchainInfo() external view returns (uint256 currentEpoch, uint256 currentBlock, uint256 totalStake);

    /**
     * @notice Returns user's total staking information across all validators
     * @param _delegator Address of the delegator to query
     * @return totalStake Total amount of PROS staked across all validators
     * @return totalRewards Total unclaimed rewards across all validators
     * @return totalClaimed Total rewards already claimed across all validators
     */
    function getUserTotalInfo(address _delegator)
        external
        view
        returns (uint256 totalStake, uint256 totalRewards, uint256 totalClaimed);

    /**
     * @notice Returns list of validators where the user has staked
     * @param _delegator Address of the delegator to query
     * @return poolIds Array of pool IDs where the user has stakes
     * @return stakes Array of stake amounts corresponding to each pool ID
     * @return rewards Array of unclaimed rewards corresponding to each pool ID
     */
    function getUserValidators(address _delegator)
        external
        view
        returns (bytes32[] memory poolIds, uint256[] memory stakes, uint256[] memory rewards);

    /**
     * @notice Returns all pending undelegations for a user
     * @param _delegator Address of the delegator to query
     * @return undelegations Array of PendingUndelegation structs
     */
    function getUserPendingUndelegations(address _delegator)
        external
        view
        returns (PendingUndelegation[] memory undelegations);

    /**
     * @notice Returns the estimated APR for a validator's pool
     * @param _poolId Unique identifier of the validator's pool
     * @return apr Estimated annual percentage rate (scaled by 100, e.g., 500 = 5%)
     * @dev APR is calculated based on inflation rate and validator's share of total stake
     */
    function getValidatorApr(bytes32 _poolId) external view returns (uint256 apr);
}
