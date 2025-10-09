# pylint: disable=no-name-in-module
"""
Chain module for w3ext library.

This module provides the Chain class, which is the central component for interacting
with Ethereum-compatible blockchains. It extends web3.py functionality with enhanced
features like batch operations, automatic token/NFT loading, and simplified account management.
"""

import asyncio
import os
from contextlib import ExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Optional, Any, Union, cast, Type, Dict

from eth_typing import HexAddress, ChecksumAddress
from eth_account.signers.local import LocalAccount
from web3 import AsyncWeb3 as _AsyncWeb3, AsyncHTTPProvider
from web3.eth import AsyncEth
from web3.providers import AsyncBaseProvider
from web3.middleware import (
    AttributeDictMiddleware,
    BufferedGasEstimateMiddleware,
    GasPriceStrategyMiddleware,
    ValidationMiddleware,
    ExtraDataToPOAMiddleware
)
from web3.types import HexBytes, TxParams, HexStr, TxReceipt, StateOverride, BlockIdentifier

from .chainlist import get_chain_provider
from ..contract import Contract
from ..exceptions import ChainException
from ..token import Currency, Token, CurrencyAmount
from ..nft import Nft721Collection
from ..utils import is_eip1559, load_abi, to_checksum_address, AsyncSignSendRawMiddleware
from ..batch import Batch, is_batch_method, to_batch_aware_method
from ..account import Account


ABI_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'abi')
_batcher_ctx_var = ContextVar("_batcher_ctx_var", default={})
_accounts_ctx_var = ContextVar("_accounts_ctx_var", default={})


async def a_dummy(value):
    return value


class AsyncEthProxy:
    def __init__(self, eth: AsyncEth, chain: "Chain") -> None:
        self._eth = eth
        self._chain = chain

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._eth, name)
        if is_batch_method(self._eth, name):
            return to_batch_aware_method(self._chain, value)

        # If the attribute is callable, we want to rebind it so that within its body,
        # self will resolve via our proxy.
        if callable(value):
            # If the attribute is a classmethod, then it will be a 'classmethod' descriptor.
            # We detect that and use its __get__ to bind it properly.
            if isinstance(value, classmethod):
                return value.__get__(self, type(self))

            # For normal methods, check if it's a bound method (i.e. has __func__)
            if hasattr(value, '__func__'):
                # Return a wrapper that calls the underlying unbound function with self replaced by the proxy.
                @wraps(value)
                def wrapper(*args, **kwargs):
                    return value.__func__(self, *args, **kwargs)
                return wrapper

        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in ('_eth', '_chain'):
            setattr(self._eth, name, value)
        super().__setattr__(name, value)


def patch_provider(provider_instance, chain):
    """
    Dynamically creates a subclass of the provider's class that adds a property
    _is_batching, then changes the instance's __class__ to that subclass.
    """
    # Save the original class
    orig_cls = provider_instance.__class__

    # Define a new subclass dynamically.
    class PatchedProvider(orig_cls):
        @property
        def _is_batching(self):
            # Custom getter: return whether _batcher_ctx_var indicates batching is active.
            return chain._is_batching

        @_is_batching.setter
        def _is_batching(self, value):
            # don't modify batching var
            pass

    # Change the instance's class to the new patched subclass.
    provider_instance.__class__ = PatchedProvider
    return provider_instance


class AsyncWeb3(_AsyncWeb3):
    def __init__(self, chain: "Chain", *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._chain = chain

    def __getattribute__(self, name: str) -> Any:
        value = super().__getattribute__(name)
        if name == 'provider' and value.__class__.__name__ != "PatchedProvider":
                value = patch_provider(value, self._chain)
        return value


class Chain:
    """
    Central component for blockchain interaction in w3ext.

    The Chain class provides a high-level interface for interacting with Ethereum-compatible
    blockchains. It extends web3.py functionality with enhanced features like batch operations,
    automatic token/NFT loading, and simplified account management.

    Features:
    - Automatic RPC connection management
    - Built-in support for ERC20 tokens and ERC721 NFTs
    - Batch operation support for improved performance
    - EIP-1559 transaction support detection
    - Integrated block explorer URL generation
    - ABI caching for common contract types

    Attributes:
        currency (Currency): The native currency of the chain (e.g., ETH, BNB)
        scan (str, optional): Base URL for block explorer
        name (str, optional): Human-readable name of the chain
        chain_id (str): The chain ID as a string
    """

    _DEFAULT_MIDDLEWARE = [
            GasPriceStrategyMiddleware,
            AttributeDictMiddleware,
            ValidationMiddleware,
            BufferedGasEstimateMiddleware,
            ExtraDataToPOAMiddleware,
    ]

    def __init__(
        self,
        chain_id: Union[str, int],
        currency: Union[str, 'Currency'] = 'ETH',
        scan: Optional[str] = None,
        name: Optional[str] = None,
        *,
        request_kwargs: Optional[dict] = None
    ) -> None:
        """
        Initialize a Chain instance.

        Args:
            chain_id: The blockchain's chain ID (e.g., 1 for Ethereum mainnet)
            currency: The native currency symbol or Currency object (default: 'ETH')
            scan: Base URL for block explorer (e.g., 'https://etherscan.io')
            name: Human-readable name for the chain (e.g., 'Ethereum Mainnet')

        Note:
            This constructor creates an unconnected Chain instance. Use Chain.connect()
            class method to create a connected instance, or call connect_rpc() afterwards.
        """
        # Internal AsyncWeb3 instance with custom middleware
        # Initialize with a dummy provider to prevent AutoProvider from probing IPC/localhost
        self.__web3: AsyncWeb3 = AsyncWeb3(
            self,
            middleware=self._DEFAULT_MIDDLEWARE,
            provider=get_chain_provider(chain_id, request_kwargs)
        )
        self.__web3.eth = AsyncEthProxy(self.__web3.eth, self)
        # Chain ID stored as string for consistency
        self._chain_id: str = str(chain_id)
        # Cached EIP-1559 support detection result
        self._is_eip1559: Optional[bool] = None

        self.currency = currency
        self.scan = scan
        self.name = name

        # Cache for loaded ABI files to avoid repeated disk reads
        self._abi_cache: Dict[str, Any] = {}

        # install signing middleware that reads active accounts from chain context
        if not self.__web3.middleware_onion.get('w3ext-signing'):
            def _w3ext_signing_factory(w3, _self=self):
                # Middleware pulls accounts from the current async context
                return AsyncSignSendRawMiddleware(w3, lambda: _self._get_active_accounts())
            self.__web3.middleware_onion.add(_w3ext_signing_factory, 'w3ext-signing')

    @classmethod
    async def connect(
        cls: Type["Chain"],
        rpc: str,
        chain_id: Union[str, int],
        *,
        currency: Union[str, 'Currency'] = 'ETH',
        scan: Optional[str] = None,
        name: Optional[str] = None,
        request_kwargs: Optional[dict] = None
    ) -> "Chain":
        """
        Create and connect a Chain instance to an RPC endpoint.

        This is the recommended way to create a Chain instance as it automatically
        establishes the RPC connection and verifies the chain ID.

        Args:
            rpc: RPC endpoint URL (e.g., 'https://mainnet.infura.io/v3/PROJECT_ID')
            chain_id: Expected chain ID for verification
            currency: Native currency symbol or Currency object (default: 'ETH')
            scan: Block explorer base URL (optional)
            name: Human-readable chain name (optional)
            request_kwargs: Additional HTTP request parameters (optional)

        Returns:
            Connected Chain instance ready for use

        Raises:
            ChainException: If the actual chain ID doesn't match the expected one

        Example:
            >>> chain = await Chain.connect(
            ...     rpc="https://mainnet.infura.io/v3/YOUR_PROJECT_ID",
            ...     chain_id=1,
            ...     name="Ethereum Mainnet",
            ...     scan="https://etherscan.io"
            ... )
        """
        instance = cls(chain_id, currency, scan, name)
        await instance.connect_rpc(rpc, request_kwargs)
        return instance

    @property
    def _is_batching(self):
        return self.batcher is not None

    @property
    def batcher(self):
        store = _batcher_ctx_var.get()
        return store.get(id(self)) if store else None

    # Returns active signer accounts for this chain from the async context
    def _get_active_accounts(self) -> Dict[ChecksumAddress, LocalAccount]:
        store = _accounts_ctx_var.get()
        return store.get(id(self), {}) if store else {}

    @contextmanager
    def use_account(self, account: "Account"):
        """
        Temporarily add an Account into the active signer set for this chain within the current async context.
        The signing middleware will pick it up for eth_sendTransaction and sign accordingly.
        """
        store = _accounts_ctx_var.get() or {}
        token = None
        try:
            # copy-on-write to avoid mutating parent context
            new_store = dict(store)
            per_chain = dict(new_store.get(id(self), {}))
            # Account holds LocalAccount internally; we use duck typing for sign_transaction
            local_acc = getattr(account, "_acc", None) or account
            per_chain[account.address] = local_acc  # type: ignore[assignment]
            new_store[id(self)] = per_chain
            token = _accounts_ctx_var.set(new_store)
            yield self
        finally:
            if token is not None:
                _accounts_ctx_var.reset(token)

    @asynccontextmanager
    async def use_batch(self, max_size: int = 20, max_wait: float = 0.1):
        """
        Context manager for batch operations to improve performance.

        Batches multiple blockchain calls together to reduce the number of RPC requests.
        This is particularly useful when making many contract calls or balance queries.

        Args:
            max_size: Maximum number of requests per batch (default: 20)
            max_wait: Maximum time to wait before sending a batch in seconds (default: 0.1)

        Yields:
            Batch: The batch manager instance

        Example:
            >>> async with chain.use_batch(max_size=10, max_wait=0.1):
            ...     balances = await asyncio.gather(
            ...         token1.get_balance(address1),
            ...         token2.get_balance(address2),
            ...         token3.get_balance(address3)
            ...     )
        """
        token = None
        try:
            async with Batch(self.__web3, max_size=max_size, max_wait=max_wait) as batcher:
                store = _batcher_ctx_var.get() or {}
                new_store = dict(store)
                new_store[id(self)] = batcher
                token = _batcher_ctx_var.set(new_store)
                yield batcher
        finally:
            if token:
                _batcher_ctx_var.reset(token)

    async def _add_to_batch_request_info(self, request_info):
        return await self.batcher._add_request_info(request_info)

    async def _verify_chain_id(self, chain_id: str):
        w3_chain_id = str(await self._web3.eth.chain_id)
        if chain_id != w3_chain_id:
            raise ChainException(f"{self.name}: Unexpected chain_id received "
                                 "({w3_chain_id} vs expected {chain_id})")

    async def connect_rpc(
        self,
        rpc: Union[str, AsyncBaseProvider],
        request_kwargs: Optional[dict] = None
    ) -> None:
        if isinstance(rpc, AsyncBaseProvider):
            provider = rpc
        else:
            # Ensure a default timeout of 30 seconds if not explicitly provided
            if request_kwargs is None:
                request_kwargs = {}
            request_kwargs.setdefault("timeout", 60)
            provider = AsyncHTTPProvider(rpc, request_kwargs)

        self.__web3.provider = provider
        await self._verify_chain_id(self.chain_id)

    @property
    def _web3(self) -> AsyncWeb3:
        return self.__web3

    @property
    def currency(self):
        return self._currency

    @currency.setter
    def currency(self, currency: Union['Currency', str]):
        self._currency = currency if isinstance(currency, Currency) else Currency(currency, currency)

    @property
    def chain_id(self):
        return self._chain_id

    async def _get_abi(self, name):
        if name not in self._abi_cache:
            self._abi_cache[name] = await self._load_abi(f'{name}.json')
        return self._abi_cache[name]

    async def _load_abi(self, name) -> Any:
        return await load_abi(os.path.join(ABI_PATH, name))

    async def erc20_abi(self):
        return await self._get_abi('erc20')

    async def erc721_abi(self):
        return await self._get_abi('erc721')

    async def is_eip1559(self) -> bool:
        if self._is_eip1559 is None:
            self._is_eip1559 = await is_eip1559(self._web3)
        return self._is_eip1559

    async def load_token(
        self,
        contract: HexAddress, *,
        cache_as: Optional[str] = None,
        abi: Optional[Any] = None,
        name: Optional[str] = None,
        symbol: Optional[str] = None,
        decimals: Optional[int] = None,
        **kwargs
    ) -> Optional['Token']:
        """
        Load an ERC20 token contract and create a Token instance.

        Automatically fetches token metadata (name, symbol, decimals) from the contract
        and creates a Token instance for easy interaction.

        Args:
            contract: Token contract address
            cache_as: Attribute name to cache the token on this Chain instance (optional)
            abi: Custom ABI to use instead of standard ERC20 ABI (optional)
            name: Token name override (e.g., "USD Coin") - if provided, skips RPC call
            symbol: Token symbol override (e.g., "USDC") - if provided, skips RPC call
            decimals: Token decimals override (e.g., 6) - if provided, skips RPC call
            **kwargs: Additional keyword arguments (for future extensibility)

        Returns:
            Token instance ready for use

        Note:
            If all three metadata fields (name, symbol, decimals) are provided,
            no RPC requests will be made to fetch token metadata from the contract.

        Example:
            >>> # Load USDC token with automatic metadata fetching
            >>> usdc = await chain.load_token(
            ...     "0xA0b86a33E6441b8e776f1b0b8c8e6e8b8e8e8e8e",
            ...     cache_as="usdc"
            ... )
            >>> # Load token with predefined metadata (no RPC calls)
            >>> custom_token = await chain.load_token(
            ...     "0x...",
            ...     name="Custom Token",
            ...     symbol="CTK",
            ...     decimals=18
            ... )
            >>> # Now accessible as chain.usdc
            >>> balance = await chain.usdc.get_balance(address)
        """
        token_contract = self.contract(contract, abi=abi or await self.erc20_abi())

        # Combine explicit parameters with kwargs for backward compatibility
        metadata = {'name': name, 'symbol': symbol, 'decimals': decimals}
        metadata.update(kwargs)

        tasks = [
            getattr(token_contract.functions, key)().call()
            if (val := metadata.get(key)) is None else a_dummy(val)
            for key in ['name', 'symbol', 'decimals']
        ]
        name, symbol, decimals = await asyncio.gather(*tasks)

        token = Token(token_contract, name, symbol, decimals)
        if cache_as is not None:
            setattr(self, cache_as, token)
        return token

    async def load_nft721(
        self,
        contract: HexAddress, *,
        cache_as: Optional[str] = None,
        abi: Optional[Any] = None
    ) -> Optional['Nft721Collection']:
        """
        Load an ERC721 NFT collection contract and create an Nft721Collection instance.

        Automatically fetches the collection name from the contract and creates
        an Nft721Collection instance for easy NFT interaction.

        Args:
            contract: NFT collection contract address
            cache_as: Attribute name to cache the collection on this Chain instance (optional)
            abi: Custom ABI to use instead of standard ERC721 ABI (optional)

        Returns:
            Nft721Collection instance ready for use

        Example:
            >>> # Load CryptoPunks collection
            >>> punks = await chain.load_nft721(
            ...     "0xb47e3cd837dDF8e4c57F05d70Ab865de6e193BBB",
            ...     cache_as="cryptopunks"
            ... )
            >>> # Get owned NFTs
            >>> owned = await chain.cryptopunks.get_owned_by(address)
        """
        token_contract = self.contract(contract, abi=abi or await self.erc721_abi())
        name = await token_contract.functions.name().call()
        collection = Nft721Collection(token_contract, name)
        if cache_as is not None:
            setattr(self, cache_as, collection)
        return collection

    async def get_balance(
        self,
        address: Union[HexAddress, "Account"],
        token: Optional[Token] = None
    ) -> 'CurrencyAmount':
        """
        Get the balance of native currency or a specific token for an address.

        Args:
            address: Wallet address or Account instance to check balance for
            token: Specific Token instance to check balance for (optional)
                  If None, returns native currency balance (e.g., ETH)

        Returns:
            CurrencyAmount representing the balance with proper decimal handling

        Example:
            >>> # Get ETH balance
            >>> eth_balance = await chain.get_balance("0x...")
            >>> print(f"ETH Balance: {eth_balance}")

            >>> # Get token balance
            >>> usdc_balance = await chain.get_balance("0x...", usdc_token)
            >>> print(f"USDC Balance: {usdc_balance}")
        """
        if isinstance(address, Account):
            address = address.address
        if token is not None and isinstance(token, Token):
            return await token.get_balance(address)

        address = to_checksum_address(str(address))
        amount = await self._web3.eth.get_balance(address)
        return CurrencyAmount(self.currency, amount)

    async def get_nonce(self, address: HexAddress) -> int:
        return await self.eth.get_transaction_count(
            cast(ChecksumAddress, address)
        )

    async def estimate_gas(
        self,
        transaction: TxParams,
        block_identifier: Optional[BlockIdentifier] = None,
        state_override: Optional[StateOverride] = None,
    ) -> int:
        return await self._web3.eth.estimate_gas(transaction, block_identifier, state_override)

    async def send_transaction(self, tx: TxParams, account: Optional["Account"] = None) -> HexBytes:
        with ExitStack() as stack:
            if account is not None:
                stack.enter_context(account.onchain(self))
                tx['from'] = account.address
                tx['chainId'] = hex(int(self.chain_id))
                if 'to' in tx:
                    tx['to'] = to_checksum_address(tx['to'])

            return await self._web3.eth.send_transaction(tx)

        # silent mypy error "missing return statement"
        assert False, "unreachable"

    async def send_raw_transaction(self, data: Union[HexStr, bytes]) -> HexBytes:
        return await self._web3.eth.send_raw_transaction(data)

    async def wait_for_transaction_receipt(self, tx_hash: HexBytes, timeout: float = 180) -> TxReceipt:
        return await self._web3.eth.wait_for_transaction_receipt(tx_hash, timeout)

    def contract(self, address: HexAddress, abi: Optional[Any] = None) -> 'Contract':
        address = to_checksum_address(address)
        contract = (
            self._web3.eth.contract(address, abi=abi)
            if abi is not None else address
        )
        return Contract(contract, self)

    def get_tx_scan(self, tx_hash: HexBytes):
        if not self.scan:
            return tx_hash
        hash_str = tx_hash.hex()
        if not hash_str.startswith('0x'):
            hash_str = f"0x{hash_str}"
        scan_base = self.scan[:-1] if self.scan.endswith('/') else self.scan
        return f"{scan_base}/tx/{hash_str}"

    def __getattr__(self, name) -> Any:
        if name == self.currency.symbol:
            return self.currency
        return getattr(self._web3, name)

    def __str__(self) -> str:
        return self.name or f"Chain#{self.chain_id}"