// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Burnable} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title ProjectToken
 * @notice A standard ERC-20 with two distinct powers, deliberately separated:
 *
 *   owner    - sets WHO may mint. Can hand the role to any address, at any
 *              time, and can transfer ownership itself. Cannot mint directly,
 *              and cannot raise the supply cap.
 *   operator - mints, up to the immutable cap. Exactly what you want on a hot
 *              wallet: it can create tokens and nothing else.
 *
 * The cap is `immutable`: it is fixed at deployment and baked into the
 * bytecode, so no key anywhere can raise it later. That is the difference
 * between "the operator can issue more" and "the operator can issue infinitely
 * more", and it is the reason the cap is not a storage variable.
 *
 * Burning is included (OpenZeppelin ERC20Burnable, unprivileged: a holder can
 * only burn what they already own or have been approved to spend). Burned
 * supply frees room under the cap, so `totalSupply()` is the number the cap
 * constrains, not the cumulative amount ever minted.
 *
 * Not included, on purpose: pausing, blacklisting, fees, and rebasing. Each of
 * those puts a discretionary power in someone's hands, and none is needed to
 * issue a normal token.
 */
contract ProjectToken is ERC20, ERC20Burnable, Ownable {
    /// @notice Hard ceiling on totalSupply(), fixed forever at deployment.
    uint256 public immutable maxSupply;

    /// @notice Addresses allowed to call mint().
    mapping(address account => bool allowed) public isOperator;

    /// @notice Emitted whenever mint rights are granted or revoked.
    event OperatorUpdated(address indexed operator, bool allowed);

    /// @notice Thrown when an address without mint rights calls mint().
    error NotOperator(address account);

    /// @notice Thrown when a mint would push totalSupply() past maxSupply.
    error MaxSupplyExceeded(uint256 requested, uint256 cap);

    /// @notice Thrown for a zero address where one is meaningless.
    error ZeroAddress();

    /// @notice Thrown when the cap would be below the supply minted at deploy.
    error CapBelowInitialSupply(uint256 initialSupply, uint256 cap);

    /// @notice Thrown when the cap is zero, which would make the token mintable-never.
    error ZeroMaxSupply();

    modifier onlyOperator() {
        if (!isOperator[msg.sender]) {
            revert NotOperator(msg.sender);
        }
        _;
    }

    /**
     * @param name_             ERC-20 name, e.g. "My Token".
     * @param symbol_           ERC-20 symbol, e.g. "MTK".
     * @param initialSupply_    Minted to `initialOwner_` at deployment. May be 0.
     * @param maxSupply_        Permanent ceiling on totalSupply(). Must be >= initialSupply_ and > 0.
     * @param initialOwner_     Receives ownership: manages operators, can transferOwnership.
     * @param initialOperator_  Receives mint rights. Pass address(0) for none; the owner
     *                          can grant it later with setOperator().
     */
    constructor(
        string memory name_,
        string memory symbol_,
        uint256 initialSupply_,
        uint256 maxSupply_,
        address initialOwner_,
        address initialOperator_
    ) ERC20(name_, symbol_) Ownable(initialOwner_) {
        if (maxSupply_ == 0) {
            revert ZeroMaxSupply();
        }
        if (initialSupply_ > maxSupply_) {
            revert CapBelowInitialSupply(initialSupply_, maxSupply_);
        }

        maxSupply = maxSupply_;

        if (initialOperator_ != address(0)) {
            isOperator[initialOperator_] = true;
            emit OperatorUpdated(initialOperator_, true);
        }

        if (initialSupply_ != 0) {
            _mint(initialOwner_, initialSupply_);
        }
    }

    /**
     * @notice Create `amount` new tokens for `to`, up to the cap.
     * @dev Operator only. Reverts rather than silently minting a partial amount.
     */
    function mint(address to, uint256 amount) external onlyOperator {
        uint256 newTotal = totalSupply() + amount;
        if (newTotal > maxSupply) {
            revert MaxSupplyExceeded(newTotal, maxSupply);
        }
        _mint(to, amount);
    }

    /**
     * @notice Grant or revoke mint rights.
     * @dev Owner only. Passing address(0) is rejected: nobody can sign for it,
     *      so granting it would silently destroy mint rights rather than
     *      delegate them.
     */
    function setOperator(address operator, bool allowed) external onlyOwner {
        if (operator == address(0)) {
            revert ZeroAddress();
        }
        isOperator[operator] = allowed;
        emit OperatorUpdated(operator, allowed);
    }

    /// @notice How many more tokens the operators may collectively mint.
    function remainingMintable() external view returns (uint256) {
        return maxSupply - totalSupply();
    }
}
