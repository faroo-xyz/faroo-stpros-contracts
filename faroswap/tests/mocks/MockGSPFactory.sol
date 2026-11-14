// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {MockGSP} from "./MockGSP.sol";

interface ITestCloneFactory {
    function clone(address prototype) external returns (address proxy);
}

/// @title Test double of the deployed CloneFactory (EIP-1167 `create`, non-deterministic).
contract MockCloneFactory is ITestCloneFactory {
    function clone(address prototype) external override returns (address proxy) {
        bytes20 targetBytes = bytes20(prototype);
        assembly {
            let clone := mload(0x40)
            mstore(clone, 0x3d602d80600a3d3981f3363d3d373d3d3d363d73000000000000000000000000)
            mstore(add(clone, 0x14), targetBytes)
            mstore(add(clone, 0x28), 0x5af43d82803e903d91602b57fd5bf30000000000000000000000000000000000)
            proxy := create(0, clone, 0x37)
        }
        return proxy;
    }
}

/// @title Test double of the deployed GSPFactory (registry + forced default maintainer).
contract MockGSPFactory {
    MockCloneFactory public immutable _CLONE_FACTORY_;
    address public _DEFAULT_MAINTAINER_;
    address public _GSP_TEMPLATE_;

    mapping(address => mapping(address => address[])) public _REGISTRY_;

    event NewGSP(address baseToken, address quoteToken, address creator, address GSP);

    constructor(address cloneFactory, address gspTemplate, address defaultMaintainer) {
        _CLONE_FACTORY_ = MockCloneFactory(cloneFactory);
        _GSP_TEMPLATE_ = gspTemplate;
        _DEFAULT_MAINTAINER_ = defaultMaintainer;
    }

    function updateGSPTemplate(address newTemplate) external {
        _GSP_TEMPLATE_ = newTemplate;
    }

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
    ) external returns (address newGasSavingPool) {
        newGasSavingPool = _CLONE_FACTORY_.clone(_GSP_TEMPLATE_);
        MockGSP(newGasSavingPool).init(
            _DEFAULT_MAINTAINER_,
            admin,
            baseToken,
            quoteToken,
            lpFeeRate,
            mtFeeRate,
            i,
            k,
            priceLimit,
            isOpenTWAP
        );
        _REGISTRY_[baseToken][quoteToken].push(newGasSavingPool);
        emit NewGSP(baseToken, quoteToken, msg.sender, newGasSavingPool);
    }

    function getDODOPool(address baseToken, address quoteToken)
        external
        view
        returns (address[] memory machines)
    {
        return _REGISTRY_[baseToken][quoteToken];
    }
}
