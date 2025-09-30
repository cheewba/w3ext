"""
Account management module for w3ext.

This module provides account management functionality for Ethereum blockchain interactions,
including private key handling, transaction signing, message signing, and chain binding.
It wraps web3.py's Account functionality with additional convenience methods and type safety.

Classes:
    Account: Wrapper around Web3 Account for private key management and signing
    ChainAccount: Account bound to a specific Chain instance for blockchain operations
    SignedMessage: Named tuple containing signature components

Example:
    >>> # Create account from private key
    >>> account = Account.from_key("0x1234...")
    >>>
    >>> # Sign a message
    >>> signature = await account.sign("Hello, world!")
    >>>
    >>> # Use with a chain
    >>> chain_account = account.use_chain(chain)
    >>> balance = await chain_account.get_balance()
"""

# pylint: disable=no-name-in-module
import json
from contextlib import contextmanager, ExitStack
from typing import TypeVar, Any, TYPE_CHECKING, Union, List, Iterator, Optional, NamedTuple, Mapping

from eth_account.signers.local import LocalAccount
from eth_account import Account as Web3Account
from eth_account.messages import encode_defunct, SignableMessage
from eth_typing import ChecksumAddress
from web3.types import HexBytes
try:
    from eth_account.messages import encode_typed_data
except ImportError:
    from eth_account.messages import encode_structured_data
    def encode_typed_data(*args, full_message: dict, **kwargs) -> SignableMessage:
        return encode_structured_data(full_message)

from .token import Token
if TYPE_CHECKING:
    from .chain import Chain
    from .token import CurrencyAmount

__all__ = ["Account", ]

Self = TypeVar("Self")

class SignedMessage(NamedTuple):
    """
    Container for message signature components.

    Attributes:
        messageHash: Hash of the signed message
        r: First component of ECDSA signature
        s: Second component of ECDSA signature
        v: Recovery parameter
        signature: Complete signature as bytes
    """
    messageHash: HexBytes
    r: int
    s: int
    v: int
    signature: HexBytes


class Account:
    """
    Wrapper around Web3 Account for private key management and signing.

    This class provides a high-level interface for Ethereum account operations
    including message signing, transaction signing, and chain binding. It's not
    bound to any specific Chain instance, making it reusable across different networks.

    Example:
        >>> # Create from private key
        >>> account = Account.from_key("0x1234567890abcdef...")
        >>> print(account.address)  # 0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0
        >>>
        >>> # Sign a message
        >>> signature = await account.sign("Hello, world!")
        >>>
        >>> # Use with a chain
        >>> chain_account = account.use_chain(ethereum_chain)
    """
    # Checksum address of the account (set in from_key)
    address: ChecksumAddress

    def __init__(self) -> None:
        """Initialize an empty Account instance."""
        # Internal LocalAccount instance from eth_account
        self._acc: LocalAccount = Web3Account()

    @classmethod
    def from_key(cls, key: str) -> 'Account':
        """
        Create an Account from a private key.

        Args:
            key: Private key as hex string (with or without '0x' prefix)

        Returns:
            Account instance initialized with the private key

        Example:
            >>> account = Account.from_key("1234567890abcdef...")
            >>> # or with 0x prefix
            >>> account = Account.from_key("0x1234567890abcdef...")
        """
        instance = cls()
        key = key if key.startswith('0x') else f"0x{key}"
        instance._acc = Web3Account.from_key(key)
        return instance

    def use_chain(self, chain: "Chain") -> "ChainAccount":
        """
        Bind this account to a specific chain for blockchain operations.

        Args:
            chain: Chain instance to bind to

        Returns:
            ChainAccount instance with access to blockchain operations

        Example:
            >>> account = Account.from_key("0x1234...")
            >>> chain_account = account.use_chain(ethereum_chain)
            >>> balance = await chain_account.get_balance()
        """
        return ChainAccount(self, chain)

    @contextmanager
    def onchain(self, *chains: "Chain") -> Iterator[Union["ChainAccount", List["ChainAccount"]]]:
        """
        Context manager to temporarily add account signing to chains.

        Adds this account to each chain's async-context account set using Chain.use_account.
        The chain's signing middleware reads the active accounts at request time.
        """
        with ExitStack() as stack:
            for chain in chains:
                stack.enter_context(chain.use_account(self))
            bound = [self.use_chain(chain) for chain in chains]
            yield bound[0] if len(bound) == 1 else bound

    async def sign(self, data: Union[bytes, str, Mapping], hex_only=True) -> Union[SignedMessage, HexBytes]:
        """
        Sign arbitrary data with this account's private key.

        Supports multiple data formats:
        - EIP-712 typed data (as dict or JSON string)
        - Raw bytes
        - Hex strings (with 0x prefix)
        - Plain text strings

        Args:
            data: Data to sign (bytes, string, or EIP-712 dict)
            hex_only: If True, return only signature bytes; if False, return full SignedMessage

        Returns:
            Signature bytes (if hex_only=True) or SignedMessage with all components

        Example:
            >>> account = Account.from_key("0x1234...")
            >>>
            >>> # Sign plain text
            >>> sig = await account.sign("Hello, world!")
            >>>
            >>> # Sign EIP-712 typed data
            >>> typed_data = {
            ...     "types": {...},
            ...     "primaryType": "Mail",
            ...     "domain": {...},
            ...     "message": {...}
            ... }
            >>> sig = await account.sign(typed_data)
            >>>
            >>> # Get full signature components
            >>> full_sig = await account.sign("Hello", hex_only=False)
            >>> print(full_sig.r, full_sig.s, full_sig.v)
        """
        is_eip712 = isinstance(data, Mapping)
        if not is_eip712:
            try:
                decoded = json.loads(data)
                if all(map(lambda key: key in decoded),
                       ['types', 'primaryType', 'domain', 'message']):
                    is_eip712 = True
                    data = decoded
            except json.JSONDecodeError:
                pass

        if is_eip712:
            encoded = encode_typed_data(full_message=data)
        elif (isinstance(data, bytes)):
            encoded = encode_defunct(bytes=data)
        elif data.startswith('0x'):
            encoded = encode_defunct(hexstr=data)
        else:
            # by default encode it as a simple text
            encoded = encode_defunct(text=data)
        signed = self._acc.sign_message(encoded)

        return signed.signature if hex_only else signed

    def __getattr__(self, name) -> Any:
        """
        Delegate attribute access to the underlying LocalAccount.

        This allows direct access to all eth_account functionality like
        signing transactions, accessing the private key, etc.

        Example:
            >>> account = Account.from_key("0x1234...")
            >>> # Access underlying account properties
            >>> private_key = account.key
            >>> public_key = account.public_key
        """
        return getattr(self._acc, name)

    def __str__(self) -> str:
        """Return the account's address as string representation."""
        return self.address


class ChainAccount:
    """
    Account bound to a specific Chain instance for blockchain operations.

    This class combines an Account with a Chain to provide convenient access
    to blockchain operations like getting balances, sending transactions, etc.
    It delegates most functionality to the underlying Account while adding
    chain-specific operations.

    Example:
        >>> account = Account.from_key("0x1234...")
        >>> chain_account = account.use_chain(ethereum_chain)
        >>>
        >>> # Get ETH balance
        >>> eth_balance = await chain_account.get_balance()
        >>>
        >>> # Get token balance
        >>> usdc = await ethereum_chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
        >>> usdc_balance = await chain_account.get_balance(usdc)
    """

    def __init__(self, account: "Account", chain: "Chain") -> None:
        """
        Initialize ChainAccount with an account and chain.

        Args:
            account: Account instance to bind
            chain: Chain instance to bind to
        """
        # The underlying Account instance
        self._account: "Account" = account
        # The Chain instance this account is bound to
        self._chain: "Chain" = chain

    def chain(self) -> "Chain":
        """
        Get the chain this account is bound to.

        Returns:
            Chain instance
        """
        return self._chain

    async def get_balance(self, token: Optional['Token'] = None) -> 'CurrencyAmount':
        """
        Get balance for this account.

        Args:
            token: Optional Token to get balance for. If None, gets native currency balance.

        Returns:
            CurrencyAmount representing the balance

        Example:
            >>> chain_account = account.use_chain(ethereum_chain)
            >>>
            >>> # Get ETH balance
            >>> eth_balance = await chain_account.get_balance()
            >>> print(f"ETH: {eth_balance.to_fixed(4)}")
            >>>
            >>> # Get USDC balance
            >>> usdc = await ethereum_chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
            >>> usdc_balance = await chain_account.get_balance(usdc)
            >>> print(f"USDC: {usdc_balance.to_fixed(2)}")
        """
        return await (
            token.get_balance(self.address) if isinstance(token, Token)
            else self._chain.get_balance(self.address)
        )

    def __getattr__(self, name) -> Any:
        """
        Delegate attribute access to the underlying Account.

        This allows access to all Account functionality like signing,
        address, private key, etc.
        """
        return getattr(self._account, name)

    def __str__(self) -> str:
        """Return string representation showing chain and account."""
        return f"{self._chain}({self._account})"
