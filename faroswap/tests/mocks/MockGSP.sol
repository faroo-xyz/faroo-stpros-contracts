// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @title Test double of the deployed FaroSwap GSP 1.0.1 pool (admin + funding semantics).
/// @notice Ports the exact behavior of the pinned implementation
///         (Faroswap/contracts @ 41fefb37..., GSPVault.sol / GSPFunding.sol / GSP.sol) for the
///         members the Price Manager and bootstrap depend on: `adjustPrice`'s relative price
///         limit check, `adjustPriceLimit`'s bound, and `buyShares`' first-mint and
///         proportional-deposit math (DecimalMath floor division). Not used on forks; fork
///         tests exercise the real deployed bytecode.
contract MockGSP {
    event IChange(uint256 newI);
    event BuyShares(address to, uint256 increaseShares, uint256 totalShares);

    // -- DecimalMath (floor) --
    function mulFloor(uint256 a, uint256 b) public pure returns (uint256) {
        return a * b / 1e18;
    }

    function divFloor(uint256 a, uint256 b) public pure returns (uint256) {
        return a * 1e18 / b;
    }

    // -- storage mirroring GSPStorage --
    bool public _GSP_INITIALIZED_;
    address public _MAINTAINER_;
    address public _ADMIN_;
    IERC20 public _BASE_TOKEN_;
    IERC20 public _QUOTE_TOKEN_;
    uint112 public _BASE_RESERVE_;
    uint112 public _QUOTE_RESERVE_;
    uint112 public _BASE_TARGET_;
    uint112 public _QUOTE_TARGET_;
    uint32 public _RState_;
    uint256 public totalSupply;
    mapping(address => uint256) public _SHARES_;
    uint256 public _MT_FEE_RATE_;
    uint256 public _LP_FEE_RATE_;
    uint256 public _K_;
    uint256 public _I_;
    uint256 public _PRICE_LIMIT_;

    // -- instrumentation (not part of GSP) --
    uint256 public adjustPriceCalls;
    uint256 public adjustPriceLimitCalls;
    /// @notice when set, adjustPrice calls back into `reenterTarget` first (reentrancy probe).
    address public reenterTarget;

    function init(
        address maintainer,
        address admin,
        address baseTokenAddress,
        address quoteTokenAddress,
        uint256 lpFeeRate,
        uint256 mtFeeRate,
        uint256 i,
        uint256 k,
        uint256 priceLimit,
        bool isOpenTWAP
    ) external {
        require(!_GSP_INITIALIZED_, "GSP_INITIALIZED");
        _GSP_INITIALIZED_ = true;
        require(baseTokenAddress != quoteTokenAddress, "BASE_QUOTE_CAN_NOT_BE_SAME");
        _BASE_TOKEN_ = IERC20(baseTokenAddress);
        _QUOTE_TOKEN_ = IERC20(quoteTokenAddress);
        require(i > 0 && i <= 10 ** 36, "INVALID_I");
        _I_ = i;
        require(k <= 10 ** 18, "INVALID_K");
        _K_ = k;
        _LP_FEE_RATE_ = lpFeeRate;
        _MT_FEE_RATE_ = mtFeeRate;
        _MAINTAINER_ = maintainer;
        _ADMIN_ = admin;
        _PRICE_LIMIT_ = priceLimit;
        require(isOpenTWAP == false);
    }

    function adjustPrice(uint256 i) external {
        require(msg.sender == _ADMIN_, "ADMIN_ACCESS_DENIED");
        adjustPriceCalls += 1;
        if (reenterTarget != address(0)) {
            // solhint-disable-next-line avoid-low-level-calls
            (bool ok,) = reenterTarget.call(abi.encodeWithSignature("reenterProbe()"));
            ok; // result ignored; GSP itself never does this — probe only
        }
        uint256 offset = i > _I_ ? i - _I_ : _I_ - i;
        require((offset * 1e6 / _I_) <= _PRICE_LIMIT_, "EXCEED_PRICE_LIMIT");
        _I_ = i;
        emit IChange(i);
    }

    function adjustPriceLimit(uint256 priceLimit) external {
        require(msg.sender == _ADMIN_, "ADMIN_ACCESS_DENIED");
        adjustPriceLimitCalls += 1;
        require(priceLimit <= 1e6, "INVALID_PRICE_LIMIT");
        _PRICE_LIMIT_ = priceLimit;
    }

    function setRState(uint32 r) external {
        _RState_ = r;
    }

    function buyShares(address to)
        external
        returns (uint256 shares, uint256 baseInput, uint256 quoteInput)
    {
        uint256 baseBalance = _BASE_TOKEN_.balanceOf(address(this));
        uint256 quoteBalance = _QUOTE_TOKEN_.balanceOf(address(this));
        uint256 baseReserve = _BASE_RESERVE_;
        uint256 quoteReserve = _QUOTE_RESERVE_;

        baseInput = baseBalance - baseReserve;
        quoteInput = quoteBalance - quoteReserve;

        require(baseInput > 0, "NO_BASE_INPUT");

        if (totalSupply == 0) {
            require(quoteBalance > 0, "ZERO_QUOTE_AMOUNT");
            shares = quoteBalance < mulFloor(baseBalance, _I_) ? divFloor(quoteBalance, _I_) : baseBalance;
            _BASE_TARGET_ = uint112(shares);
            _QUOTE_TARGET_ = uint112(mulFloor(shares, _I_));
            require(_QUOTE_TARGET_ > 0, "QUOTE_TARGET_IS_ZERO");
            require(shares > 2001, "MINT_AMOUNT_NOT_ENOUGH");
            _mint(address(0), 1001);
            shares -= 1001;
        } else if (baseReserve > 0 && quoteReserve > 0) {
            uint256 baseInputRatio = divFloor(baseInput, baseReserve);
            uint256 quoteInputRatio = divFloor(quoteInput, quoteReserve);
            uint256 mintRatio = quoteInputRatio < baseInputRatio ? quoteInputRatio : baseInputRatio;
            shares = mulFloor(totalSupply, mintRatio);
            _BASE_TARGET_ =
                uint112(uint256(_BASE_TARGET_) + (mulFloor(uint256(_BASE_TARGET_), mintRatio)));
            _QUOTE_TARGET_ =
                uint112(uint256(_QUOTE_TARGET_) + (mulFloor(uint256(_QUOTE_TARGET_), mintRatio)));
        }
        _mint(to, shares);
        _BASE_RESERVE_ = uint112(baseBalance);
        _QUOTE_RESERVE_ = uint112(quoteBalance);
        emit BuyShares(to, shares, _SHARES_[to]);
    }

    function getVaultReserve() external view returns (uint256 baseReserve, uint256 quoteReserve) {
        return (_BASE_RESERVE_, _QUOTE_RESERVE_);
    }

    function balanceOf(address owner) external view returns (uint256) {
        return _SHARES_[owner];
    }

    function _mint(address to, uint256 amount) internal {
        _SHARES_[to] += amount;
        totalSupply += amount;
    }
}
