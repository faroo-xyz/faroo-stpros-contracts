// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// @title ERC-1822 proxiableUUID, the standard UUPS implementation-compatibility signal.
/// @notice Mirrors the OpenZeppelin `IERC1822Proxiable` surface (not vendored in the frozen
///         root closure). A contract is accepted as an upgrade target only if a staticcall to
///         `proxiableUUID()` returns the ERC-1967 implementation slot — the same check
///         OpenZeppelin's UUPSUpgradeable performs before switching implementations.
interface IERC1822Proxiable {
    /// @notice MUST return the storage slot the proxy reads the implementation address from.
    function proxiableUUID() external view returns (bytes32);
}
