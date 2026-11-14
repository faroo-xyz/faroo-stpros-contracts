// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// @title Subset of the deployed FaroSwap GSPFactory used for pool-creation
///        rehearsal and bind-time provenance validation.
/// @notice Signatures mirror the pinned public implementation
///         (Faroswap/contracts @ 41fefb37ae11ca6be98b6cb17977ef9f6375cf6a,
///         contracts/Factory/GSPFactory.sol). Pool creation is permissionless;
///         the maintainer is forced to the factory default and cannot be chosen
///         by the caller.
interface IFaroSwapGSPFactory {
    function _GSP_TEMPLATE_() external view returns (address);

    function _DEFAULT_MAINTAINER_() external view returns (address);

    function _CLONE_FACTORY_() external view returns (address);

    function createDODOGasSavingPool(
        address admin,
        address baseToken,
        address quoteToken,
        uint256 lpFeeRate,
        uint256 mtFeeRate,
        uint256 i,
        uint256 k,
        uint256 priceLimit,
        bool isOpenTWAP
    ) external returns (address newGasSavingPool);

    function getDODOPool(address baseToken, address quoteToken)
        external
        view
        returns (address[] memory machines);

    function getDODOPoolBidirection(address token0, address token1)
        external
        view
        returns (address[] memory baseToken0Machines, address[] memory baseToken1Machines);
}
