# pylint: disable=no-name-in-module
import asyncio
import os
from contextlib import ExitStack, asynccontextmanager
from collections import deque
from functools import wraps
from typing import Optional, Any, Union, cast, Type

from eth_typing import HexAddress, ChecksumAddress
from web3 import AsyncWeb3, AsyncHTTPProvider
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
                # Bind the class method on the proxy (so that inside it, "cls" becomes proxy's type or proxy itself).
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
        if  name not in ('_eth', '_chain'):
            setattr(self._eth, name, value)
        super().__setattr__(name, value)


class Chain:
    _currency: 'Currency'
    _chain_id: str
    __web3: AsyncWeb3

    _is_eip1559: Optional[bool] = None

    scan: Optional[str]

    def __init__(
        self,
        chain_id: Union[str, int],
        currency: Union[str, 'Currency'] = 'ETH',
        scan: Optional[str] = None,
        name: Optional[str] = None
    ) -> None:

        self.__web3 = AsyncWeb3(middleware=[
            GasPriceStrategyMiddleware,
            AttributeDictMiddleware,
            ValidationMiddleware,
            BufferedGasEstimateMiddleware,
            ExtraDataToPOAMiddleware,
        ])
        self.__web3.eth = AsyncEthProxy(self.__web3.eth, self)
        self._chain_id = str(chain_id)

        self.currency = currency
        self.scan = scan
        self.name = name

        self._batchers = deque()

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
        """ Convenient way to initialize Chain instance and connect to RPC. """
        instance = cls(chain_id, currency, scan, name)
        await instance.connect_rpc(rpc, request_kwargs)
        return instance

    @property
    def _is_batching(self):
        return len(self._batchers) > 0

    @asynccontextmanager
    async def use_batch(self, max_size: int = 20, max_wait: float = 0.1):
        added = False
        try:
            async with Batch(self.__web3, max_size=max_size, max_wait=max_wait) as batcher:
                self._batchers.append(batcher)
                added = True
                yield batcher
        finally:
            if added:
                self._batchers.pop()
            # hack to be sure the batching is still on
            self.__web3.provider._is_batching = len(self._batchers) > 0

    async def _add_to_batch_request_info(self, request_info):
        return await self._batchers[-1]._add_request_info(request_info)

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
        """ AsyncWeb3 instance, for internal use only. """
        return self.__web3

    @property
    def currency(self):
        return self._currency

    @currency.setter
    def currency(self, currency: Union['Currency', str]):
        self._currency = (currency if isinstance(currency, Currency)
                          else Currency(currency, currency))

    @property
    def chain_id(self):
        return self._chain_id

    async def _get_abi(self, name):
        key = f'_{name}_abi'
        abi = getattr(self, key, None)
        if not abi:
            abi = await self._load_abi(f'{name}.json')
            setattr(self, key, abi)
        return abi

    async def _load_abi(self, name) -> Any:
        return await load_abi(os.path.join(ABI_PATH, name))

    async def erc20_abi(self):
        return await self._get_abi('erc20')

    async def erc721_abi(self):
        return await self._get_abi('erc721')

    async def is_eip1559(self) -> bool:
        if (self._is_eip1559 is None):
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

        name, symbol, decimals = await asyncio.gather(*[
            getattr(token_contract.functions, key)().call()
            if (val := kwargs.get(key, None)) is None else a_dummy(val)
            for key in ['name', 'symbol', 'decimals']
        ])
        token = Token(token_contract, name, symbol, decimals)
        if (cache_as is not None):
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
        if (cache_as is not None):
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
        return await self.eth.get_transaction_count(  # type: ignore
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
        contract = (self._web3.eth.contract(address, abi=abi)
                    if abi is not None else address)
        return Contract(contract, self)

    def get_tx_scan(self, tx_hash: HexBytes):
        if not self.scan:
            return tx_hash
        hash = tx_hash.hex()
        if not hash.startswith('0x'):
            hash = f"0x{hash}"
        return '/'.join([self.scan if not self.scan.endswith('/') else self.scan[:-1], 'tx', hash])

    def __getattr__(self, name) -> Any:
        if name == self.currency.symbol:
            return self.currency
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self._web3, name)

    def __str__(self) -> str:
        return self.name or f"Chain#{self.chain_id}"