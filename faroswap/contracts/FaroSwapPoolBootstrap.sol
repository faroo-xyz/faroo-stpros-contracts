// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {IFaroSwapGSP} from "./interfaces/IFaroSwapGSP.sol";

/**
 * @title One-shot atomic GSP bootstrap (0.01 stPROS + matching WPROS)
 * @notice The GSP requires tokens to be transferred to the pool before `buyShares`; doing that
 *         as separate public transactions would expose the seeded funds to third-party LP-mint
 *         interception. This helper makes the pull, the pool delivery and the LP mint one
 *         atomic transaction: the funding account pre-approves exactly the fixed amounts, then
 *         calls `execute()`. Tokens move straight from the funder into the pool; this contract
 *         never holds balances, keeps no residual allowance authority, and can do nothing else.
 *
 * @dev Failure of any step reverts the whole transaction, so nothing partial is ever left in
 *      the pool. LP-value protection is strict: the pool must still be empty (`totalSupply ==
 *      0`) when execution starts — a pre-seeded, front-run or otherwise initialized pool is
 *      rejected before any funds move; after the mint, the base/quote inputs must be exactly
 *      the configured amounts (any pre-existing token donation shows up as excess input and
 *      reverts) and the minted amount must be exactly the GSP first-mint formula result for
 *      those inputs (`BASE_AMOUNT - 1001` lock), so an imbalanced or manipulated pool can never
 *      absorb the budget at a wrong ratio. See docs/DESIGN.md for the rounding derivation.
 */
contract FaroSwapPoolBootstrap {
    using SafeERC20 for IERC20;

    /// @dev First-mint locked shares of the byte-verified GSP 1.0.1 template (GSPFunding).
    uint256 public constant FIRST_MINT_LOCK = 1001;

    error NotFunder(address caller);
    error AlreadyExecuted();
    error PoolTokenMismatch(address base, address quote);
    error PoolAlreadySeeded(uint256 totalSupply);
    error UnexpectedPoolInputs(uint256 baseInput, uint256 quoteInput, uint256 expectedBase, uint256 expectedQuote);
    error UnexpectedFirstMint(uint256 shares, uint256 expected);
    error SharesNotReceived(uint256 received, uint256 minted);

    event Bootstrapped(
        address indexed pool, address indexed lpRecipient, uint256 shares, uint256 baseInput, uint256 quoteInput
    );

    IFaroSwapGSP public immutable POOL;
    IERC20 public immutable BASE_TOKEN;
    IERC20 public immutable QUOTE_TOKEN;
    /// @notice The only account that may trigger the bootstrap; it funds the exact amounts.
    address public immutable FUNDER;
    /// @notice Final LP-share recipient (the governance Safe).
    address public immutable LP_RECIPIENT;
    uint256 public immutable BASE_AMOUNT;
    uint256 public immutable QUOTE_AMOUNT;

    bool private executed;

    constructor(
        address pool_,
        address baseToken_,
        address quoteToken_,
        address funder_,
        address lpRecipient_,
        uint256 baseAmount_,
        uint256 quoteAmount_
    ) {
        POOL = IFaroSwapGSP(pool_);
        BASE_TOKEN = IERC20(baseToken_);
        QUOTE_TOKEN = IERC20(quoteToken_);
        FUNDER = funder_;
        LP_RECIPIENT = lpRecipient_;
        BASE_AMOUNT = baseAmount_;
        QUOTE_AMOUNT = quoteAmount_;
    }

    /// @notice Pulls the exact base/quote amounts from the funder into the pool and mints the
    ///         LP shares to `LP_RECIPIENT`, atomically. Every failure — wrong pair, non-empty
    ///         pool, donation-diluted inputs, unexpected mint amount, insufficient funding —
    ///         reverts the entire transaction; the funder's funds never leave a failed run.
    function execute() external {
        if (msg.sender != FUNDER) revert NotFunder(msg.sender);
        if (executed) revert AlreadyExecuted();

        if (address(POOL._BASE_TOKEN_()) != address(BASE_TOKEN)
            || address(POOL._QUOTE_TOKEN_()) != address(QUOTE_TOKEN)) {
            revert PoolTokenMismatch(address(POOL._BASE_TOKEN_()), address(POOL._QUOTE_TOKEN_()));
        }
        // Empty-pool state is validated before any funds move: a pool that already holds LP
        // (pre-seeded, front-run first mint, or leftover) is refused outright.
        uint256 supplyBefore = POOL.totalSupply();
        if (supplyBefore != 0) revert PoolAlreadySeeded(supplyBefore);

        BASE_TOKEN.safeTransferFrom(FUNDER, address(POOL), BASE_AMOUNT);
        QUOTE_TOKEN.safeTransferFrom(FUNDER, address(POOL), QUOTE_AMOUNT);

        uint256 recipientBefore = POOL.balanceOf(LP_RECIPIENT);
        (uint256 shares, uint256 baseInput, uint256 quoteInput) = POOL.buyShares(LP_RECIPIENT);

        // Inputs must be exactly ours: GSP counts every balance delta as input, so any tokens
        // donated to the empty pool before this call surface here and abort the bootstrap.
        if (baseInput != BASE_AMOUNT || quoteInput != QUOTE_AMOUNT) {
            revert UnexpectedPoolInputs(baseInput, quoteInput, BASE_AMOUNT, QUOTE_AMOUNT);
        }
        // With exact inputs on an empty pool, the byte-verified template's first-mint formula
        // is deterministic: shares = BASE_AMOUNT - FIRST_MINT_LOCK (see docs/DESIGN.md).
        if (shares != BASE_AMOUNT - FIRST_MINT_LOCK) revert UnexpectedFirstMint(shares, BASE_AMOUNT - FIRST_MINT_LOCK);
        uint256 received = POOL.balanceOf(LP_RECIPIENT) - recipientBefore;
        if (received < shares) revert SharesNotReceived(received, shares);

        executed = true;
        emit Bootstrapped(address(POOL), LP_RECIPIENT, shares, baseInput, quoteInput);
    }
}
