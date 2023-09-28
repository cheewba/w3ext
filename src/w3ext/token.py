# pylint: disable=no-name-in-module
from numbers import Number
from typing import Optional, TypeVar, Any, Union, TYPE_CHECKING

from eth_typing import Address
from web3.contract.async_contract import AsyncContract
from web3.types import TxParams, HexBytes

from .utils import to_checksum_address
if TYPE_CHECKING:
    from .account import Account


SelfCurrencyAmount = TypeVar("SelfCurrencyAmount", bound="CurrencyAmount")


class Currency:
    def __init__(self, name: str, symbol: str = None, decimals: int = 18) -> None:
        self.name = name
        self.symbol = symbol or name
        self.decimals = decimals

    def to_amount(self, amount: Number) -> 'CurrencyAmount':
        """ Build `CurrencyAmount` instance using amount as is. """
        return CurrencyAmount(self, amount)

    def parse_amount(self, amount: Number) -> 'CurrencyAmount':
        """ Convert human-readable amount to the `CurrencyAmount`. """
        return CurrencyAmount(self, amount * 10 ** self.decimals)
    __call__ = parse_amount

    def __str__(self) -> str:
        return self.symbol or self.name


class Token(Currency):
    MAX_AMOUNT = '0x' + 'f' * 64

    def __init__(self, contract: AsyncContract, name: str, symbol: str = None, decimals: int = 18) -> None:
        super().__init__(name, symbol, decimals)
        self.contract = contract

    @property
    def address(self):
        return self.contract.address

    def to_amount(self, amount: Number) -> 'TokenAmount':
        """ Build `TokenAmount` instance using amount as is. """
        return TokenAmount(self, amount)

    def parse_amount(self, amount: Number) -> 'TokenAmount':
        """ Convert human-readable amount to the `TokenAmount` instance. """
        return TokenAmount(self, amount * 10 ** self.decimals)
    __call__ = parse_amount

    async def get_balance(self, address: str) -> "TokenAmount":
        amount = await self.contract.functions.balanceOf(to_checksum_address(address)).call()
        return TokenAmount(self, amount)

    async def approve(
        self,
        account: "Account",
        spender: Address,
        amount: Optional[Union[Number, 'TokenAmount']] = None,
        transaction: Optional[TxParams] = None
    ) -> HexBytes:
        amount = (self.MAX_AMOUNT if amount is None
                  else (amount if isinstance(amount, TokenAmount)
                        else self.parse_amount(amount)).amount)
        return await self.contract.functions \
            .approve(to_checksum_address(spender), amount) \
            .transact(account, transaction)

    async def get_allowance(self, owner: Address, spender: Address) -> 'TokenAmount':
        allowance = await self.contract.functions \
            .allowance(to_checksum_address(owner), to_checksum_address(spender)) \
            .call()
        return TokenAmount(self, allowance)

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.contract, name)


class CurrencyAmount:
    currency: Currency

    def __init__(self, currency: Currency, amount: Number) -> None:
        self.currency = currency
        self.amount = amount

    def to_amount(self, val: any) -> SelfCurrencyAmount:
        if not isinstance(val, CurrencyAmount):
            val = CurrencyAmount(self.currency, float(val))
        return val

    def __add__(self, other: SelfCurrencyAmount) -> SelfCurrencyAmount:
        return self.__class__(self.currency, self.amount + self.to_amount(other).amount)
    __radd__ = __add__

    def __sub__(self, other: SelfCurrencyAmount) -> SelfCurrencyAmount:
        return self.__class__(self.currency, self.amount - self.to_amount(other).amount)
    __rsub__ = __sub__

    def __mul__(self, other: SelfCurrencyAmount) -> SelfCurrencyAmount:
        return self.__class__(self.currency,
                              int(self.amount * self.to_amount(other).amount / 10 ** other.currency.decimals))
    __rmul__ = __mul__

    def __div__(self, other: SelfCurrencyAmount) -> SelfCurrencyAmount:
        return self.__class__(self.currency,
                              int(self.amount / self.to_amount(other).amount / 10 ** other.currency.decimals))
    __rdiv__ = __div__

    def __gt__(self, other: SelfCurrencyAmount) -> bool:
        if isinstance(other, self.__class__):
            return self.amount > other.amount
        return False

    def __lt__(self, other: SelfCurrencyAmount) -> bool:
        if isinstance(other, self.__class__):
            return self.amount < other.amount
        return False

    def __ge__(self, other: SelfCurrencyAmount) -> bool:
        if isinstance(other, self.__class__):
            return self.amount >= other.amount
        return False

    def __le__(self, other: SelfCurrencyAmount) -> bool:
        if isinstance(other, self.__class__):
            return self.amount <= other.amount
        return False

    def __eq__(self, other: SelfCurrencyAmount) -> bool:
        if isinstance(other, self.__class__):
            return self.amount == other.amount
        return False

    def __ne__(self, other: SelfCurrencyAmount) -> bool:
        if isinstance(other, self.__class__):
            return self.amount != other.amount
        return False

    def __str__(self) -> str:
        return f"{self.to_fixed()} {self.currency}"

    def to_fixed(self, decimals=3):
        return round(self.amount / 10 ** self.currency.decimals, decimals)


class TokenAmount(CurrencyAmount):
    currency: Token

    async def transfer(self, account: "Account", to: str, *, tx: Optional[TxParams] = None) -> HexBytes:
        return await self.currency.functions \
            .transfer(to, self.amount) \
            .transact(account, tx)