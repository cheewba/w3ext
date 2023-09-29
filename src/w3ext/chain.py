# pylint: disable=no-name-in-module
import asyncio
import os
from contextlib import ExitStack
from typing import Optional, Any, Union, TYPE_CHECKING, cast, Type

from eth_typing import HexAddress, ChecksumAddress
from web3 import AsyncWeb3, AsyncHTTPProvider
from web3.middleware.geth_poa import async_geth_poa_middleware
from web3.types import HexBytes, TxParams, HexStr, TxReceipt

from .contract import Contract
from .token import Currency, Token, CurrencyAmount
from .nft import Nft721Collection
from .utils import is_eip1559, load_abi, to_checksum_address
if TYPE_CHECKING:
    from .account import Account

__all__ = ["Chain"]

ABI_PATH = os.path.join(os.path.dirname(__file__), 'abi')


class Chain:
    __web3: AsyncWeb3
    _currency: 'Currency'
    _chain_id: Optional[int]
    _is_eip1559: Optional[bool] = None

    scan: Optional[str]

    def __init__(self, rpc: Union[str, AsyncWeb3], *,
                 currency: Union[str, 'Currency'] = 'ETH',
                 chain_id: Optional[int] = None,
                 scan: Optional[str] = None,
                 name: Optional[str] = None) -> None:
        self.__web3 = (rpc if isinstance(rpc, AsyncWeb3) else
                       AsyncWeb3(AsyncHTTPProvider(rpc)))
        self.__web3.middleware_onion.inject(async_geth_poa_middleware, layer=0)

        if not isinstance(currency, Currency):
            currency = Currency(currency, currency)
        self._currency = currency
        setattr(self, currency.symbol, currency)
        self._chain_id = chain_id

        self.scan = scan
        self.name = name

    @classmethod
    async def connect(
        cls: Type["Chain"],
        rpc: str, *,
        currency: Union[str, 'Currency'] = 'ETH',
        chain_id: Optional[int] = None,
        scan: Optional[str] = None,
        name: Optional[str] = None,
    ) -> "Chain":
        """ Convenient way to initialize and validate a Chain instance. """
        w3 = AsyncWeb3(AsyncHTTPProvider(rpc))

        w3_chain_id = await w3.eth.chain_id
        if chain_id != None:
            assert chain_id == w3_chain_id, \
                f"Rpc chain ID doesn't match: {w3_chain_id} <> {chain_id}"
        chain_id = chain_id or w3_chain_id

        return cls(w3, currency=currency, chain_id=chain_id,
                   scan=scan, name=name)

    @property
    def currency(self):
        return self._currency

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
            self._is_eip1559 = await is_eip1559(self.__web3)
        return self._is_eip1559

    async def load_token(
        self,
        contract: HexAddress, *,
        cache_as: Optional[str] = None,
        abi: Optional[Any] = None
    ) -> Optional['Token']:
        token_contract = self.contract(contract, abi=abi or await self.erc20_abi())
        name, symbol, decimals = await asyncio.gather(*[
            token_contract.functions.name().call(),
            token_contract.functions.symbol().call(),
            token_contract.functions.decimals().call(),
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
        amount = await self.__web3.eth.get_balance(address)
        return CurrencyAmount(self.currency, amount)

    async def get_nonce(self, address: HexAddress) -> int:
        return await self.eth.get_transaction_count(  # type: ignore
            cast(ChecksumAddress, address)
        )

    async def send_transaction(self, tx: TxParams, account: Optional["Account"] = None) -> HexBytes:
        with ExitStack() as stack:
            if account is not None:
                stack.enter_context(account.onchain())
                tx['from'] = account.address

            return await self.__web3.eth.send_transaction(tx)

        # silent mypy error "missing return statement"
        assert False, "unreachable"

    async def send_raw_transaction(self, data: Union[HexStr, bytes]) -> HexBytes:
        return await self.__web3.eth.send_raw_transaction(data)

    async def wait_for_transaction_receipt(self, tx_hash: HexBytes, timeout: float = 180) -> TxReceipt:
        return await self.__web3.eth.wait_for_transaction_receipt(tx_hash, timeout)

    def contract(self, address: HexAddress, abi: Any) -> 'Contract':
        return Contract(self.__web3.eth.contract(to_checksum_address(address), abi=abi), self)

    def get_tx_scan(self, tx_hash: HexBytes):
        if not self.scan:
            return tx_hash
        return '/'.join([self.scan if not self.scan.endswith('/') else self.scan[:-1], 'tx', tx_hash.hex()])

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.__web3, name)

    def __str__(self) -> str:
        return self.name or f"Chain#{self.chain_id}"