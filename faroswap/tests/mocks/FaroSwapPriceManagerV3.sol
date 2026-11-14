// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {FaroSwapPriceManagerV2} from "./FaroSwapPriceManagerV2.sol";

/// @notice Second forward-upgrade target for chained-upgrade and rollback rehearsals:
///         identical storage layout, inherits the guarded ERC-1822 signal.
contract FaroSwapPriceManagerV3 is FaroSwapPriceManagerV2 {
    string public constant MARKER_V3 = "faroswap-price-manager-v3";

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
        FaroSwapPriceManagerV2(
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
