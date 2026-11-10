// SPDX-License-Identifier: GPL-3.0
pragma solidity 0.8.28;
import {Test} from "forge-std/Test.sol";
import {SlpPharosProxy} from "../contracts/pharos/SlpPharosProxy.sol";
import {SlpPharosRewardsVault} from "../contracts/pharos/SlpPharosRewardsVault.sol";
interface Distributor {
    struct Payment { address slp; uint256 amount; }
    function distribute(Payment[] calldata) external;
    function getSlps() external view returns(address[] memory);
    function isSlp(address) external view returns(bool);
    function keeper() external view returns(address);
    function owner() external view returns(address);
    function setSlps(address[] calldata,bool) external;
}
interface OracleView {
    function poolInfo(address) external view returns(uint256,uint256);
    function lastUpdateAt(address) external view returns(uint256);
    function maxUpdateAmount() external view returns(uint256);
    function updateInterval() external view returns(uint256);
    function maxAprPpm() external view returns(uint256);
    function canUpdate(address,uint256) external view returns(bool);
    function update(address,uint256) external;
}
interface StToken { function depositWithPROS() external payable returns(uint256); function totalSupply() external view returns(uint256); }
contract RejectRecipient { receive() external payable { revert("recipient refuses"); } }
contract ReenterRecipient {
    SlpPharosProxy public target; bool public blocked; bytes public reason;
    constructor(SlpPharosProxy t) { target=t; }
    receive() external payable { (bool ok,bytes memory data)=address(target).call(abi.encodeCall(target.transferBack,(0))); blocked=!ok; reason=data; }
}
contract ForcedDonation { constructor(address payable to) payable { selfdestruct(to); } }

/// Fork state is selected ONLY by the explicit isolated runner; no RPC fallback.
/// REAL_FORK cases use existing cash. CONTROLLED cases explicitly seed test cash,
/// time or local governance changes; none stand in for native Staking credits.
contract CurrentMainnet is Test {
    SlpPharosProxy internal a=SlpPharosProxy(payable(address(bytes20(hex"ec1d6163e05b3f5d0fb8f354881f6c8b793ad612"))));
    SlpPharosProxy internal b=SlpPharosProxy(payable(address(bytes20(hex"c53d2c9dd1e488e23ff77e219ca68d8138f90c2c"))));
    SlpPharosRewardsVault internal rv=SlpPharosRewardsVault(payable(address(bytes20(hex"d0c44d3079e97ff285dd7a3e4e0246320a9487e5"))));
    address internal constant TL=address(bytes20(hex"c30db0543412c6881a543390d2c21fbbff42805b"));
    address internal constant K=address(bytes20(hex"8736e68f37c1156e09445ac2d395a139e58fcab4"));
    address internal constant D=address(bytes20(hex"f772b375f93650f798bf724ce7a9a45c9e7a579f"));
    address internal constant O=address(bytes20(hex"051713fd66845a13bf23baca008c5c22c27ccb58"));
    address internal constant W=address(bytes20(hex"52c48d4213107b20bc583832b0d951fb9ca8f0b0"));
    address internal constant ST=address(bytes20(hex"6b0a44c64190279f7034b77c13a566e914fe5ec4"));
    address internal constant BR=address(bytes20(hex"f1df1cb080256f48e8702c24149136e4a6dec25e"));
    address internal constant PA=address(bytes20(hex"480eb384681777bf21e72b8c8538987c2801b4b7"));
    bytes32 internal constant IMPL=bytes32(uint256(keccak256("eip1967.proxy.implementation"))-1);
    bytes32 internal constant ADMIN=bytes32(uint256(keccak256("eip1967.proxy.admin"))-1);
    function setUp() public {
        assertEq(block.chainid,1672); assertEq(block.number,vm.envUint("SLP_FORK_BLOCK"));
        address[3] memory ps=[address(a),address(b),address(rv)];
        address[3] memory impls=[address(bytes20(hex"c3d088842dcf02c13699f936bb83dfbbc6f721ab")),address(bytes20(hex"d15e3eb96409ff2c5be569d132a4520e324a1124")),address(bytes20(hex"e6985303e0c917bda24c4566bb2f9af99916c531"))];
        for(uint i;i<3;i++) {
            assertEq(address(uint160(uint256(vm.load(ps[i],IMPL)))),impls[i]);
            assertEq(address(uint160(uint256(vm.load(ps[i],ADMIN)))),PA);
            assertEq(impls[i].codehash,i<2?bytes32(0x48686658ca6bf1f01e3f47ddadc34c1531b4104c2c5ed12ca785fcf235fc0aa3):bytes32(0x08c89ff66bb36f6ca361f89c06a45cbd79b480e5c5201b3cc93a4833318c50eb));
        }
    }
    function pay(address to,uint n) internal { Distributor.Payment[] memory p=new Distributor.Payment[](1);p[0]=Distributor.Payment(to,n);vm.prank(K);Distributor(D).distribute(p); }
    function seed(uint n) internal { vm.deal(address(this),n);(bool ok,)=address(rv).call{value:n}("");assertTrue(ok); }
    function ready() internal { uint next=OracleView(O).lastUpdateAt(W)+OracleView(O).updateInterval(); if(block.timestamp<next)vm.warp(next); }
    function tokenAmount() internal view returns(uint t) {(t,)=OracleView(O).poolInfo(W);}

    function test_REAL_FORK_DistinctInstancesAndGovernance() public view {
        assertTrue(address(a)!=address(b));assertTrue(a.registeredPools().length>0);assertTrue(b.registeredPools().length>0);
        assertEq(a.getRoleMemberCount(0),1);assertEq(b.getRoleMemberCount(0),1);assertEq(rv.getRoleMemberCount(0),1);
        assertEq(a.getRoleMember(0,0),TL);assertEq(b.getRoleMember(0,0),TL);assertEq(rv.getRoleMember(0,0),TL);
        assertEq(a.getRoleMemberCount(a.WHITELIST_ROLE()),1);assertEq(b.getRoleMemberCount(b.WHITELIST_ROLE()),1);assertEq(rv.getRoleMemberCount(rv.WHITELIST_ROLE()),1);assertEq(a.getRoleMember(a.WHITELIST_ROLE(),0),K);assertEq(b.getRoleMember(b.WHITELIST_ROLE(),0),K);assertEq(rv.getRoleMember(rv.WHITELIST_ROLE(),0),K);
        assertEq(Distributor(D).owner(),TL);assertEq(Distributor(D).keeper(),K);
    }
    function test_REAL_FORK_ThreeDestinationWhitelist() public view { assertEq(Distributor(D).getSlps().length,3);assertTrue(Distributor(D).isSlp(address(a)));assertTrue(Distributor(D).isSlp(address(b)));assertTrue(Distributor(D).isSlp(BR));assertFalse(Distributor(D).isSlp(address(rv))); }
    function test_REAL_FORK_RejectRewardsVaultDestinationAtomically() public {
        uint bal=address(a).balance;uint dist=D.balance;
        Distributor.Payment[] memory p=new Distributor.Payment[](2);p[0]=Distributor.Payment(address(a),1);p[1]=Distributor.Payment(address(rv),1);
        vm.prank(K);vm.expectRevert(abi.encodeWithSignature("SlpNotWhitelisted(address)",address(rv)));Distributor(D).distribute(p);
        assertEq(address(a).balance,bal);assertEq(D.balance,dist);
    }
    function test_REAL_FORK_FundBothAndReturnPrincipal() public {
        uint da=address(a).balance;uint db=address(b).balance;uint d=D.balance;uint ca=a.totalRewardClaimed();uint cb=b.totalRewardTransferred();
        pay(address(a),1 ether);pay(address(b),2 ether);assertEq(address(a).balance,da+1 ether);assertEq(address(b).balance,db+2 ether);
        vm.prank(K);a.transferBack(1 ether);vm.prank(K);b.transferBack(2 ether);assertEq(D.balance,d);assertEq(a.totalRewardClaimed(),ca);assertEq(b.totalRewardTransferred(),cb);
    }
    function test_REAL_FORK_BridgeLiquidityRoute() public { uint before_=BR.balance;pay(BR,1);assertEq(BR.balance,before_+1); }
    function test_REAL_FORK_NonKeeperDenied() public {
        bytes32 pool=b.registeredPools()[0];vm.expectRevert();a.transferBack(0);vm.expectRevert();b.claimReward(pool);vm.expectRevert();rv.notifyOracle();
        Distributor.Payment[] memory p=new Distributor.Payment[](0);vm.expectRevert(abi.encodeWithSignature("NotKeeper(address)",address(this)));Distributor(D).distribute(p);
    }
    function test_REAL_FORK_AdminAndUpgradeDenied() public {
        vm.prank(K);vm.expectRevert();a.setBridgeVault(address(this));vm.prank(K);vm.expectRevert();rv.setSlpPharosProxy(address(this));
        (bool ok,bytes memory reason)=PA.call(abi.encodeWithSignature("upgrade(address,address)",address(a),address(b)));assertFalse(ok);assertEq(reason,abi.encodeWithSignature("Error(string)","Ownable: caller is not the owner"));
        address[] memory empty=new address[](0);vm.expectRevert();a.initialize(address(this),empty,D,address(rv));
    }
    function test_REAL_FORK_DuplicateAndIndependentPools() public {
        bytes32 pool=a.registeredPools()[0];vm.prank(K);vm.expectRevert(SlpPharosProxy.PoolIdAlreadyRegistered.selector);a.registerPoolId(pool);
        bytes32 fresh=keccak256("batch3-local-membership");assertFalse(a.isPoolIdRegistered(fresh));assertFalse(b.isPoolIdRegistered(fresh));
        vm.prank(K);a.registerPoolId(fresh);assertTrue(a.isPoolIdRegistered(fresh));assertFalse(b.isPoolIdRegistered(fresh));vm.prank(K);a.unregisterPoolId(fresh);assertFalse(a.isPoolIdRegistered(fresh));
    }
    function test_REAL_FORK_UnregisteredNativeOperationsDenied() public {
        bytes32 p=keccak256("unregistered");
        vm.startPrank(K);vm.expectRevert(SlpPharosProxy.PoolIdNotRegistered.selector);a.bond(p,0);
        vm.expectRevert(SlpPharosProxy.PoolIdNotRegistered.selector);a.unbond(p);vm.expectRevert(SlpPharosProxy.PoolIdNotRegistered.selector);a.claim(p);vm.expectRevert(SlpPharosProxy.PoolIdNotRegistered.selector);a.claimReward(p);vm.stopPrank();
    }
    function test_REAL_FORK_BatchValidationAndEmptyRewards() public {
        bytes32[] memory pools=new bytes32[](2);pools[0]=a.registeredPools()[0];pools[1]=keccak256("unregistered");uint[] memory ns=new uint[](2);ns[0]=1;
        uint bonded=a.totalBonded();vm.prank(K);vm.expectRevert(SlpPharosProxy.PoolIdNotRegistered.selector);a.batchBond(pools,ns);assertEq(a.totalBonded(),bonded);
        vm.prank(K);vm.expectRevert(SlpPharosProxy.MismatchLength.selector);a.batchBond(pools,new uint[](0));
        uint claimed=a.totalRewardClaimed();vm.prank(K);a.batchClaimReward(new bytes32[](0));assertEq(a.totalRewardClaimed(),claimed);
    }
    function test_REAL_FORK_TransferCountersIndependentFromClaim() public {
        pay(address(b),1);uint ca=b.totalRewardClaimed();uint tr=b.totalRewardTransferred();uint other=a.totalRewardTransferred();uint v=address(rv).balance;
        vm.prank(K);b.transferToRewardsVault(1);assertEq(b.totalRewardClaimed(),ca);assertEq(b.totalRewardTransferred(),tr+1);assertEq(a.totalRewardTransferred(),other);assertEq(address(rv).balance,v+1);
    }
    function test_REAL_FORK_InsufficientBalanceErrorOverload() public {vm.prank(K);vm.expectRevert(bytes4(keccak256("InsufficientBalance()")));a.transferBack(address(a).balance+1);}
    function test_REAL_FORK_OracleReporterDenied() public {vm.prank(K);vm.expectRevert(abi.encodeWithSignature("NotSlp(address)",K));OracleView(O).update(W,1);}

    function test_CONTROLLED_DepositWprosUnwrapAndDualRouting() public {
        vm.deal(address(this),2 ether);uint d=D.balance;uint t=tokenAmount();StToken(ST).depositWithPROS{value:2 ether}();assertEq(D.balance,d+2 ether);assertEq(tokenAmount(),t+2 ether);pay(address(a),1 ether);pay(address(b),1 ether);assertEq(D.balance,d);
    }
    function test_CONTROLLED_DonationWholeBalanceReportAndNoSecondReport() public {
        seed(1 ether);ready();uint n=address(rv).balance;assertTrue(OracleView(O).canUpdate(W,n));uint t=tokenAmount();uint bal=address(a).balance;uint claimed=a.totalRewardClaimed();
        vm.prank(K);rv.notifyOracle();assertEq(tokenAmount(),t+n);assertEq(address(a).balance,bal+n);assertEq(address(rv).balance,0);assertEq(a.totalRewardClaimed(),claimed);
        vm.prank(K);vm.expectRevert(bytes4(keccak256("ZeroUpdateAmount()")));rv.notifyOracle();assertEq(tokenAmount(),t+n);
    }
    function test_CONTROLLED_ForcedDonationIsReportedAsYield() public {vm.deal(address(this),1 ether);new ForcedDonation{value:1 ether}(payable(address(rv)));ready();uint n=address(rv).balance;uint t=tokenAmount();vm.prank(K);rv.notifyOracle();assertEq(tokenAmount(),t+n);}
    function test_CONTROLLED_PrincipalCanBeMisreportedAndCountersExceedClaims() public {
        pay(address(b),1 ether);uint c=b.totalRewardClaimed();uint tr=b.totalRewardTransferred();vm.prank(K);b.transferToRewardsVault(1 ether);assertEq(b.totalRewardClaimed(),c);assertEq(b.totalRewardTransferred(),tr+1 ether);assertGt(b.totalRewardTransferred(),b.totalRewardClaimed());
        ready();uint n=address(rv).balance;uint t=tokenAmount();vm.prank(K);rv.notifyOracle();assertEq(tokenAmount(),t+n);
    }
    function test_CONTROLLED_AmountCapFailureAtomicity() public {
        uint cap=OracleView(O).maxUpdateAmount();seed(cap+1);ready();uint n=address(rv).balance;uint t=tokenAmount();uint supply=StToken(ST).totalSupply();uint last=OracleView(O).lastUpdateAt(W);
        vm.prank(K);vm.expectRevert(abi.encodeWithSignature("UpdateAmountTooLarge(uint256,uint256)",n,cap));rv.notifyOracle();assertEq(address(rv).balance,n);assertEq(tokenAmount(),t);assertEq(StToken(ST).totalSupply(),supply);assertEq(OracleView(O).lastUpdateAt(W),last);
    }
    function test_CONTROLLED_AprAllowanceBelowAbsoluteCap() public {
        ready();uint elapsed=block.timestamp-OracleView(O).lastUpdateAt(W);if(elapsed>7 days)elapsed=7 days;
        uint allowance_=tokenAmount()*OracleView(O).maxAprPpm()*elapsed/(365 days*1_000_000);
        uint n=allowance_+1;assertLt(n,OracleView(O).maxUpdateAmount());seed(n);uint t=tokenAmount();
        vm.prank(K);vm.expectRevert(abi.encodeWithSignature("UpdateAmountTooLarge(uint256,uint256)",address(rv).balance,allowance_));rv.notifyOracle();assertEq(tokenAmount(),t);
    }
    function test_CONTROLLED_ReportThenRestakeDoesNotReportTwice() public {
        seed(1 ether);ready();uint n=address(rv).balance;uint t=tokenAmount();uint bonded=a.totalBonded();bytes32 pool=a.registeredPools()[0];
        vm.prank(K);rv.notifyOracle();assertEq(tokenAmount(),t+n);vm.prank(K);a.bond(pool,n);assertEq(a.totalBonded(),bonded+n);assertEq(tokenAmount(),t+n);
    }
    function test_CONTROLLED_IntervalBoundary() public {
        seed(1);uint next=OracleView(O).lastUpdateAt(W)+OracleView(O).updateInterval();vm.warp(next-1);uint n=address(rv).balance;
        vm.prank(K);vm.expectRevert(abi.encodeWithSignature("UpdateTooFrequent(uint256)",next));rv.notifyOracle();assertEq(address(rv).balance,n);vm.warp(next);vm.prank(K);rv.notifyOracle();assertEq(address(rv).balance,0);
    }
    function test_CONTROLLED_FailedRecipientRollsBackOracleAndRecovery() public {
        seed(1 ether);ready();RejectRecipient r=new RejectRecipient();vm.prank(TL);rv.setSlpPharosProxy(address(r));uint t=tokenAmount();uint supply=StToken(ST).totalSupply();uint last=OracleView(O).lastUpdateAt(W);uint n=address(rv).balance;
        vm.prank(K);vm.expectRevert();rv.notifyOracle();assertEq(tokenAmount(),t);assertEq(StToken(ST).totalSupply(),supply);assertEq(OracleView(O).lastUpdateAt(W),last);assertEq(address(rv).balance,n);
        vm.prank(TL);rv.setSlpPharosProxy(address(a));vm.prank(K);rv.notifyOracle();assertEq(tokenAmount(),t+n);
    }
    function test_CONTROLLED_PrivilegedReentrantRecipientBlocked() public {
        pay(address(a),1);ReenterRecipient r=new ReenterRecipient(a);vm.startPrank(TL);a.grantRole(a.WHITELIST_ROLE(),address(r));a.setBridgeVault(address(r));vm.stopPrank();vm.prank(K);a.transferBack(1);assertTrue(r.blocked());assertEq(r.reason(),abi.encodeWithSignature("ReentrancyGuardReentrantCall()"));assertEq(address(r).balance,1);
    }
    function test_CONTROLLED_FailedReturnDoesNotConsumeCash() public {pay(address(a),1);RejectRecipient r=new RejectRecipient();vm.prank(TL);a.setBridgeVault(address(r));uint bal=address(a).balance;vm.prank(K);vm.expectRevert();a.transferBack(1);assertEq(address(a).balance,bal);}
    function test_CONTROLLED_DistributorRecipientFailureRollsBackEarlierPayment() public {
        RejectRecipient r=new RejectRecipient();address[] memory xs=new address[](1);xs[0]=address(r);vm.prank(TL);Distributor(D).setSlps(xs,true);
        Distributor.Payment[] memory p=new Distributor.Payment[](2);p[0]=Distributor.Payment(address(a),1);p[1]=Distributor.Payment(address(r),1);uint bal=address(a).balance;uint d=D.balance;vm.prank(K);vm.expectRevert(abi.encodeWithSignature("TransferFailed(address,uint256)",address(r),1));Distributor(D).distribute(p);assertEq(address(a).balance,bal);assertEq(D.balance,d);
    }
}

/// Diagnostic native boundaries: failures remain failures in the captured log.
/// Success proves EVM execution only, never node-generated native credits.
contract NativeBoundary is CurrentMainnet {
    function test_NATIVE_BondBothRealPositions() public {
        pay(address(a),1 ether);pay(address(b),1 ether);uint x=a.totalBonded();uint y=b.totalBonded();
        bytes32 pa=a.registeredPools()[0];bytes32 pb=b.registeredPools()[0];vm.prank(K);a.bond(pa,1 ether);assertEq(a.totalBonded(),x+1 ether);assertEq(b.totalBonded(),y);
        vm.prank(K);b.bond(pb,1 ether);assertEq(b.totalBonded(),y+1 ether);
    }
    function test_NATIVE_ClaimRewardNoSyntheticCredit() public {
        bytes32 p=a.registeredPools()[0];uint c=a.totalRewardClaimed();uint cash=address(a).balance;uint v=address(rv).balance;
        uint transferred=a.totalRewardTransferred();vm.prank(K);a.claimReward(p);uint delta=a.totalRewardClaimed()-c;assertGt(delta,0);emit log_named_uint("claimed counter delta",delta);emit log_named_uint("synchronous cash delta",address(a).balance-cash);assertEq(address(rv).balance,v);assertEq(a.totalRewardTransferred(),transferred);
        // This pin has no starting cash: the EVM call cannot forward missing node credits.
        assertEq(cash,0);assertEq(address(a).balance,0);vm.prank(K);vm.expectRevert(bytes4(keccak256("InsufficientBalance()")));a.transferToRewardsVault(delta);assertEq(a.totalRewardClaimed(),c+delta);
    }
    function test_NATIVE_DuplicateRewardPoolAndZeroSkip() public {
        bytes32 p=a.registeredPools()[0];bytes32[] memory pools=new bytes32[](2);pools[0]=p;pools[1]=p;uint c=a.totalRewardClaimed();
        vm.prank(K);a.batchClaimReward(pools);uint delta=a.totalRewardClaimed()-c;assertGt(delta,0);assertEq(a.STAKING().getDelegatorReward(p,address(a)),0);
        vm.prank(K);a.batchClaimReward(pools);assertEq(a.totalRewardClaimed(),c+delta);
    }
    function test_NATIVE_LaterPoolFailureRollsBackEarlierClaim() public {
        bytes32 p=a.registeredPools()[0];bytes32[] memory pools=new bytes32[](2);pools[0]=p;pools[1]=keccak256("unregistered");uint c=a.totalRewardClaimed();uint reward=a.STAKING().getDelegatorReward(p,address(a));assertGt(reward,0);
        vm.prank(K);vm.expectRevert(SlpPharosProxy.PoolIdNotRegistered.selector);a.batchClaimReward(pools);assertEq(a.totalRewardClaimed(),c);assertEq(a.STAKING().getDelegatorReward(p,address(a)),reward);
    }
    function test_NATIVE_UnbondAndImmediateClaimBoundary() public {
        bytes32 p=b.registeredPools()[0];uint before_=b.totalUnbonded();vm.prank(K);b.unbond(p);emit log_named_uint("unbond amount",b.totalUnbonded()-before_);
        uint cash=address(b).balance;vm.prank(K);(bool ok,bytes memory reason)=address(b).call(abi.encodeCall(b.claim,(p)));emit log_named_uint("immediate claim success",ok?1:0);emit log_named_bytes("claim response",reason);emit log_named_uint("claim cash delta",address(b).balance-cash);
    }
}
