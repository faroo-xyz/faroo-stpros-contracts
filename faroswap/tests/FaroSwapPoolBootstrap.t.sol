// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";

import {FaroSwapPoolBootstrap} from "../contracts/FaroSwapPoolBootstrap.sol";
import {MockGSP} from "./mocks/MockGSP.sol";
import {MockGSPFactory, MockCloneFactory} from "./mocks/MockGSPFactory.sol";
import {MockStPROS} from "./mocks/Mocks.sol";
import {MockERC20} from "./mocks/MockERC20.sol";

/// @title Atomic 0.01 stPROS bootstrap tests against the GSP-semantics mock pool.
contract FaroSwapPoolBootstrapTest is Test {
    uint256 internal constant BASE_AMOUNT = 10 ** 16; // 0.01 stPROS, fixed bootstrap budget

    address internal safe;
    address internal funder;
    address internal attacker;
    address internal maintainer;

    MockStPROS internal stpros;
    MockERC20 internal wpros;
    MockGSP internal template;
    MockGSPFactory internal factory;
    address internal pool;
    FaroSwapPoolBootstrap internal bootstrap;

    function setUp() public {
        safe = makeAddr("governance-safe");
        funder = makeAddr("funder");
        attacker = makeAddr("attacker");
        maintainer = makeAddr("faroswap-maintainer");

        stpros = new MockStPROS("stPROS", "stPROS");
        wpros = new MockERC20("WPROS", "WPROS");
        template = new MockGSP();
        factory = new MockGSPFactory(address(new MockCloneFactory()), address(template), maintainer);

        pool = factory.createDODOGasSavingPool(
            makeAddr("admin"), address(stpros), address(wpros), 5e14, 0, 1e18, 2e15, 10000, false
        );
    }

    function _quoteAmount(uint256 rate) internal pure returns (uint256) {
        // 0.01 stPROS * rate (1e18 scaling), floored — deterministic integer rounding.
        return BASE_AMOUNT * rate / 1e18;
    }

    function _deployBootstrap(uint256 rate) internal returns (FaroSwapPoolBootstrap) {
        return new FaroSwapPoolBootstrap(
            pool, address(stpros), address(wpros), funder, safe, BASE_AMOUNT, _quoteAmount(rate)
        );
    }

    function _fundAndApprove(uint256 rate) internal {
        stpros.mint(funder, BASE_AMOUNT);
        wpros.mint(funder, _quoteAmount(rate));
        vm.startPrank(funder);
        stpros.approve(address(bootstrap), BASE_AMOUNT);
        wpros.approve(address(bootstrap), _quoteAmount(rate));
        vm.stopPrank();
    }

    function _execute() internal {
        vm.prank(funder);
        bootstrap.execute();
    }

    // ---------------------------------------------------------------- happy path
    function test_bootstrap_mintsFirstLPToSafe_atomically() public {
        stpros.setRate(1e18);
        bootstrap = _deployBootstrap(1e18);
        _fundAndApprove(1e18);

        uint256 expectedShares = BASE_AMOUNT - 1001; // first mint locks 1001 shares at 0x0
        vm.expectEmit(true, true, true, true, address(bootstrap));
        emit FaroSwapPoolBootstrap.Bootstrapped(
            pool, safe, expectedShares, BASE_AMOUNT, _quoteAmount(1e18)
        );
        _execute();

        // LP belongs to the Safe, lock lives at 0x0, total includes the lock.
        assertEq(MockGSP(pool).balanceOf(safe), expectedShares);
        assertEq(MockGSP(pool).balanceOf(address(0)), 1001, "first-mint locked shares");
        assertEq(MockGSP(pool).totalSupply(), BASE_AMOUNT, "totalSupply == pre-deduction shares");

        // Targets are set from the pre-deduction share amount (GSP first-mint semantics).
        assertEq(MockGSP(pool)._BASE_TARGET_(), BASE_AMOUNT);
        assertEq(MockGSP(pool)._QUOTE_TARGET_(), BASE_AMOUNT); // i == 1e18

        // Exact tokens moved into the pool; reserves follow balances.
        assertEq(stpros.balanceOf(pool), BASE_AMOUNT);
        assertEq(wpros.balanceOf(pool), BASE_AMOUNT);
        (uint256 bR, uint256 qR) = MockGSP(pool).getVaultReserve();
        assertEq(bR, BASE_AMOUNT);
        assertEq(qR, BASE_AMOUNT);

        // No residual custody or approvals.
        assertEq(stpros.balanceOf(address(bootstrap)), 0);
        assertEq(wpros.balanceOf(address(bootstrap)), 0);
        assertEq(stpros.allowance(funder, address(bootstrap)), 0, "base allowance consumed");
        assertEq(wpros.allowance(funder, address(bootstrap)), 0, "quote allowance consumed");
    }

    function test_bootstrap_quoteScalesWithRedemptionRate() public {
        uint256 rate = 1009531657654669604; // live mainnet rate sample
        stpros.setRate(rate);
        bootstrap = _deployBootstrap(rate);
        _fundAndApprove(rate);
        _execute();

        assertEq(wpros.balanceOf(pool), _quoteAmount(rate));
        assertEq(wpros.balanceOf(pool), 10095316576546696, "0.01 * rate floored");
        // The pool derives its quote target from its OWN i (1e18 in this fixture), not from
        // the vault rate; at mainnet creation i equals the live rate, so both coincide.
        assertEq(MockGSP(pool)._QUOTE_TARGET_(), BASE_AMOUNT * MockGSP(pool)._I_() / 1e18);
        // shares = quoteBalance < baseBalance*i ? quoteBalance/i : baseBalance
        assertEq(MockGSP(pool).balanceOf(safe), BASE_AMOUNT - 1001, "rate-balanced deposit mints base value");
    }

    // ---------------------------------------------------------------- access and one-shot
    function test_bootstrap_onlyFunderMayExecute() public {
        bootstrap = _deployBootstrap(1e18);
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(FaroSwapPoolBootstrap.NotFunder.selector, attacker));
        bootstrap.execute();
    }

    function test_bootstrap_executeIsOneShot() public {
        bootstrap = _deployBootstrap(1e18);
        _fundAndApprove(1e18);
        _execute();
        vm.prank(funder);
        vm.expectRevert(FaroSwapPoolBootstrap.AlreadyExecuted.selector);
        bootstrap.execute();
    }

    // ---------------------------------------------------------------- atomicity / rollback
    function test_bootstrap_insufficientQuote_revertsWholeTx() public {
        stpros.setRate(1e18);
        bootstrap = _deployBootstrap(1e18);
        stpros.mint(funder, BASE_AMOUNT);
        wpros.mint(funder, _quoteAmount(1e18) - 1); // one wei short
        vm.startPrank(funder);
        stpros.approve(address(bootstrap), BASE_AMOUNT);
        wpros.approve(address(bootstrap), _quoteAmount(1e18));
        vm.stopPrank();

        vm.prank(funder);
        vm.expectRevert("insufficient balance");
        bootstrap.execute();

        // Nothing reached the pool and no LP exists anywhere.
        assertEq(stpros.balanceOf(pool), 0, "atomic: base returned");
        assertEq(wpros.balanceOf(pool), 0, "atomic: quote never moved");
        assertEq(MockGSP(pool).totalSupply(), 0);
    }

    function test_bootstrap_tinyAmount_poolMinimumEnforced() public {
        uint256 tiny = 2000;
        stpros.setRate(1e18);
        FaroSwapPoolBootstrap tinyBootstrap =
            new FaroSwapPoolBootstrap(pool, address(stpros), address(wpros), funder, safe, tiny, tiny);
        stpros.mint(funder, tiny);
        wpros.mint(funder, tiny);
        vm.startPrank(funder);
        stpros.approve(address(tinyBootstrap), tiny);
        wpros.approve(address(tinyBootstrap), tiny);
        vm.expectRevert("MINT_AMOUNT_NOT_ENOUGH");
        tinyBootstrap.execute();
        vm.stopPrank();
        assertEq(MockGSP(pool).totalSupply(), 0, "no partial state");
        assertEq(stpros.balanceOf(pool), 0, "no partial funds");
    }

    // ---------------------------------------------------------------- hostile scenarios
    function test_bootstrap_rejectsFrontRunFirstMint_withoutMovingFunds() public {
        // An attacker mints the first LP with their own tokens before our execute(). The pool
        // is no longer empty, so the bootstrap refuses outright — before any transferFrom —
        // leaving the funder's funds untouched.
        stpros.setRate(1e18);
        bootstrap = _deployBootstrap(1e18);
        _fundAndApprove(1e18);

        uint256 attackerBase = 5 * 10 ** 15;
        uint256 attackerQuote = 5 * 10 ** 15;
        stpros.mint(attacker, attackerBase);
        wpros.mint(attacker, attackerQuote);
        vm.startPrank(attacker);
        stpros.transfer(pool, attackerBase);
        wpros.transfer(pool, attackerQuote);
        MockGSP(pool).buyShares(attacker);
        vm.stopPrank();
        assertEq(MockGSP(pool).balanceOf(attacker), attackerBase - 1001, "attacker is first LP");
        uint256 poolBaseBefore = stpros.balanceOf(pool);

        vm.prank(funder);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPoolBootstrap.PoolAlreadySeeded.selector, attackerBase)
        );
        bootstrap.execute();

        assertEq(MockGSP(pool).balanceOf(safe), 0, "no LP leaked to the Safe");
        assertEq(stpros.balanceOf(funder), BASE_AMOUNT, "funder base untouched");
        assertEq(wpros.balanceOf(funder), BASE_AMOUNT, "funder quote untouched");
        assertEq(stpros.balanceOf(pool), poolBaseBefore, "pool untouched");
        assertEq(stpros.allowance(funder, address(bootstrap)), BASE_AMOUNT, "approval intact for retry");
    }

    function test_bootstrap_rejectsPreSeededImbalancedPool() public {
        // A pre-existing pool with heavily one-sided liquidity (huge base reserve, dust quote)
        // must be rejected before funding: proportional-case minting would absorb the budget
        // at an attacker-influenced ratio.
        stpros.setRate(1e18);
        bootstrap = _deployBootstrap(1e18);
        _fundAndApprove(1e18);

        uint256 hugeBase = 5 * 10 ** 18;
        uint256 dustQuote = 10 ** 6;
        stpros.mint(attacker, hugeBase);
        wpros.mint(attacker, dustQuote);
        vm.startPrank(attacker);
        stpros.transfer(pool, hugeBase);
        wpros.transfer(pool, dustQuote);
        MockGSP(pool).buyShares(attacker);
        vm.stopPrank();

        // First-mint shares are min(quote-side, base-side) = the quote dust: 1e6.
        assertEq(MockGSP(pool).totalSupply(), dustQuote, "imbalanced seed mints quote-side shares");
        vm.prank(funder);
        vm.expectRevert(
            abi.encodeWithSelector(FaroSwapPoolBootstrap.PoolAlreadySeeded.selector, dustQuote)
        );
        bootstrap.execute();
        assertEq(stpros.balanceOf(funder), BASE_AMOUNT, "no funds moved");
        assertEq(MockGSP(pool).balanceOf(safe), 0);
    }

    function test_bootstrap_rejectsQuoteDonation_toEmptyPool_atomically() public {
        // Donation to an EMPTY pool (totalSupply still 0): buyShares counts the donated quote
        // as our input, so the exact-input check aborts after the mint — and the atomic
        // rollback returns the funder's tokens. The attacker's donation stays in the pool,
        // untouched and valueless to them unless governance proceeds deliberately.
        stpros.setRate(1e18);
        bootstrap = _deployBootstrap(1e18);
        _fundAndApprove(1e18);

        uint256 donation = 2 * 10 ** 15;
        wpros.mint(attacker, donation);
        vm.prank(attacker);
        wpros.transfer(pool, donation);

        vm.prank(funder);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPoolBootstrap.UnexpectedPoolInputs.selector,
                BASE_AMOUNT,
                BASE_AMOUNT + donation,
                BASE_AMOUNT,
                BASE_AMOUNT
            )
        );
        bootstrap.execute();

        assertEq(MockGSP(pool).totalSupply(), 0, "no LP minted");
        assertEq(stpros.balanceOf(pool), 0, "atomic: base returned to funder");
        assertEq(wpros.balanceOf(pool), donation, "only the attacker's donation remains");
        assertEq(stpros.balanceOf(funder), BASE_AMOUNT, "funder base restored");
        assertEq(wpros.balanceOf(funder), BASE_AMOUNT, "funder quote restored");
    }

    function test_bootstrap_rejectsBaseDonation_toEmptyPool_atomically() public {
        stpros.setRate(1e18);
        bootstrap = _deployBootstrap(1e18);
        _fundAndApprove(1e18);

        uint256 donation = 3 * 10 ** 15;
        stpros.mint(attacker, donation);
        vm.prank(attacker);
        stpros.transfer(pool, donation);

        vm.prank(funder);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPoolBootstrap.UnexpectedPoolInputs.selector,
                BASE_AMOUNT + donation,
                BASE_AMOUNT,
                BASE_AMOUNT,
                BASE_AMOUNT
            )
        );
        bootstrap.execute();

        assertEq(MockGSP(pool).totalSupply(), 0, "no LP minted");
        assertEq(wpros.balanceOf(pool), 0, "atomic: quote returned to funder");
        assertEq(stpros.balanceOf(pool), donation, "only the attacker's donation remains");
        assertEq(stpros.balanceOf(funder), BASE_AMOUNT);
        assertEq(wpros.balanceOf(funder), BASE_AMOUNT);
    }

    function test_bootstrap_enforcesExactFirstMintAmount() public {
        // Happy path pins the strict LP amount: shares == BASE_AMOUNT - 1001 and inputs are
        // exactly the configured amounts (also asserted via the Bootstrapped event).
        stpros.setRate(1009531657654669604); // non-trivial rate exercises the floor path
        bootstrap = _deployBootstrap(1009531657654669604);
        _fundAndApprove(1009531657654669604);
        vm.expectEmit(true, true, true, true, address(bootstrap));
        emit FaroSwapPoolBootstrap.Bootstrapped(
            pool, safe, BASE_AMOUNT - 1001, BASE_AMOUNT, _quoteAmount(1009531657654669604)
        );
        _execute();
        assertEq(MockGSP(pool).balanceOf(safe), BASE_AMOUNT - 1001);
    }

    function test_bootstrap_rejectsWrongPairPool() public {
        address wrongPair =
            factory.createDODOGasSavingPool(makeAddr("a"), address(wpros), address(stpros), 5e14, 0, 1e18, 2e15, 10000, false);
        FaroSwapPoolBootstrap wrong =
            new FaroSwapPoolBootstrap(wrongPair, address(stpros), address(wpros), funder, safe, 1, 1);
        vm.prank(funder);
        vm.expectRevert(
            abi.encodeWithSelector(
                FaroSwapPoolBootstrap.PoolTokenMismatch.selector, address(wpros), address(stpros)
            )
        );
        wrong.execute();
    }
}
