// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {MockERC20} from "./MockERC20.sol";

/// @title Fixed-source redemption-rate vault double for Price Manager tests.
/// @notice Only the rate surface the Price Manager reads: `convertToAssets(uint256)`. The rate
///         is 1e18-scaled WPROS per stPROS share, floored, mirroring production
///         stPROS.convertToAssets -> Oracle.getTokenAmountByVToken(..., Floor). On Pharos
///         mainnet the same stPROS contract is both the ERC-4626 source and the pool base
///         token, so the test base token combines both roles.
contract MockRateVault {
    enum Mode {
        Normal,
        RevertAlways
    }

    uint256 public rate = 1e18; // 1:1 by default
    Mode public mode = Mode.Normal;

    function setRate(uint256 rate_) external {
        rate = rate_;
    }

    function setRevertAlways(bool yes) external {
        mode = yes ? Mode.RevertAlways : Mode.Normal;
    }

    function convertToAssets(uint256 shares) external view returns (uint256) {
        if (mode == Mode.RevertAlways) revert("vault read failed");
        return shares * rate / 1e18;
    }
}

/// @title stPROS stand-in: ERC20 base token that is simultaneously the redemption-rate vault.
contract MockStPROS is MockERC20, MockRateVault {
    constructor(string memory name_, string memory symbol_) MockERC20(name_, symbol_) {}
}
