# pylint: disable=no-name-in-module
from typing import Optional, Any, Union, TYPE_CHECKING, Self, cast

from eth_typing import HexAddress
from web3.types import TxParams, HexBytes

from .utils import to_checksum_address
if TYPE_CHECKING:
    from .account import Account
    from .contract import Contract

__all__ = ['Currency', 'Token', 'CurrencyAmount', 'TokenAmount']


class Currency:
    def __init__(
        self,
        name: str,
        symbol: Optional[str] = None,
        decimals: int = 18
    ) -> None:
        self.name = name
        self.symbol = symbol or name
        self.decimals = decimals

    def to_amount(self, amount: int) -> 'CurrencyAmount':
        """ Build `CurrencyAmount` instance using amount as is. """
        return CurrencyAmount(self, amount)

    def parse_amount(self, amount: float) -> 'CurrencyAmount':
        """ Convert human-readable amount to the `CurrencyAmount`. """
        return CurrencyAmount(self, amount * 10 ** self.decimals)
    __call__ = parse_amount

    def __str__(self) -> str:
        return self.symbol or self.name
    def __repr__(self) -> str:
        return str(self)


class Token(Currency):
    MAX_AMOUNT = '0x' + 'f' * 64

    def __init__(
        self,
        contract: "Contract",
        name: str,
        symbol: Optional[str] = None,
        decimals: int = 18
    ) -> None:
        super().__init__(name, symbol or name, decimals)

        self.contract = contract

    @property
    def address(self):
        return self.contract.address

    def to_amount(self, amount: int) -> 'TokenAmount':
        """ Build `TokenAmount` instance using amount as is. """
        return TokenAmount(self, amount)

    def parse_amount(self, amount: float) -> 'TokenAmount':
        """ Convert human-readable amount to the `TokenAmount` instance. """
        return TokenAmount(self, amount * 10 ** self.decimals)
    __call__ = parse_amount

    async def get_balance(self, address: Union[HexAddress, "Account"]) -> "TokenAmount":
        address = cast(HexAddress, str(address))
        amount = await self.contract.functions.balanceOf(to_checksum_address(address)).call()
        return TokenAmount(self, amount)

    async def approve(
        self,
        account: "Account",
        spender: HexAddress,
        amount: Optional[Union[int, 'TokenAmount']] = None,
        transaction: Optional[TxParams] = None
    ) -> HexBytes:
        amount = (int(self.MAX_AMOUNT, 16) if amount is None
                  else (amount if isinstance(amount, TokenAmount)
                        else self.parse_amount(amount)).amount)
        return await self.contract.functions \
            .approve(to_checksum_address(spender), amount) \
            .transact(account, transaction)

    async def get_allowance(self, owner: HexAddress, spender: HexAddress) -> 'TokenAmount':
        allowance = await self.contract.functions \
            .allowance(to_checksum_address(owner), to_checksum_address(spender)) \
            .call()
        return TokenAmount(self, allowance)

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.contract, name)


class CurrencyAmount:
    currency: Currency
    amount: int

    def __init__(self, currency: Currency, amount: Union[int, str]) -> None:
        self.currency = currency
        if isinstance(amount, str):
            amount = int(amount, 16 if amount.startswith('0x') else 10)
        self.amount = amount

    def _to_amount(self: Self, val: Union[str, int, "CurrencyAmount"]) -> "CurrencyAmount":
        if not isinstance(val, CurrencyAmount):
            return self.__class__(self.currency, val)
        return val

    def _new_amount(self: Self, amount: Union[int, str]) -> Self:
        return self.__class__(self.currency, amount)

    def __add__(self: Self, other: Self) -> Self:
        return self._new_amount(self.amount + self._to_amount(other).amount)
    __radd__ = __add__

    def __sub__(self: Self, other: Self) -> Self:
        return self._new_amount(self.amount - self._to_amount(other).amount)
    __rsub__ = __sub__

    def __mul__(self: Self, other: Self) -> Self:
        return self._new_amount(int(self.amount * self._to_amount(other).amount / 10 ** other.currency.decimals))
    __rmul__ = __mul__

    def __div__(self: Self, other: Self) -> Self:
        return self._new_amount(int(self.amount / self._to_amount(other).amount / 10 ** other.currency.decimals))
    __rdiv__ = __div__

    def __gt__(self: Self, other: Self) -> bool:
        if isinstance(other, self.__class__):
            return self.amount > other.amount
        return False

    def __lt__(self: Self, other: Self) -> bool:
        if isinstance(other, self.__class__):
            return self.amount < other.amount
        return False

    def __ge__(self: Self, other: Self) -> bool:
        if isinstance(other, self.__class__):
            return self.amount >= other.amount
        return False

    def __le__(self: Self, other: Self) -> bool:
        if isinstance(other, self.__class__):
            return self.amount <= other.amount
        return False

    def __eq__(self: Self, other: Self) -> bool:  # type: ignore[override]
        if isinstance(other, self.__class__):
            return self.amount == other.amount
        return False

    def __ne__(self: Self, other: Self) -> bool:  # type: ignore[override]
        if isinstance(other, self.__class__):
            return self.amount != other.amount
        return False

    def __str__(self) -> str:
        return f"{self.to_fixed()} {self.currency}"

    def __repr__(self) -> str:
        return str(self)

    def to_fixed(self, decimals=3):
        return round(self.amount / 10 ** self.currency.decimals, decimals)


class TokenAmount(CurrencyAmount):
    currency: Token

    async def transfer(self, account: "Account", to: str, *, tx: Optional[TxParams] = None) -> HexBytes:
        return await self.currency.functions \
            .transfer(to, self.amount) \
            .transact(account, tx)