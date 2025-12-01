"""
Utility functions and middleware for w3ext.

This module provides essential utility functions for blockchain operations including:
- Transaction parameter filling (nonce, gas price, chain ID)
- ABI loading from files or URLs
- EIP-1559 detection and gas price management
- Automatic transaction signing middleware
- Cryptographic utilities (keccak256 hashing)
- Helper classes for attribute access

The utilities are designed to work seamlessly with both AsyncWeb3 and Chain instances,
providing a consistent interface for blockchain operations.

Example:
    >>> # Load ABI from file or URL
    >>> abi = await load_abi('path/to/contract.json')
    >>> abi = await load_abi('https://api.etherscan.io/api?module=contract&action=getabi...')
    >>>
    >>> # Fill transaction parameters automatically
    >>> tx = {'to': '0x123...', 'value': 1000}
    >>> tx = await fill_nonce(w3, tx)
    >>> tx = await fill_gas_price(w3, tx)
    >>> tx = await fill_chain_id(w3, tx)
    >>>
    >>> # Hash data using keccak256
    >>> hash_result = keccak256('Hello, World!')
    >>> hash_result = keccak256(b'binary data')
    >>> hash_result = keccak256('0x1234abcd')
"""

# pylint: disable=no-name-in-module
import json
from cytoolz.dicttoolz import assoc
from typing import Any, Callable, Collection, Union, cast, TYPE_CHECKING, Optional, Dict

import aiohttp
from eth_account.signers.local import LocalAccount
from eth_keys.datatypes import PrivateKey
from eth_typing import ChecksumAddress, HexStr
from eth_utils.toolz import curry
from eth_utils.crypto import keccak
from web3 import AsyncWeb3, Web3
from web3.middleware import Web3Middleware
from web3.middleware.signing import format_transaction, gen_normalized_accounts
from web3.types import RPCEndpoint, RPCResponse, TxParams
try:
    from web3._utils.async_transactions import async_fill_transaction_defaults as fill_transaction_defaults
except ImportError:
    from web3._utils.async_transactions import fill_transaction_defaults as fill_transaction_defaults

if TYPE_CHECKING:
    from .chain import Chain


_PrivateKey = Union[LocalAccount, PrivateKey, HexStr, bytes]

to_checksum_address = AsyncWeb3.to_checksum_address


async def load_abi(filename: str, process: Optional[Callable] = None) -> str:
    """
    Load contract ABI from file or URL.

    Supports loading ABI definitions from local JSON files or remote URLs.
    Optionally applies a processing function to transform the loaded ABI.

    Args:
        filename: Path to local file or HTTP(S) URL
        process: Optional function to process the loaded ABI

    Returns:
        Loaded and optionally processed ABI

    Example:
        >>> # Load from local file
        >>> abi = await load_abi('contracts/ERC20.json')
        >>>
        >>> # Load from Etherscan API
        >>> abi = await load_abi(
        ...     'https://api.etherscan.io/api?module=contract&action=getabi&address=0x123...'
        ... )
        >>>
        >>> # Load with processing
        >>> abi = await load_abi('contract.json', lambda x: x['abi'])
    """
    if (filename.startswith('http')):
        async with aiohttp.ClientSession() as session:
            async with session.get(filename) as resp:
                abi = await resp.json()
    else:
        with open(filename) as f:
            abi = json.load(f)

    if process is not None:
        abi = process(abi)

    return abi


async def is_eip1559(w3: 'AsyncWeb3'):
    """
    Check if the network supports EIP-1559 (London hard fork).

    Determines if the network supports EIP-1559 by checking if the latest
    block has a non-zero base fee per gas.

    Args:
        w3: AsyncWeb3 instance

    Returns:
        True if EIP-1559 is supported, False otherwise

    Example:
        >>> if await is_eip1559(w3):
        ...     # Use maxFeePerGas and maxPriorityFeePerGas
        ...     tx['maxFeePerGas'] = 20000000000
        ... else:
        ...     # Use legacy gasPrice
        ...     tx['gasPrice'] = 20000000000
    """
    fee = await w3.eth.fee_history(1, 'latest')
    return fee['baseFeePerGas'][0] != 0


async def get_gas_price(w3: 'Chain') -> int:
    """
    Get the current gas price for transactions.

    For EIP-1559 networks, calculates the effective gas price by summing the
    base fee and priority fee. For legacy networks, returns the current gas
    price directly.

    Args:
        w3: Chain or AsyncWeb3 instance

    Returns:
        Gas price in wei

    Example:
        >>> gas_price = await get_gas_price(chain)
        >>> print(f"Gas price: {gas_price} wei")
    """
    _eip1559 = await (
        w3.is_eip1559() if hasattr(w3, 'is_eip1559') else is_eip1559(w3)
    )
    if _eip1559:
        base_fee = (await w3.eth.get_block('latest'))['baseFeePerGas']
        priority_fee = await w3.eth.max_priority_fee
        return base_fee + priority_fee
    else:
        return await w3.eth.gas_price


async def fill_gas_price(w3: Union['AsyncWeb3', 'Chain'], transaction: TxParams) -> TxParams:
    """
    Fill gas price in a transaction if not already set.

    For EIP-1559 networks, it sets 'maxFeePerGas' and 'maxPriorityFeePerGas' if they
    are not provided. For legacy networks, it sets 'gasPrice'.

    Args:
        w3: An AsyncWeb3 or Chain instance.
        transaction: The transaction dictionary.

    Returns:
        The transaction dictionary with gas price parameters filled.
    """
    _eip1559 = await (w3.is_eip1559() if hasattr(w3, 'is_eip1559') else is_eip1559(w3))
    if _eip1559:
        if 'maxFeePerGas' not in transaction or 'maxPriorityFeePerGas' not in transaction:
            base_fee = (await w3.eth.get_block('latest'))['baseFeePerGas']
            priority_fee = await w3.eth.max_priority_fee
            transaction['maxPriorityFeePerGas'] = priority_fee
            transaction['maxFeePerGas'] = int(base_fee * 1.2) + priority_fee
    elif 'gasPrice' not in transaction:
        transaction['gasPrice'] = await w3.eth.gas_price

    return transaction


async def fill_chain_id(w3: Union['AsyncWeb3', 'Chain'], transaction: TxParams) -> TxParams:
    """
    Fill chain ID in transaction if not already set.

    Automatically sets the chainId parameter based on the connected network.
    Ensures the chain ID is properly formatted as a hex string.

    Args:
        w3: AsyncWeb3 or Chain instance
        transaction: Transaction parameters to fill

    Returns:
        Transaction with chain ID filled

    Example:
        >>> tx = {'to': '0x123...', 'value': 1000}
        >>> tx = await fill_chain_id(w3, tx)
        >>> # tx now has 'chainId' set to current network's chain ID
    """
    if transaction.get("chainId") is None:
        if isinstance(w3, AsyncWeb3):
            transaction['chainId'] = hex(int(await w3.eth.chain_id))
        elif (chain_id := getattr(w3, 'chain_id', None)) is not None:
            transaction['chainId'] = hex(int(chain_id))
    if ((chain_id := transaction.get('chainId')) is not None
            and not str(chain_id).startswith('0x')):
        transaction['chainId'] = hex(int(chain_id))
    return transaction


@curry
async def fill_nonce(w3: Union['AsyncWeb3', 'Chain'], transaction: TxParams) -> TxParams:
    """
    Fill nonce in transaction if not already set.

    Automatically sets the nonce based on the current transaction count
    for the 'from' address. This is curried to allow partial application.

    Args:
        w3: AsyncWeb3 or Chain instance
        transaction: Transaction parameters to fill

    Returns:
        Transaction with nonce filled if 'from' address is present

    Example:
        >>> tx = {'from': account.address, 'to': '0x123...', 'value': 1000}
        >>> tx = await fill_nonce(w3, tx)
        >>> # tx now has 'nonce' set to next available nonce
        >>>
        >>> # Can also be used as a curried function
        >>> fill_nonce_for_w3 = fill_nonce(w3)
        >>> tx = await fill_nonce_for_w3(tx)
    """
    if 'from' in transaction and 'nonce' not in transaction:
        return assoc(
            transaction,
            'nonce',
            await w3.eth.get_transaction_count(  # type: ignore
                cast(ChecksumAddress, transaction['from'])
            ),
        )
    return transaction


class AsyncSignSendRawMiddleware(Web3Middleware):
    """
    Middleware for automatic transaction signing and sending.

    This middleware intercepts eth_sendTransaction calls and automatically
    signs them using provided accounts, then sends them as raw transactions.
    This enables seamless transaction sending without manual signing steps.

    Attributes:
        _accounts: Dictionary mapping addresses to LocalAccount instances

    Example:
        >>> accounts = {account.address: account}
        >>> middleware = AsyncSignSendRawMiddleware(w3, accounts)
        >>> w3.middleware_onion.add(middleware)
        >>> # Now eth_sendTransaction calls will be auto-signed
    """

    def __init__(
        self,
        w3: AsyncWeb3,
        accounts: Union[Dict[ChecksumAddress, LocalAccount], Callable[[], Dict[ChecksumAddress, LocalAccount]]],
    ) -> None:
        """
        Initialize the signing middleware.

        Args:
            w3: AsyncWeb3 instance
            accounts: Either:
                      - a dict mapping addresses to LocalAccount instances, or
                      - a callable returning such dict (evaluated per request)
        """
        super().__init__(w3)
        self._accounts: Dict[ChecksumAddress, LocalAccount] = {}
        self._accounts_fn: Optional[Callable[[], Dict[ChecksumAddress, LocalAccount]]] = None
        if callable(accounts):
            # when callable passed, we will call it on each request to fetch active accounts
            self._accounts_fn = accounts
        else:
            self._accounts = accounts

    async def async_wrap_make_request(self, make_request):
        """
        Wrap the request handler to intercept and sign transactions.

        Intercepts eth_sendTransaction calls, fills transaction parameters,
        signs with the appropriate account, and sends as raw transaction.

        Args:
            make_request: Original request handler

        Returns:
            Wrapped request handler
        """
        async def middleware(method: RPCEndpoint, params: Any) -> RPCResponse:
            if method != 'eth_sendTransaction':
                return await make_request(method, params)

            transaction = params[0]
            transaction = await fill_chain_id(self._w3, transaction)
            transaction = await fill_nonce(self._w3, transaction)
            transaction = await fill_transaction_defaults(self._w3, transaction)
            transaction = await fill_gas_price(self._w3, transaction)
            transaction = format_transaction(transaction)

            if 'from' not in transaction:
                return await make_request(method, params)

            accounts = self._accounts_fn() if getattr(self, "_accounts_fn", None) else self._accounts
            sender = transaction.get('from')
            if sender not in accounts:
                return await make_request(method, params)

            # pylint: disable=unsubscriptable-object
            account = accounts[sender]
            raw_tx = account.sign_transaction(transaction).raw_transaction

            return await make_request(RPCEndpoint('eth_sendRawTransaction'),
                                      [AsyncWeb3.to_hex(raw_tx)])

        return middleware


def construct_async_sign_and_send_raw_middleware(
    private_key_or_account: Union[_PrivateKey, Collection[_PrivateKey]]
) -> Callable[[AsyncWeb3], AsyncSignSendRawMiddleware]:
    """
    Create middleware for automatic transaction signing.

    Constructs middleware that automatically signs and sends transactions
    using the provided private keys or accounts. Supports multiple account
    formats and collections of accounts.

    Args:
        private_key_or_account: Single private key/account or collection of them.
            Supported formats:
            - eth_account.LocalAccount object
            - eth_keys.PrivateKey object
            - Raw private key as hex string or bytes

    Returns:
        Middleware constructor function

    Example:
        >>> # Single account
        >>> middleware = construct_async_sign_and_send_raw_middleware(private_key)
        >>> w3.middleware_onion.add(middleware)
        >>>
        >>> # Multiple accounts
        >>> middleware = construct_async_sign_and_send_raw_middleware([key1, key2, key3])
        >>> w3.middleware_onion.add(middleware)
    """
    accounts = gen_normalized_accounts(private_key_or_account)
    def middleware(w3: AsyncWeb3 | Web3):
        return AsyncSignSendRawMiddleware(w3, accounts)
    return middleware


def keccak256(value: Union[str, bytes]) -> str:
    """
    Compute Keccak-256 hash of input value.

    Supports multiple input formats: bytes, hex strings, and text strings.
    Always returns a hex string with '0x' prefix.

    Args:
        value: Input to hash - bytes, hex string, or text string

    Returns:
        Keccak-256 hash as hex string with '0x' prefix

    Example:
        >>> # Hash text
        >>> hash1 = keccak256('Hello, World!')
        >>>
        >>> # Hash bytes
        >>> hash2 = keccak256(b'binary data')
        >>>
        >>> # Hash hex string
        >>> hash3 = keccak256('0x1234abcd')
        >>>
        >>> # All return format: '0x...'
    """
    if isinstance(value, bytes):
        hashed = keccak(value)
    elif value.startswith('0x'):
        hashed = keccak(hexstr=value)
    else:
        hashed = keccak(text=value)

    hex = hashed.hex()
    # Add the '0x' prefix if not present
    if not hex.startswith('0x'):
        hex = '0x' + hex
    return hex


solidity_keccak = Web3.solidity_keccak

class AttrDict(dict):
    """
    Dictionary with attribute-style access.

    Allows accessing dictionary keys as attributes for more convenient
    syntax when working with structured data.

    Example:
        >>> data = AttrDict({'name': 'Alice', 'age': 30})
        >>> print(data.name)  # 'Alice'
        >>> print(data['age'])  # 30
        >>> data.city = 'New York'  # Sets data['city']
    """

    def __getattr__(self, name):
        """
        Get dictionary value as attribute.

        Args:
            name: Attribute name (dictionary key)

        Returns:
            Dictionary value for the key

        Raises:
            AttributeError: If key doesn't exist
        """
        if name in self:
            return self[name]
        super().__getattribute__(name)