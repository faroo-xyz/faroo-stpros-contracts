// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";
import {ERC1967Utils} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Utils.sol";
import {Initializable} from "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import {Ownable2StepUpgradeable} from
    "@openzeppelin/contracts-upgradeable/access/Ownable2StepUpgradeable.sol";

import {IFaroSwapGSP} from "./interfaces/IFaroSwapGSP.sol";
import {IFaroSwapGSPFactory} from "./interfaces/IFaroSwapGSPFactory.sol";
import {IERC1822Proxiable} from "./interfaces/IERC1822Proxiable.sol";

/**
 * @title FaroSwap GSP Price Manager
 * @notice Upgradeable admin of the public FaroSwap stPROS/WPROS GSP pool. It holds exactly
 *         the two admin capabilities the GSP implementation grants:
 *           1. `syncPrice()` — an authorized executor triggers it, the contract itself reads
 *              the fixed stPROS ERC-4626 redemption rate, computes the target `i` internally
 *              and calls `pool.adjustPrice(targetI)`. Executors cannot supply a price, a
 *              source, a formula, a target contract or arbitrary calldata.
 *           2. `setPriceLimit(uint256)` — governance-Safe only, calls `pool.adjustPriceLimit`.
 *         Business ownership and the upgrade authority are both the governance Safe; executors
 *         hold neither. The target pool is bound once and validated before the binding sticks.
 *
 * @dev Upgrade model: an ERC-1967 proxy (deployed from the vendored `ERC1967Proxy`) delegates
 *      to this implementation; `upgradeToAndCall` is owner-only and reuses the vendored
 *      `ERC1967Utils` safety checks. The bare implementation is inert: `initialize` is disabled
 *      in its constructor, so owner remains zero on the implementation itself and on any
 *      attacker-erected proxy of it.
 *
 *      Reentrancy: every external call targets either the bound pool — proven at bind time to
 *      be an exact EIP-1167 clone of the verified GSP 1.0.1 template, whose `adjustPrice` /
 *      `adjustPriceLimit` / `buyShares` never call out — or the fixed view `convertToAssets`
 *      on the frozen stPROS vault. No untrusted callback surface exists, so no reentrancy
 *      guard is installed; the clone-code proof at binding is the load-bearing control.
 *
 *      The sync cadence (default ~4h) lives in the off-chain scheduler and is intentionally
 *      NOT encoded here; the contract applies no cooldown. Executors and the scheduler read
 *      `previewSync()` to skip broadcasting when the target `i` already equals the pool `i`;
 *      `syncPrice()` re-derives everything on-chain and returns without state changes, pool
 *      calls or events in that case, after authorization and source validation.
 */
contract FaroSwapPriceManager is Initializable, Ownable2StepUpgradeable {
    // ------------------------------------------------------------------ errors
    error NotExecutor(address caller);
    error PoolNotBound();
    error PoolAlreadyBound();
    error ZeroAddress();
    error InvalidTargetI(uint256 targetI);
    error PriceLimitAboveMax(uint256 limit);
    error PoolNotContract(address pool);
    error PoolNotClone(address pool, uint256 codeSize);
    error PoolTemplateMismatch(address pool, address cloned, address expectedTemplate);
    error FactoryTemplateChanged(address liveTemplate, address expectedTemplate);
    error PoolAdminMismatch(address poolAdmin, address self);
    error PoolTokensMismatch(address base, address quote);
    error PoolNotInRegistry(address pool);
    error PoolCreationParamsMismatch(
        address pool, uint256 lpFeeRate, uint256 mtFeeRate, uint256 k, uint256 priceLimit
    );
    error PoolNotInitialized(address pool);
    error OwnershipRenounceDisabled();
    error UnsupportedProxiableUUID(address implementation);
    error UUPSUnauthorizedCallContext();
    error UpgradeToSelfProxy(address target);
    error PoolNotEmpty(uint256 totalSupply);
    error BindPriceDeviation(uint256 poolI, uint256 vaultRate, uint256 tolerancePpm);

    // ------------------------------------------------------------------ events
    event ExecutorUpdated(address indexed executor, bool allowed);
    event PoolBound(address indexed pool, uint256 initialI);
    event PriceSynced(uint256 oldI, uint256 newI);
    event PriceLimitUpdated(uint256 newLimit);

    // ------------------------------------------------------- fixed protocol identity
    /// @notice stPROS vault (base token and the fixed redemption-rate source).
    address public immutable STPROS_VAULT;
    /// @notice WPROS (quote token).
    address public immutable QUOTE_TOKEN;
    /// @notice Verified FaroSwap GSPFactory; used for bind-time provenance checks.
    address public immutable GSP_FACTORY;
    /// @notice Verified GSP 1.0.1 template the factory must still point at when binding.
    address public immutable VERIFIED_GSP_TEMPLATE;
    /// @notice Creation parameters the bound pool must show (see docs/PARAMS.md).
    uint256 public immutable EXPECTED_LP_FEE_RATE;
    uint256 public immutable EXPECTED_MT_FEE_RATE;
    uint256 public immutable EXPECTED_K;
    uint256 public immutable EXPECTED_PRICE_LIMIT;

    // ------------------------------------------------------------- protocol bounds
    /// @notice Upper bound the GSP `init` applies to `i`; mirrored before any `adjustPrice`.
    uint256 public constant MAX_I = 10 ** 36;
    /// @notice GSP protocol bound for `priceLimit` (1e6 precision; 1,000,000 = 100%).
    uint256 public constant MAX_PRICE_LIMIT = 10 ** 6;
    /// @notice One whole stPROS share; the fixed input the redemption rate is read for.
    uint256 public constant SHARE_UNIT = 10 ** 18;

    // ------------------------------------------------------------- namespaced storage
    /// @custom:storage-location erc7201:faroswap.price-manager.v1
    struct PriceManagerStorage {
        mapping(address executor => bool allowed) isExecutor;
        address pool;
        bool poolBound;
    }

    // keccak256(abi.encode(uint256(keccak256("faroswap.price-manager.v1")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant STORAGE = 0x1b7714590d5370353247b3299283b707765d01564ce2954820f2462bb8b85f00;

    function _pm() private pure returns (PriceManagerStorage storage s) {
        assembly {
            s.slot := STORAGE
        }
    }

    // ------------------------------------------------------------------ modifiers
    modifier onlyExecutor() {
        if (!_pm().isExecutor[msg.sender]) revert NotExecutor(msg.sender);
        _;
    }

    modifier whenPoolBound() {
        if (!_pm().poolBound) revert PoolNotBound();
        _;
    }

    // ---------------------------------------------------------------- constructor
    /// @notice Freezes the protocol identity and creation-parameter expectations into the
    ///         implementation. An upgraded implementation repeats these values (or updated,
    ///         explicitly reviewed ones); they are read from the deployment package.
    constructor(
        address stprosVault_,
        address quoteToken_,
        address gspFactory_,
        address verifiedGspTemplate_,
        uint256 expectedLpFeeRate_,
        uint256 expectedMtFeeRate_,
        uint256 expectedK_,
        uint256 expectedPriceLimit_
    ) {
        if (
            stprosVault_ == address(0) || quoteToken_ == address(0) || gspFactory_ == address(0)
                || verifiedGspTemplate_ == address(0)
        ) revert ZeroAddress();
        STPROS_VAULT = stprosVault_;
        QUOTE_TOKEN = quoteToken_;
        GSP_FACTORY = gspFactory_;
        VERIFIED_GSP_TEMPLATE = verifiedGspTemplate_;
        EXPECTED_LP_FEE_RATE = expectedLpFeeRate_;
        EXPECTED_MT_FEE_RATE = expectedMtFeeRate_;
        EXPECTED_K = expectedK_;
        EXPECTED_PRICE_LIMIT = expectedPriceLimit_;
        _disableInitializers();
    }

    /// @notice Proxy initialization: the governance Safe becomes the business owner. Executors
    ///         are NOT granted here; `setExecutor` is the explicit, independently verifiable
    ///         authorization step.
    function initialize(address initialOwner) external initializer {
        if (initialOwner == address(0)) revert ZeroAddress();
        __Ownable_init(initialOwner);
    }

    // ---------------------------------------------------------------- executor set
    /// @notice governance-Safe only. Grants or revokes the sync-trigger permission. This
    ///         permission carries no upgrade, pool-management, limit or funds authority.
    function setExecutor(address executor, bool allowed) external onlyOwner {
        if (executor == address(0)) revert ZeroAddress();
        _pm().isExecutor[executor] = allowed;
        emit ExecutorUpdated(executor, allowed);
    }

    // ---------------------------------------------------------------- one-time binding
    /// @notice governance-Safe only, one-time. Validates and permanently binds the target pool:
    ///         exact EIP-1167 clone of the factory's current template, which must still be the
    ///         verified GSP 1.0.1 template; created through the factory (registry membership);
    ///         admin == this proxy; base == stPROS and quote == WPROS; initialized (`i > 0`);
    ///         and creation economics exactly the confirmed parameters. Executors cannot bind
    ///         or re-bind. A wrong pool fails loudly and leaves the manager unbound.
    function bindPool(address pool) external onlyOwner {
        PriceManagerStorage storage s = _pm();
        if (s.poolBound) revert PoolAlreadyBound();
        if (pool == address(0)) revert ZeroAddress();
        if (pool.code.length == 0) revert PoolNotContract(pool);

        IFaroSwapGSPFactory factory = IFaroSwapGSPFactory(GSP_FACTORY);
        address liveTemplate = factory._GSP_TEMPLATE_();
        if (liveTemplate != VERIFIED_GSP_TEMPLATE) {
            revert FactoryTemplateChanged(liveTemplate, VERIFIED_GSP_TEMPLATE);
        }
        address cloned = _eip1167TemplateOf(pool);
        if (cloned == address(0)) revert PoolNotClone(pool, pool.code.length);
        if (cloned != liveTemplate) revert PoolTemplateMismatch(pool, cloned, liveTemplate);

        if (IFaroSwapGSP(pool)._ADMIN_() != address(this)) {
            revert PoolAdminMismatch(IFaroSwapGSP(pool)._ADMIN_(), address(this));
        }
        if (IFaroSwapGSP(pool)._BASE_TOKEN_() != STPROS_VAULT
            || IFaroSwapGSP(pool)._QUOTE_TOKEN_() != QUOTE_TOKEN) {
            revert PoolTokensMismatch(IFaroSwapGSP(pool)._BASE_TOKEN_(), IFaroSwapGSP(pool)._QUOTE_TOKEN_());
        }
        uint256 poolI = IFaroSwapGSP(pool)._I_();
        if (poolI == 0) revert PoolNotInitialized(pool);
        if (
            IFaroSwapGSP(pool)._LP_FEE_RATE_() != EXPECTED_LP_FEE_RATE
                || IFaroSwapGSP(pool)._MT_FEE_RATE_() != EXPECTED_MT_FEE_RATE
                || IFaroSwapGSP(pool)._K_() != EXPECTED_K
                || IFaroSwapGSP(pool)._PRICE_LIMIT_() != EXPECTED_PRICE_LIMIT
        ) {
            revert PoolCreationParamsMismatch(
                pool,
                IFaroSwapGSP(pool)._LP_FEE_RATE_(),
                IFaroSwapGSP(pool)._MT_FEE_RATE_(),
                IFaroSwapGSP(pool)._K_(),
                IFaroSwapGSP(pool)._PRICE_LIMIT_()
            );
        }

        // The pool must still be empty: binding is permanent, and a seeded pool (front-run
        // first deposit or accident) would fix the LP shape chosen by a third party. An
        // abandoned pool is cheap to replace; a wrongly bound one is not.
        uint256 poolSupply = IFaroSwapGSP(pool).totalSupply();
        if (poolSupply != 0) revert PoolNotEmpty(poolSupply);

        // Price consistency at binding, measured EXACTLY as the pool's own adjustPrice will
        // measure the first synchronization: floor(|vaultRate - poolI| * 1e6 / poolI) <=
        // priceLimit, i.e. the same offset, the same old-price denominator and the same floor
        // division the verified GSP code applies to newI = vaultRate. A pool accepted here is
        // therefore guaranteed to synchronize at least once under its confirmed 1% limit; a
        // pool born too far below the oracle rate (smaller denominator, larger relative step)
        // is refused instead of being bound permanently unsyncable. Fixed vault source,
        // read-only, no ongoing price control.
        uint256 vaultRate = _targetI();
        uint256 offset = poolI > vaultRate ? poolI - vaultRate : vaultRate - poolI;
        if (offset * 10 ** 6 / poolI > EXPECTED_PRICE_LIMIT) {
            revert BindPriceDeviation(poolI, vaultRate, EXPECTED_PRICE_LIMIT);
        }

        address[] memory registry = factory.getDODOPool(STPROS_VAULT, QUOTE_TOKEN);
        bool found;
        for (uint256 idx = 0; idx < registry.length; idx++) {
            if (registry[idx] == pool) {
                found = true;
                break;
            }
        }
        if (!found) revert PoolNotInRegistry(pool);

        s.pool = pool;
        s.poolBound = true;
        emit PoolBound(pool, poolI);
    }

    // ---------------------------------------------------------------- price sync
    /// @notice Authorized-executor trigger. Reads the fixed redemption rate, computes the
    ///         target `i`, and — only when it differs from the pool `i` — calls
    ///         `pool.adjustPrice(targetI)`. Authorization and source validation precede the
    ///         same-price return: an unauthorized caller or an invalid source always reverts,
    ///         never silently no-ops. A pool rejection (e.g. EXCEED_PRICE_LIMIT) reverts the
    ///         call; nothing partial is written. Only `i` changes; the pool implementation
    ///         itself touches no other state in `adjustPrice`.
    function syncPrice() external onlyExecutor whenPoolBound {
        uint256 newI = _targetI();
        uint256 currentI = IFaroSwapGSP(_pm().pool)._I_();
        if (currentI == newI) {
            // Same price: return normally. No pool call, no state write, no event.
            return;
        }
        IFaroSwapGSP(_pm().pool).adjustPrice(newI);
        emit PriceSynced(currentI, newI);
    }

    /// @notice Off-chain view for the scheduler's gas-saving pre-check (BR-13): recompute the
    ///         target `i` and compare with the pool. `needed == false` means a sync broadcast
    ///         would be a no-op. The scheduler decision is advisory only; `syncPrice` always
    ///         re-derives on-chain.
    function previewSync() external view returns (uint256 newTargetI, uint256 currentI, bool needed) {
        if (!_pm().poolBound) revert PoolNotBound();
        newTargetI = _targetI();
        currentI = IFaroSwapGSP(_pm().pool)._I_();
        needed = newTargetI != currentI;
    }

    /// @notice The economic definition of `i`, identical for pool creation and every sync:
    ///         WPROS obtained for redeeming exactly one stPROS share, floored, 1e18-scaled
    ///         (`stPROS.convertToAssets(1e18)`), i.e. quote per base as GSP defines `i`.
    function targetI() external view returns (uint256) {
        return _targetI();
    }

    function _targetI() internal view returns (uint256 rate) {
        rate = IERC4626(STPROS_VAULT).convertToAssets(SHARE_UNIT);
        if (rate == 0 || rate > MAX_I) revert InvalidTargetI(rate);
    }

    // ---------------------------------------------------------------- price limit
    /// @notice governance-Safe only. Sets the pool's single-update price-move limit (1e6
    ///         precision, 0..1,000,000 inclusive). Validated locally so an over-range value
    ///         reverts cleanly without touching the pool. Executors cannot call this; there is
    ///         no automatic limit relaxation and no stepping around it.
    function setPriceLimit(uint256 newLimit) external onlyOwner whenPoolBound {
        if (newLimit > MAX_PRICE_LIMIT) revert PriceLimitAboveMax(newLimit);
        IFaroSwapGSP(_pm().pool).adjustPriceLimit(newLimit);
        emit PriceLimitUpdated(newLimit);
    }

    // ---------------------------------------------------------------- upgrades
    /// @dev The implementation's own address, frozen at construction (immutables are read
    ///      from the implementation's code even through a delegatecall, so `__self` differs
    ///      from `address(this)` exactly when the code runs behind a proxy).
    address private immutable __self = address(this);

    /// @dev Reverts outside a proxy: a direct call on the bare implementation must never be
    ///      able to move the (nonexistent for it) proxy slot or pose as an upgrade path.
    ///      Also refuses the ERC-1967 transparent-proxy admin as caller, mirroring the
    ///      OpenZeppelin UUPSUpgradeable `_checkProxy` semantics.
    modifier onlyProxy() {
        if (address(this) == __self || ERC1967Utils.getAdmin() == msg.sender) {
            revert UUPSUnauthorizedCallContext();
        }
        _;
    }

    /// @dev Reverts inside a proxy: the ERC-1822 signal is only meaningful probed directly
    ///      on the implementation (as `upgradeToAndCall` does); through a delegatecall it
    ///      would answer for the proxy, not for the implementation being certified.
    modifier notDelegateCall() {
        if (address(this) != __self) revert UUPSUnauthorizedCallContext();
        _;
    }

    /// @notice ERC-1822 compatibility signal: this implementation is meant to sit behind an
    ///         ERC-1967 proxy at the implementation slot. Direct calls return the slot; calls
    ///         routed through any proxy revert.
    function proxiableUUID() external view notDelegateCall returns (bytes32) {
        return ERC1967Utils.IMPLEMENTATION_SLOT;
    }

    /// @notice governance-Safe only, and only through the proxy. Upgrades in place; the pool
    ///         admin association (this proxy address) is unchanged. Storage-compatible
    ///         implementations preserve owner, executors and the pool binding; layout
    ///         compatibility is a release-gate check, not something this function can verify
    ///         at runtime. The candidate must expose the ERC-1822 `proxiableUUID()` signal
    ///         returning the ERC-1967 implementation slot (the standard OpenZeppelin UUPS
    ///         check): that inherently rejects proxies and clones of compliant implementations
    ///         (their delegated signal reverts), and the explicit self-reference check below
    ///         rejects the proxy's own address with a precise error.
    function upgradeToAndCall(address newImplementation, bytes calldata data)
        external
        payable
        onlyProxy
        onlyOwner
    {
        if (newImplementation == address(this)) revert UpgradeToSelfProxy(newImplementation);
        _checkImplementationCompatibility(newImplementation);
        ERC1967Utils.upgradeToAndCall(newImplementation, data);
    }

    function _checkImplementationCompatibility(address candidate) private view {
        (bool ok, bytes memory ret) =
            candidate.staticcall(abi.encodeCall(IERC1822Proxiable.proxiableUUID, ()));
        if (!ok || ret.length != 32 || abi.decode(ret, (bytes32)) != ERC1967Utils.IMPLEMENTATION_SLOT) {
            revert UnsupportedProxiableUUID(candidate);
        }
    }

    // ---------------------------------------------------------------- views
    function pool() external view returns (address) {
        return _pm().pool;
    }

    function poolBound() external view returns (bool) {
        return _pm().poolBound;
    }

    function isExecutor(address account) external view returns (bool) {
        return _pm().isExecutor[account];
    }

    // ---------------------------------------------------------------- internals
    /// @dev Returns the implementation address embedded in an EIP-1167 minimal-proxy runtime,
    ///      or zero when `at` does not carry exactly the 45-byte clone runtime.
    function _eip1167TemplateOf(address at) private view returns (address implementation) {
        if (at.code.length != 45) return address(0);
        bytes memory code = at.code;
        // 363d3d373d3d3d363d73<20-byte address>5af43d82803e903d91602b57fd5bf3
        bytes10 prefix = 0x363d3d373d3d3d363d73;
        bytes15 suffix = 0x5af43d82803e903d91602b57fd5bf3;
        for (uint256 i = 0; i < 10; i++) {
            if (code[i] != prefix[i]) return address(0);
        }
        for (uint256 i = 0; i < 15; i++) {
            if (code[30 + i] != suffix[i]) return address(0);
        }
        uint160 impl;
        for (uint256 i = 0; i < 20; i++) {
            impl |= uint160(uint8(code[10 + i])) << uint160(8 * (19 - i));
        }
        return address(impl);
    }

    // ---------------------------------------------------------------- ownable guards
    /// @dev Renunciation would strand the pool admin forever (GSP has no admin transfer), so
    ///      it is disabled, mirroring the repository's GuardedOwnableUpgradeable precedent.
    function renounceOwnership() public virtual override onlyOwner {
        revert OwnershipRenounceDisabled();
    }
}
