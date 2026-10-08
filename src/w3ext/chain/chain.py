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
from typing import Any, ClassVar, Optional, Union, cast

from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexAddress, HexStr
from hexbytes import HexBytes
from web3 import AsyncHTTPProvider
from web3 import AsyncWeb3 as _AsyncWeb3
from web3.eth import AsyncEth
from web3.exceptions import Web3ValidationError
from web3.manager import _AsyncPersistentMessageStream
from web3.middleware import (
    AttributeDictMiddleware,
    BufferedGasEstimateMiddleware,
    ExtraDataToPOAMiddleware,
    GasPriceStrategyMiddleware,
    ValidationMiddleware,
)
from web3.providers import AsyncBaseProvider
from web3.providers.persistent import PersistentConnection, PersistentConnectionProvider
from web3.providers.persistent.subscription_manager import SubscriptionManager
from web3.types import BlockIdentifier, StateOverride, TxParams, TxReceipt

from ..account import Account
from ..batch import Batch, is_batch_method, to_batch_aware_method
from ..contract import Contract
from ..exceptions import ChainException
from ..nft import Nft721Collection
from ..token import Currency, CurrencyAmount, Token
from ..utils import (
    AsyncSignSendRawMiddleware,
    get_gas_price,
    is_eip1559,
    load_abi,
    to_checksum_address,
)
from .middlewares import DynamicContextMiddleware, _middlewares_ctx_var
from .routers import RoutingProvider, RoutingRequestManager

ABI_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "abi")
_batcher_ctx_var: ContextVar[dict[int, Batch] | None] = ContextVar(
    "_batcher_ctx_var", default=None
)
_accounts_ctx_var: ContextVar[dict[int, dict[ChecksumAddress, LocalAccount]] | None] = (
    ContextVar("_accounts_ctx_var", default=None)
)
_active_chain_ctx_var: ContextVar["Chain | None"] = ContextVar(
    "_active_chain_ctx_var", default=None
)


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
            if hasattr(value, "__func__"):
                unbound = cast(Any, value).__func__

                # Return a wrapper that calls the underlying unbound function with self replaced by the proxy.
                @wraps(value)
                def wrapper(*args, **kwargs):
                    return unbound(self, *args, **kwargs)

                return wrapper

        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in ("_eth", "_chain"):
            setattr(self._eth, name, value)
        super().__setattr__(name, value)


def patch_provider(provider_instance):
    """Expose the current task's Chain state through a shared provider."""
    # Save the original class
    orig_cls = provider_instance.__class__

    # Define a new subclass dynamically.
    class PatchedProvider(orig_cls):
        @property
        def _is_batching(self):
            # Web3's listener runs in a different task from the batch caller.
            listener_task = getattr(self, "_message_listener_task", None)
            if listener_task is not None and asyncio.current_task() is listener_task:
                return getattr(self, "_w3ext_wire_batch_active", False)
            chain = _active_chain_ctx_var.get()
            return chain._is_batching if chain is not None else False

        @_is_batching.setter
        def _is_batching(self, value):
            # don't modify batching var
            pass

        @property
        def has_persistent_connection(self):
            chain = _active_chain_ctx_var.get()
            return orig_cls.has_persistent_connection and not (
                chain is not None
                and chain._routing_provider._processing_responses_directly.get()
            )

    # Change the instance's class to the new patched subclass.
    provider_instance.__class__ = PatchedProvider
    return provider_instance


class RoutedSubscriptionManager(SubscriptionManager):
    """Keep a retained subscription manager on its original RPC connection."""

    def __init__(self, w3, provider, routing_provider):
        self._routing_provider = routing_provider
        with routing_provider.use_provider(provider):
            super().__init__(w3)

    async def subscribe(self, subscriptions):
        with self._routing_provider.use_provider(self._provider):
            return await super().subscribe(subscriptions)

    async def unsubscribe(self, subscriptions):
        with self._routing_provider.use_provider(self._provider):
            return await super().unsubscribe(subscriptions)

    async def handle_subscriptions(self, run_forever: bool = False) -> None:
        with self._routing_provider.use_provider(self._provider):
            await super().handle_subscriptions(run_forever)


class RoutedPersistentMessageStream(_AsyncPersistentMessageStream):
    def __init__(self, manager, provider, routing_provider):
        self._bound_provider = provider
        self._routing_provider = routing_provider
        with routing_provider.use_provider(provider):
            super().__init__(manager)

    async def __anext__(self):
        with self._routing_provider.use_provider(self._bound_provider):
            return await super().__anext__()


class RoutedPersistentConnection(PersistentConnection):
    def __init__(self, w3, provider, routing_provider):
        self._routing_provider = routing_provider
        with routing_provider.use_provider(provider):
            super().__init__(w3)

    @property
    def subscriptions(self):
        with self._routing_provider.use_provider(self.provider):
            return super().subscriptions

    async def make_request(self, method, params):
        with self._routing_provider.use_provider(self.provider):
            return await super().make_request(method, params)

    async def send(self, method, params):
        with self._routing_provider.use_provider(self.provider):
            return await super().send(method, params)

    async def recv(self):
        with self._routing_provider.use_provider(self.provider):
            return await super().recv()

    def process_subscriptions(self):
        return RoutedPersistentMessageStream(
            self._manager, self.provider, self._routing_provider
        )


class AsyncWeb3(_AsyncWeb3):
    def __init__(self, chain: "Chain", *args, **kwargs) -> None:
        self._subscription_managers: dict[
            int, tuple[PersistentConnectionProvider, SubscriptionManager]
        ] = {}
        self._persistent_connections: dict[
            int, tuple[PersistentConnectionProvider, PersistentConnection]
        ] = {}
        super().__init__(*args, **kwargs)
        self._chain = chain
        self.manager.__class__ = RoutingRequestManager
        cast(
            RoutingRequestManager, self.manager
        )._routing_provider = chain._routing_provider

    def _selected_persistent_provider(self) -> PersistentConnectionProvider:
        provider = self.provider
        if not isinstance(provider, PersistentConnectionProvider):
            raise Web3ValidationError(
                "A persistent RPC provider is required for subscriptions"
            )
        return provider

    @property
    def subscription_manager(self) -> SubscriptionManager:
        provider = self._selected_persistent_provider()
        entry = self._subscription_managers.get(id(provider))
        if entry is None or entry[0] is not provider:
            entry = (
                provider,
                RoutedSubscriptionManager(
                    self, provider, self._chain._routing_provider
                ),
            )
            self._subscription_managers[id(provider)] = entry
        return entry[1]

    @property
    def socket(self) -> PersistentConnection:
        provider = self._selected_persistent_provider()
        entry = self._persistent_connections.get(id(provider))
        if entry is None or entry[0] is not provider:
            entry = (
                provider,
                RoutedPersistentConnection(
                    self, provider, self._chain._routing_provider
                ),
            )
            self._persistent_connections[id(provider)] = entry
        return entry[1]

    def __getattribute__(self, name: str) -> Any:
        value = super().__getattribute__(name)
        if name == "provider":
            _active_chain_ctx_var.set(self._chain)
            if value.__class__.__name__ != "PatchedProvider":
                value = patch_provider(value)
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

    _DEFAULT_MIDDLEWARE: ClassVar[list] = [
        GasPriceStrategyMiddleware,
        AttributeDictMiddleware,
        ValidationMiddleware,
        BufferedGasEstimateMiddleware,
        ExtraDataToPOAMiddleware,
    ]

    def __init__(
        self,
        chain_id: str | int,
        currency: Union[str, "Currency"] = "ETH",
        scan: str | None = None,
        name: str | None = None,
        *,
        request_kwargs: dict | None = None,
    ) -> None:
        """
        Initialize a Chain instance.

        Args:
            chain_id: The blockchain's chain ID (e.g., 1 for Ethereum mainnet)
            currency: The native currency symbol or Currency object (default: 'ETH')
            scan: Base URL for block explorer (e.g., 'https://etherscan.io')
            name: Human-readable name for the chain (e.g., 'Ethereum Mainnet')

        Note:
            This constructor creates a Chain without an explicit RPC. Requests use
            Chainlist unless a provider router is active. Use Chain.connect() or
            connect_rpc() to set an explicit RPC.
        """
        # Internal AsyncWeb3 instance with custom middleware
        # Keep one provider in Web3 so requests can select a route by context.
        self._routing_provider = RoutingProvider(chain_id, request_kwargs)
        self.__web3: AsyncWeb3 = AsyncWeb3(
            self,
            middleware=self._DEFAULT_MIDDLEWARE,
            provider=self._routing_provider,
        )
        cast(Any, self.__web3).eth = AsyncEthProxy(self.__web3.eth, self)
        # Chain ID stored as string for consistency
        self._chain_id: str = str(chain_id)
        # Cached EIP-1559 support detection result
        self._is_eip1559: bool | None = None

        self.currency = currency
        self.scan = scan
        self.name = name

        # Cache for loaded ABI files to avoid repeated disk reads
        self._abi_cache: dict[str, Any] = {}

        # install signing middleware that reads active accounts from chain context
        if not self.__web3.middleware_onion.get("w3ext-signing"):
            chain = self

            class ChainSigningMiddleware(AsyncSignSendRawMiddleware):
                def __init__(self, w3):
                    super().__init__(w3, chain._get_active_accounts)

            self.__web3.middleware_onion.add(ChainSigningMiddleware, "w3ext-signing")

        # install dynamic middleware proxy for context-specific middlewares
        if not self.__web3.middleware_onion.get("w3ext-dynamic-context"):
            chain = self

            class ChainDynamicContextMiddleware(DynamicContextMiddleware):
                def __init__(self, w3):
                    super().__init__(w3, chain)

            self.__web3.middleware_onion.add(
                ChainDynamicContextMiddleware, "w3ext-dynamic-context"
            )

    @classmethod
    async def connect(
        cls: type["Chain"],
        rpc: str,
        chain_id: str | int,
        *,
        currency: Union[str, "Currency"] = "ETH",
        scan: str | None = None,
        name: str | None = None,
        request_kwargs: dict | None = None,
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
        if self._routing_provider._executing_batch.get():
            return None
        store = _batcher_ctx_var.get()
        return store.get(id(self)) if store else None

    # Returns active signer accounts for this chain from the async context
    def _get_active_accounts(self) -> dict[ChecksumAddress, LocalAccount]:
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
        batcher = Batch(
            self.__web3,
            max_size=max_size,
            max_wait=max_wait,
            routing_provider=self._routing_provider,
        )
        store = _batcher_ctx_var.get() or {}
        new_store = dict(store)
        new_store[id(self)] = batcher
        async with batcher:
            token = _batcher_ctx_var.set(new_store)
            try:
                yield batcher
            finally:
                _batcher_ctx_var.reset(token)

    @asynccontextmanager
    async def use_middlewares(self, *middlewares: list):
        """
        Context manager for applying middlewares within the current async context.

        Args:
            middlewares: List of middleware factories. Each factory should match
                         the signature (make_request, w3) -> handler.

        Yields:
            Chain: The chain instance
        """
        store = _middlewares_ctx_var.get() or {}
        token = None
        try:
            # Copy to avoid side effects
            new_store = dict(store)

            # Get existing middlewares for this chain (from parent context)
            current_chain_middlewares = list(new_store.get(id(self), []))
            # Extend with new middlewares
            current_chain_middlewares.extend(middlewares)

            new_store[id(self)] = current_chain_middlewares
            token = _middlewares_ctx_var.set(new_store)
            yield self
        finally:
            if token:
                _middlewares_ctx_var.reset(token)

    async def _add_to_batch_request_info(self, request_info):
        batcher = self.batcher
        if batcher is None:
            raise RuntimeError("No active batch")
        return await batcher._add_request_info(
            request_info, self._routing_provider._require_provider()
        )

    async def _verify_chain_id(self, chain_id: str):
        w3_chain_id = str(await self._web3.eth.chain_id)
        if chain_id != w3_chain_id:
            raise ChainException(
                f"{self.name}: Unexpected chain_id received "
                f"({w3_chain_id} vs expected {chain_id})"
            )

    async def connect_rpc(
        self, rpc: str | AsyncBaseProvider, request_kwargs: dict | None = None
    ) -> None:
        if isinstance(rpc, AsyncBaseProvider):
            provider = rpc
        else:
            # Ensure a default timeout of 30 seconds if not explicitly provided
            if request_kwargs is None:
                request_kwargs = {}
            request_kwargs.setdefault("timeout", 60)
            provider = AsyncHTTPProvider(
                rpc,
                request_kwargs,
                cache_allowed_requests=True,
                cacheable_requests={"eth_chainId"},
                request_cache_validation_threshold=60 * 60,
            )

        with self._routing_provider.use_provider(provider):
            await self._verify_chain_id(self.chain_id)
        self._routing_provider.explicit_provider = provider

    async def close(self):
        """Close explicit, Chainlist, and previously routed RPC connections."""
        await self._routing_provider.disconnect()

    @property
    def _web3(self) -> AsyncWeb3:
        return self.__web3

    @property
    def currency(self):
        return self._currency

    @currency.setter
    def currency(self, currency: Union["Currency", str]):
        self._currency = (
            currency if isinstance(currency, Currency) else Currency(currency, currency)
        )

    @property
    def chain_id(self):
        return self._chain_id

    async def _get_abi(self, name):
        if name not in self._abi_cache:
            self._abi_cache[name] = await self._load_abi(f"{name}.json")
        return self._abi_cache[name]

    async def _load_abi(self, name) -> Any:
        return await load_abi(os.path.join(ABI_PATH, name))

    async def erc20_abi(self):
        return await self._get_abi("erc20")

    async def erc721_abi(self):
        return await self._get_abi("erc721")

    async def is_eip1559(self) -> bool:
        if self._is_eip1559 is None:
            self._is_eip1559 = await is_eip1559(self._web3)
        return self._is_eip1559

    async def load_token(
        self,
        contract: HexAddress,
        *,
        cache_as: str | None = None,
        abi: Any | None = None,
        name: str | None = None,
        symbol: str | None = None,
        decimals: int | None = None,
        **kwargs,
    ) -> Optional["Token"]:
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
        metadata = {"name": name, "symbol": symbol, "decimals": decimals}
        metadata.update(kwargs)

        tasks = [
            (
                getattr(token_contract.functions, key)().call()
                if (val := metadata.get(key)) is None
                else a_dummy(val)
            )
            for key in ["name", "symbol", "decimals"]
        ]
        name, symbol, decimals = await asyncio.gather(*tasks)
        if (
            not isinstance(name, str)
            or (symbol is not None and not isinstance(symbol, str))
            or not isinstance(decimals, int)
        ):
            raise ChainException("Invalid token metadata returned by contract")

        token = Token(token_contract, name, symbol, decimals)
        if cache_as is not None:
            setattr(self, cache_as, token)
        return token

    async def load_nft721(
        self,
        contract: HexAddress,
        *,
        cache_as: str | None = None,
        abi: Any | None = None,
    ) -> Optional["Nft721Collection"]:
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
        self, address: Union[HexAddress, "Account"], token: Token | None = None
    ) -> "CurrencyAmount":
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
        return await self.eth.get_transaction_count(cast(ChecksumAddress, address))

    async def get_gas_price(self) -> CurrencyAmount:
        """
        Get the current gas price as CurrencyAmount.

        For EIP-1559 networks, calculates the effective gas price by summing
        the base fee and priority fee. For legacy networks, returns the
        current gas price directly.

        Returns:
            CurrencyAmount representing the gas price

        Example:
            >>> gas_price = await chain.get_gas_price()
            >>> print(f"Gas price: {gas_price.to_fixed(2)} ETH")
        """
        return self.currency.to_amount(await get_gas_price(self))

    async def estimate_gas(
        self,
        transaction: TxParams,
        block_identifier: BlockIdentifier | None = None,
        state_override: StateOverride | None = None,
    ) -> int:
        return await self._web3.eth.estimate_gas(
            transaction, block_identifier, state_override
        )

    async def send_transaction(
        self, tx: TxParams, account: Optional["Account"] = None
    ) -> HexBytes:
        with ExitStack() as stack:
            if account is not None:
                stack.enter_context(account.onchain(self))
                tx["from"] = account.address
                tx["chainId"] = int(self.chain_id)
                if "to" in tx:
                    tx["to"] = to_checksum_address(tx["to"])

            return await self._web3.eth.send_transaction(tx)

        # silent mypy error "missing return statement"
        assert False, "unreachable"

    async def send_raw_transaction(self, data: HexStr | bytes) -> HexBytes:
        return await self._web3.eth.send_raw_transaction(data)

    async def wait_for_transaction_receipt(
        self, tx_hash: HexBytes, timeout: float = 180
    ) -> TxReceipt:
        return await self._web3.eth.wait_for_transaction_receipt(tx_hash, timeout)

    def contract(self, address: HexAddress, abi: Any | None = None) -> "Contract":
        address = to_checksum_address(address)
        contract = (
            self._web3.eth.contract(address, abi=abi) if abi is not None else address
        )
        return Contract(contract, self)

    def get_tx_scan(self, tx_hash: HexBytes):
        if not self.scan:
            return tx_hash
        hash_str = tx_hash.hex()
        if not hash_str.startswith("0x"):
            hash_str = f"0x{hash_str}"
        scan_base = self.scan.removesuffix("/")
        return f"{scan_base}/tx/{hash_str}"

    def __getattr__(self, name) -> Any:
        if name == self.currency.symbol:
            return self.currency
        return getattr(self._web3, name)

    def __str__(self) -> str:
        return self.name or f"Chain#{self.chain_id}"
