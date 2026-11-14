// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {FaroSwapPriceManager} from "../../contracts/FaroSwapPriceManager.sol";

/// @notice Upgrade-rehearsal target: identical storage layout (same ERC-7201 namespace, no new
///         fields) plus a marker function. Used to prove upgrade preservation and layout
///         compatibility; not a deployment candidate.
contract FaroSwapPriceManagerV2 is FaroSwapPriceManager {
    string public constant MARKER = "faroswap-price-manager-v2";

    constructor(
        address stprosVault_,
        address quoteToken_,
        address gspFactory_,
        address verifiedGspTemplate_,
        uint256 expectedLpFeeRate_,
        uint256 expectedMtFeeRate_,
        uint256 expectedK_,
        uint256 expectedPriceLimit_
    )
        FaroSwapPriceManager(
            stprosVault_,
            quoteToken_,
            gspFactory_,
            verifiedGspTemplate_,
            expectedLpFeeRate_,
            expectedMtFeeRate_,
            expectedK_,
            expectedPriceLimit_
        )
    {}
}
