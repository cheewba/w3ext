# pylint: disable=no-name-in-module
"""
Token module for w3ext library.

This module provides classes for handling currencies, tokens, and their amounts
with proper decimal handling and arithmetic operations. It includes support for
both native currencies (like ETH) and ERC20 tokens.
"""
from decimal import Decimal
from numbers import Number
from typing import Optional, Any, Union, TYPE_CHECKING, Self, cast

from eth_typing import HexAddress
from web3.types import TxParams, HexBytes
from .utils import to_checksum_address
if TYPE_CHECKING:
    from .account import Account
    from .contract import Contract

__all__ = ['Currency', 'Token', 'CurrencyAmount', 'TokenAmount']


UNIT_MULTIPLIERS = {
    'wei': 1,
    'kwei': 10 ** 3,
    'babbage': 10 ** 3,
    'mwei': 10 ** 6,
    'lovelace': 10 ** 6,
    'gwei': 10 ** 9,
    'shannon': 10 ** 9,
    'microether': 10 ** 12,
    'szabo': 10 ** 12,
    'milliether': 10 ** 15,
    'finney': 10 ** 15,
    'ether': 10 ** 18,
    'kether': 10 ** 21,
    'grand': 10 ** 21,
    'mether': 10 ** 24,
    'gether': 10 ** 27,
    'tether': 10 ** 30,
}


def _from_unit(amount: float, unit: str) -> int:
    """Convert amount in given unit to wei."""
    if unit not in UNIT_MULTIPLIERS:
        valid_units = ', '.join(UNIT_MULTIPLIERS.keys())
        raise ValueError(f"Unknown unit '{unit}'. Valid units: {valid_units}")
    return int(amount * UNIT_MULTIPLIERS[unit])


def _to_unit(amount: int, unit: str) -> float:
    """Convert amount in wei to given unit."""
    if unit not in UNIT_MULTIPLIERS:
        valid_units = ', '.join(UNIT_MULTIPLIERS.keys())
        raise ValueError(f"Unknown unit '{unit}'. Valid units: {valid_units}")
    return amount / UNIT_MULTIPLIERS[unit]


class Currency:
    """
    Represents a currency (native blockchain currency or token).

    This class provides the foundation for handling currencies with proper
    decimal precision and amount calculations. It's used as the base class
    for both native currencies (like ETH) and ERC20 tokens.

    Attributes:
        name (str): Full name of the currency (e.g., "Ethereum")
        symbol (str): Symbol of the currency (e.g., "ETH")
        decimals (int): Number of decimal places (default: 18)
    """

    def __init__(
        self,
        name: str,
        symbol: Optional[str] = None,
        decimals: int = 18
    ) -> None:
        """
        Initialize a Currency instance.

        Args:
            name: Full name of the currency (e.g., "Ethereum", "USD Coin")
            symbol: Currency symbol (e.g., "ETH", "USDC"). Defaults to name if not provided
            decimals: Number of decimal places for the currency (default: 18)
        """
        self.name = name
        self.symbol = symbol or name
        self.decimals = decimals

    def to_amount(self, amount: int) -> 'CurrencyAmount':
        """
        Create a CurrencyAmount using the raw amount value.

        Args:
            amount: Raw amount in smallest unit (e.g., wei for ETH)

        Returns:
            CurrencyAmount instance
        """
        return CurrencyAmount(self, amount)

    def parse_amount(
        self,
        amount: float,
        unit: str = 'ether'
    ) -> 'CurrencyAmount':
        """
        Convert human-readable amount to CurrencyAmount.

        Args:
            amount: Human-readable amount (e.g., 1.5 for 1.5 ETH)
            unit: Unit of the amount ('wei', 'gwei', 'ether', 'kether',
                  etc.). Default: 'ether'

        Returns:
            CurrencyAmount with proper decimal conversion

        Note:
            You can also use the shorthand: currency(amount, unit) which
            calls this method.

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount1 = eth.parse_amount(1.5)  # 1.5 ether (default)
            >>> amount2 = eth.parse_amount(1500000000, 'gwei')
            >>> amount3 = eth(1.5, 'ether')  # Shorthand with unit
            >>> print(amount1.amount)  # 1500000000000000000 (wei)
        """
        raw_amount = _from_unit(amount, unit)
        return CurrencyAmount(self, raw_amount)
    __call__ = parse_amount

    def __str__(self) -> str:
        return self.symbol or self.name
    def __repr__(self) -> str:
        return str(self)
    def __hash__(self) -> int:
        return hash(self.name + self.symbol)
    def __eq__(self, value: Self) -> bool:
        return hash(self) == hash(value)


class Token(Currency):
    """
    Represents an ERC20 token with blockchain contract integration.

    Extends Currency to provide blockchain-specific functionality like
    balance queries, transfers, and approvals. Integrates with the
    underlying smart contract for all token operations.

    Note:
        The recommended way to create Token instances is via the Chain.load_token()
        method, which automatically fetches token metadata and creates the contract.

    Attributes:
        contract (Contract): The underlying smart contract instance
        address (str): Token contract address
        chain_id (str): Blockchain chain ID where the token exists
    """

    # Maximum approval amount (2^256 - 1) used for unlimited approvals
    MAX_AMOUNT = '0x' + 'f' * 64

    def __init__(
        self,
        contract: "Contract",
        name: str,
        symbol: Optional[str] = None,
        decimals: int = 18
    ) -> None:
        """
        Initialize a Token instance.

        Args:
            contract: Contract instance for the token
            name: Token name (e.g., "USD Coin")
            symbol: Token symbol (e.g., "USDC")
            decimals: Number of decimal places (e.g., 6 for USDC)

        Note:
            Consider using Chain.load_token() instead of direct instantiation.
        """
        super().__init__(name, symbol or name, decimals)
        self.contract = contract

    @property
    def address(self) -> str:
        """Get the token contract address."""
        return self.contract.address

    @property
    def chain_id(self) -> str:
        """Get the chain ID where this token exists."""
        return self.contract.chain_id

    def to_amount(self, amount: int) -> 'TokenAmount':
        """
        Create a TokenAmount using the raw amount value.

        Args:
            amount: Raw amount in smallest token unit

        Returns:
            TokenAmount instance
        """
        return TokenAmount(self, amount)

    def parse_amount(
        self,
        amount: float,
        unit: str = 'ether'
    ) -> 'TokenAmount':
        """
        Convert human-readable amount to TokenAmount.

        Args:
            amount: Human-readable amount (e.g., 100.5 for 100.5 USDC)
            unit: Unit of the amount ('wei', 'gwei', 'ether', 'kether',
                  etc.). Default: 'ether'

        Returns:
            TokenAmount with proper decimal conversion

        Note:
            You can also use the shorthand: token(amount, unit) which
            calls this method.

        Example:
            >>> usdc = await chain.load_token("0x...")
            >>> amount1 = usdc.parse_amount(100.5)  # 100.5 ether (default)
            >>> amount2 = usdc.parse_amount(100500000, 'gwei')
            >>> amount3 = usdc(100.5, 'ether')  # Shorthand with unit
        """
        raw_amount = _from_unit(amount, unit)
        return TokenAmount(self, raw_amount)
    __call__ = parse_amount

    async def get_balance(self, address: Union[HexAddress, "Account"]) -> "TokenAmount":
        """
        Get token balance for an address.

        Args:
            address: Address or Account to check balance for

        Returns:
            TokenAmount representing the balance

        Example:
            >>> usdc = await chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
            >>> balance = await usdc.get_balance("0x742d35Cc6aF4c4a7E3F4BA9814d7492A9cC6F8c0")
            >>> print(f"Balance: {balance.to_fixed(2)} USDC")
        """
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
        """
        Approve another address to spend tokens on behalf of account.

        Args:
            account: Account that owns the tokens
            spender: Address to approve for spending
            amount: Amount to approve (None for unlimited approval)
            transaction: Optional transaction parameters

        Returns:
            Transaction hash

        Example:
            >>> usdc = await chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
            >>> uniswap_router = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
            >>> # Approve 100 USDC
            >>> tx_hash = await usdc.approve(my_account, uniswap_router, usdc(100))
            >>> # Unlimited approval
            >>> tx_hash = await usdc.approve(my_account, uniswap_router)
        """
        amount = (int(self.MAX_AMOUNT, 16) if amount is None
                  else (amount if isinstance(amount, TokenAmount)
                        else self.parse_amount(amount)).amount)
        return await self.contract.functions \
            .approve(to_checksum_address(spender), amount) \
            .transact(account, transaction)

    async def get_allowance(self, owner: HexAddress, spender: HexAddress) -> 'TokenAmount':
        """
        Get the amount a spender is allowed to spend on behalf of owner.

        Args:
            owner: Address that owns the tokens
            spender: Address that is approved to spend

        Returns:
            TokenAmount representing the allowance

        Example:
            >>> usdc = await chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
            >>> uniswap_router = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
            >>> allowance = await usdc.get_allowance(my_account.address, uniswap_router)
            >>> print(f"Allowance: {allowance.to_fixed(2)} USDC")
        """
        allowance = await self.contract.functions \
            .allowance(to_checksum_address(owner), to_checksum_address(spender)) \
            .call()
        return TokenAmount(self, allowance)

    def __getattr__(self, name) -> Any:
        """
        Delegate attribute access to the underlying contract.

        This allows using the token as a contract with predefined ABI and web3 instance.
        You can access contract functions, events, and other attributes directly.

        Example:
            >>> usdc = await chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
            >>> # Access contract functions directly
            >>> total_supply = await usdc.functions.totalSupply().call()
            >>> # Access contract events
            >>> transfer_filter = usdc.events.Transfer.create_filter(fromBlock='latest')
        """
        return getattr(self.contract, name)

    def __hash__(self) -> int:
        """Generate hash based on chain ID and contract address for use in sets/dicts."""
        return hash(self.chain_id + ":" + to_checksum_address(self.address))


class CurrencyAmount:
    """
    Represents an amount of a specific currency with proper decimal handling.

    This class provides safe arithmetic operations for currency amounts, ensuring
    proper decimal precision and preventing floating-point errors. All amounts
    are stored internally as integers in the smallest unit (e.g., wei for ETH).

    Attributes:
        currency: The Currency instance this amount belongs to
        amount: Raw amount in smallest currency unit (integer)

    Example:
        >>> eth = Currency("Ethereum", "ETH", 18)
        >>> amount1 = CurrencyAmount(eth, 1000000000000000000)  # 1 ETH in wei
        >>> amount2 = eth(1.5)  # 1.5 ETH using shorthand
        >>> total = amount1 + amount2  # Safe arithmetic
        >>> print(total.to_fixed(2))  # "2.50"
    """
    currency: Currency
    amount: int

    def __init__(self, currency: Currency, amount: Union[int, str]) -> None:
        """
        Initialize a CurrencyAmount.

        Args:
            currency: Currency instance this amount belongs to
            amount: Raw amount in smallest unit, or hex string

        Note:
            Hex strings (starting with '0x') are automatically detected and parsed.
        """
        self.currency = currency
        if isinstance(amount, str):
            amount = int(amount, 16 if amount.startswith('0x') else 10)
        self.amount = int(amount)

    def _to_amount(self: Self, val: Union[str, int, "CurrencyAmount"]) -> "CurrencyAmount":
        """Convert various types to CurrencyAmount for internal operations."""
        if not isinstance(val, CurrencyAmount):
            return self.__class__(self.currency, val)
        return val

    def _new_amount(self: Self, amount: Union[int, str]) -> Self:
        """Create a new amount instance with the same currency."""
        return self.__class__(self.currency, amount)

    def __add__(self: Self, other: Self) -> Self:
        """
        Add two currency amounts.

        Args:
            other: Another CurrencyAmount or raw amount

        Returns:
            New CurrencyAmount with the sum

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount1 = eth(1.0)    # 1 ETH
            >>> amount2 = eth(0.5)    # 0.5 ETH
            >>> total = amount1 + amount2  # 1.5 ETH
            >>> print(total.to_fixed(1))  # "1.5"
        """
        return self._new_amount(self.amount + self._to_amount(other).amount)
    __radd__ = __add__

    def __sub__(self: Self, other: Self) -> Self:
        """
        Subtract two currency amounts.

        Args:
            other: Another CurrencyAmount or raw amount

        Returns:
            New CurrencyAmount with the difference

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount1 = eth(2.0)    # 2 ETH
            >>> amount2 = eth(0.5)    # 0.5 ETH
            >>> diff = amount1 - amount2  # 1.5 ETH
            >>> print(diff.to_fixed(1))  # "1.5"
        """
        return self._new_amount(self.amount - self._to_amount(other).amount)
    __rsub__ = __sub__

    def __mul__(self: Self, other: Union[Self, Number]) -> Self:
        """
        Multiply currency amount by a number or another amount.

        Args:
            other: Number or CurrencyAmount to multiply by

        Returns:
            New CurrencyAmount with the product

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount = eth(1.5)     # 1.5 ETH
            >>> doubled = amount * 2  # 3.0 ETH
            >>> print(doubled.to_fixed(1))  # "3.0"
        """
        if isinstance(other, Number):
            return self._new_amount(int(self.amount * other))
        return self._new_amount(int(self.amount * self._to_amount(other).amount / 10 ** other.currency.decimals))
    __rmul__ = __mul__

    def __truediv__(self: Self, other: Self) -> Self:
        """
        Divide currency amount by a number or another amount.

        Args:
            other: Number or CurrencyAmount to divide by

        Returns:
            New CurrencyAmount with the quotient

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount = eth(3.0)     # 3.0 ETH
            >>> half = amount / 2     # 1.5 ETH
            >>> print(half.to_fixed(1))  # "1.5"
        """
        if isinstance(other, Number):
            return self._new_amount(int(self.amount / other))
        return self._new_amount(int(self.amount / self._to_amount(other).amount / 10 ** other.currency.decimals))
    __rtruediv__ = __truediv__

    def __neg__(self: Self) -> Self:
        """
        Return the negative of this amount.

        Returns:
            New CurrencyAmount with negated amount

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount = eth(1.5)      # 1.5 ETH
            >>> negative = -amount     # -1.5 ETH
            >>> print(negative.to_fixed(1))  # "-1.5"
        """
        return self._new_amount(-self.amount)

    def __abs__(self: Self) -> Self:
        """
        Return the absolute value of this amount.

        Returns:
            New CurrencyAmount with absolute value

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount = eth(-1.5)     # -1.5 ETH
            >>> absolute = abs(amount) # 1.5 ETH
            >>> print(absolute.to_fixed(1))  # "1.5"
        """
        return self._new_amount(abs(self.amount))

    def __gt__(self: Self, other: Self) -> bool:
        """
        Check if this amount is greater than another.

        Args:
            other: Another CurrencyAmount to compare with

        Returns:
            True if this amount is greater

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount1 = eth(2.0)  # 2 ETH
            >>> amount2 = eth(1.0)  # 1 ETH
            >>> print(amount1 > amount2)  # True
        """
        if isinstance(other, CurrencyAmount):
            return self.amount > other.amount
        raise TypeError(f"Can't compare {self.__class__.__name__} and {type(other)}")

    def __lt__(self: Self, other: Self) -> bool:
        """
        Check if this amount is less than another.

        Args:
            other: Another CurrencyAmount to compare with

        Returns:
            True if this amount is less

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount1 = eth(1.0)  # 1 ETH
            >>> amount2 = eth(2.0)  # 2 ETH
            >>> print(amount1 < amount2)  # True
        """
        if isinstance(other, CurrencyAmount):
            return self.amount < other.amount
        raise TypeError(f"Can't compare {self.__class__.__name__} and {type(other)}")

    def __ge__(self: Self, other: Self) -> bool:
        """
        Check if this amount is greater than or equal to another.

        Args:
            other: Another CurrencyAmount to compare with

        Returns:
            True if this amount is greater than or equal

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount1 = eth(2.0)  # 2 ETH
            >>> amount2 = eth(2.0)  # 2 ETH
            >>> print(amount1 >= amount2)  # True
        """
        if isinstance(other, CurrencyAmount):
            return self.amount >= other.amount
        raise TypeError(f"Can't compare {self.__class__.__name__} and {type(other)}")

    def __le__(self: Self, other: Self) -> bool:
        """
        Check if this amount is less than or equal to another.

        Args:
            other: Another CurrencyAmount to compare with

        Returns:
            True if this amount is less than or equal

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount1 = eth(1.0)  # 1 ETH
            >>> amount2 = eth(1.0)  # 1 ETH
            >>> print(amount1 <= amount2)  # True
        """
        if isinstance(other, CurrencyAmount):
            return self.amount <= other.amount
        raise TypeError(f"Can't compare {self.__class__.__name__} and {type(other)}")

    def __eq__(self: Self, other: Self) -> bool:  # type: ignore[override]
        """
        Check if this amount equals another (same amount and currency).

        Args:
            other: Another CurrencyAmount to compare with

        Returns:
            True if amounts and currencies are equal

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount1 = eth(1.0)  # 1 ETH
            >>> amount2 = eth(1.0)  # 1 ETH
            >>> print(amount1 == amount2)  # True
        """
        if isinstance(other, CurrencyAmount):
            return self.amount == other.amount and self.currency == other.currency
        return False

    def __ne__(self: Self, other: Self) -> bool:  # type: ignore[override]
        """
        Check if this amount is not equal to another.

        Args:
            other: Another CurrencyAmount to compare with

        Returns:
            True if amounts or currencies are different

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> usdc = Currency("USD Coin", "USDC", 6)
            >>> amount1 = eth(1.0)   # 1 ETH
            >>> amount2 = usdc(1.0)  # 1 USDC
            >>> print(amount1 != amount2)  # True (different currencies)
        """
        if isinstance(other, CurrencyAmount):
            return self.amount != other.amount or self.currency != other.currency
        return True

    def __str__(self) -> str:
        """
        String representation showing human-readable amount with currency symbol.

        Returns:
            Formatted string like "1.500 ETH"

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount = eth(1.5)
            >>> print(str(amount))  # "1.500 ETH"
        """
        return f"{self.to_sigfrac()} {self.currency}"

    def __repr__(self) -> str:
        """
        Developer representation (same as __str__ for readability).

        Returns:
            Same as __str__
        """
        return str(self)

    def to_fixed(self, decimals: int = 3, unit: str = 'ether') -> float:
        """
        Convert to human-readable decimal format.

        Args:
            decimals: Number of decimal places to show (default: 3)
            unit: Unit to display the amount in ('wei', 'gwei', 'ether',
                  etc.). Default: 'ether'

        Returns:
            Rounded decimal value in the specified unit

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount = eth(1.23456789)
            >>> print(amount.to_fixed(2))  # 1.23 (in ether)
            >>> print(amount.to_fixed(2, 'gwei'))  # 1234567890.00 (in gwei)
            >>> print(amount.to_fixed(0, 'wei'))  # 1234567890000000000 (in wei)
        """
        return round(_to_unit(self.amount, unit), decimals)

    def to_sigfrac(self, digits: int = 3, unit: str = 'ether') -> str:
        """
        Integer part unchanged.
        Fractional part: keep all leading zeros, then up to `digits` digits
        starting at the first non-zero fractional digit.
        If fractional part is all zeros → no decimal part shown.

        Args:
            digits: Number of significant fractional digits (default: 3)
            unit: Unit to display the amount in ('wei', 'gwei', 'ether',
                  etc.). Default: 'ether'

        Returns:
            Formatted string with significant fractional digits in the
            specified unit

        Example:
            >>> eth = Currency("Ethereum", "ETH", 18)
            >>> amount = eth(1.000123)
            >>> print(amount.to_sigfrac())  # "1.000123" (in ether)
            >>> print(amount.to_sigfrac(3, 'gwei'))  # "1000.123" (in gwei)
        """
        d = Decimal(str(_to_unit(self.amount, unit)))
        sign = '-' if d.is_signed() else ''
        d = abs(d)

        s = format(d, 'f')  # no exponent, plain decimal string
        if '.' not in s:
            return sign + s

        int_part, frac_part = s.split('.', 1)

        # all zeros after decimal → return integer only
        if frac_part.strip('0') == '':
            return sign + int_part

        # keep all leading zeros before the first non-zero
        i = next(idx for idx, ch in enumerate(frac_part) if ch != '0')
        keep_zeros = frac_part[:i]
        rest = frac_part[i:i+digits]  # up to N digits after the first non-zero
        return sign + int_part + '.' + keep_zeros + rest


class TokenAmount(CurrencyAmount):
    """
    Represents an amount of a specific ERC20 token with blockchain operations.

    Extends CurrencyAmount with token-specific functionality like transfers
    and approvals. Inherits all arithmetic and comparison operations from
    CurrencyAmount while adding blockchain transaction capabilities.

    Attributes:
        currency: The Token instance this amount belongs to
        amount: Raw amount in smallest token unit (integer)

    Example:
        >>> usdc = await chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
        >>> amount = usdc(100.5)  # 100.5 USDC
        >>> # Transfer tokens
        >>> tx_hash = await amount.transfer(account, "0x742d35Cc...")
        >>> # Approve spending
        >>> approve_hash = amount.approve(account, "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984")
    """
    currency: Token

    async def transfer(self, account: "Account", to: str, *, tx: Optional[TxParams] = None) -> HexBytes:
        """
        Transfer this token amount to another address.

        Args:
            account: Account to send from (must have sufficient balance)
            to: Recipient address
            tx: Optional transaction parameters (gas, gas_price, etc.)

        Returns:
            Transaction hash

        Example:
            >>> usdc = await chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
            >>> amount = usdc(100.0)  # 100 USDC
            >>> tx_hash = await amount.transfer(my_account, "0x742d35Cc...")
            >>> print(f"Transfer sent: {tx_hash.hex()}")
        """
        return await self.currency.functions \
            .transfer(to, self.amount) \
            .transact(account, tx)

    def approve(
        self,
        account: "Account",
        spender: HexAddress,
        transaction: Optional[TxParams] = None
    ) -> HexBytes:
        """
        Approve another address to spend this token amount.

        Args:
            account: Account that owns the tokens
            spender: Address to approve for spending
            transaction: Optional transaction parameters

        Returns:
            Transaction hash

        Example:
            >>> usdc = await chain.load_token("0xA0b86a33E6441E6C7D3E4C5B")
            >>> amount = usdc(100.0)  # Approve 100 USDC
            >>> uniswap_router = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
            >>> tx_hash = amount.approve(my_account, uniswap_router)
            >>> print(f"Approval sent: {tx_hash.hex()}")
        """
        return self.currency.approve(account, spender, self, transaction)