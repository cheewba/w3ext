# pylint: disable=no-name-in-module
import asyncio
import os
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Optional, Any, Union, cast, Type

from eth_typing import HexAddress, ChecksumAddress
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
from web3.types import HexBytes, TxParams, HexStr, TxReceipt

from .contract import Contract
from .exceptions import ChainException
from .token import Currency, Token, CurrencyAmount
from .nft import Nft721Collection
from .utils import is_eip1559, load_abi, to_checksum_address
from .batch import Batch, is_batch_method, to_batch_aware_method
from .account import Account

__all__ = ["Chain"]

ABI_PATH = os.path.join(os.path.dirname(__file__), 'abi')
_batcher_var = ContextVar("_batcher_var", default=None)


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
            # Custom getter: return whether _batcher_var indicates batching is active.
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
        name: Optional[str] = None
    ) -> None:
        self.__web3 = AsyncWeb3(self, middleware=self._DEFAULT_MIDDLEWARE)
        self.__web3.eth = AsyncEthProxy(self.__web3.eth, self)
        self._chain_id = str(chain_id)
        self._is_eip1559 = None
        self.currency = currency
        self.scan = scan
        self.name = name
        self._abi_cache = {}

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
        instance = cls(chain_id, currency, scan, name)
        await instance.connect_rpc(rpc, request_kwargs)
        return instance

    @property
    def _is_batching(self):
        return self.batcher is not None

    @property
    def batcher(self):
        return _batcher_var.get()

    @asynccontextmanager
    async def use_batch(self, max_size: int = 20, max_wait: float = 0.1):
        token = None
        try:
            async with Batch(self.__web3, max_size=max_size, max_wait=max_wait) as batcher:
                token = _batcher_var.set(batcher)
                yield batcher
        finally:
            if token:
                _batcher_var.reset(token)

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
        self.__web3.provider = (rpc if isinstance(rpc, AsyncBaseProvider) else
                                AsyncHTTPProvider(rpc, request_kwargs))
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
        **kwargs
    ) -> Optional['Token']:
        token_contract = self.contract(contract, abi=abi or await self.erc20_abi())

        tasks = [
            getattr(token_contract.functions, key)().call()
            if (val := kwargs.get(key)) is None else a_dummy(val)
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
        if isinstance(address, Account):
            address = address.address
        if token is not None:
            return await token.get_balance(address)

        address = to_checksum_address(str(address))
        amount = await self._web3.eth.get_balance(address)
        return CurrencyAmount(self.currency, amount)

    async def get_nonce(self, address: HexAddress) -> int:
        return await self.eth.get_transaction_count(
            cast(ChecksumAddress, address)
        )

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