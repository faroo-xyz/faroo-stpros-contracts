// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// @title Admin-facing and LP-facing subset of the deployed FaroSwap GSP 1.0.1 pool.
/// @notice Signatures mirror the pinned public implementation
///         (Faroswap/contracts @ 41fefb37ae11ca6be98b6cb17977ef9f6375cf6a,
///         contracts/DODOGasSavingPool/GasSavingPool/...). Only the members the
///         FaroSwap Price Manager and the one-shot bootstrap rely on are listed.
///         They are revalidated against the deployed template runtime at bind time
///         (the pool must be an exact EIP-1167 clone of the verified template), so
///         interface drift cannot be smuggled in through this file alone.
interface IFaroSwapGSP {
    // ---- admin / maintainer identity and creation parameters ----
    function _ADMIN_() external view returns (address);

    function _MAINTAINER_() external view returns (address);

    function _BASE_TOKEN_() external view returns (address);

    function _QUOTE_TOKEN_() external view returns (address);

    function _I_() external view returns (uint256);

    function _K_() external view returns (uint256);

    function _LP_FEE_RATE_() external view returns (uint256);

    function _MT_FEE_RATE_() external view returns (uint256);

    function _PRICE_LIMIT_() external view returns (uint256);

    // ---- protected state the pure-i invariant is asserted against ----
    function _BASE_TARGET_() external view returns (uint112);

    function _QUOTE_TARGET_() external view returns (uint112);

    function _RState_() external view returns (uint32);

    function _BASE_RESERVE_() external view returns (uint112);

    function _QUOTE_RESERVE_() external view returns (uint112);

    function getVaultReserve() external view returns (uint256 baseReserve, uint256 quoteReserve);

    // ---- LP shares ----
    function totalSupply() external view returns (uint256);

    function balanceOf(address owner) external view returns (uint256);

    function buyShares(address to)
        external
        returns (uint256 shares, uint256 baseInput, uint256 quoteInput);

    // ---- admin capabilities used by the Price Manager ----
    /// @notice Admin-only. Writes `_I_` and requires
    ///         `|i - _I_| * 1e6 / _I_ <= _PRICE_LIMIT_`. Reverts EXCEED_PRICE_LIMIT otherwise.
    function adjustPrice(uint256 i) external;

    /// @notice Admin-only. Requires priceLimit <= 1e6.
    function adjustPriceLimit(uint256 priceLimit) external;

    // ---- trading surface (fork rehearsal micro-swaps) ----
    /// @notice Caller transfers base tokens in first; payout quote is sent to `to`.
    function sellBase(address to) external returns (uint256);

    /// @notice Caller transfers quote tokens in first; payout base is sent to `to`.
    function sellQuote(address to) external returns (uint256);
}
