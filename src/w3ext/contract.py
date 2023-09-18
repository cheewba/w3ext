from inspect import Signature, Parameter, BoundArguments
from typing import Any, Optional, Tuple, TYPE_CHECKING

from web3.contract.async_contract import AsyncContract, AsyncContractFunction

from .utils import fill_nonce, fill_gas_price

if TYPE_CHECKING:
    from .chain import Chain, Account

__all__ = ["Contract"]


_overloadedTransactSig = Signature([
    Parameter('account', Parameter.POSITIONAL_OR_KEYWORD),
    Parameter('transaction', Parameter.POSITIONAL_OR_KEYWORD, default=None),
])

def signatureMatch(sig, *args, **kwargs) -> Tuple[bool, Optional[BoundArguments]]:
    try:
        # if arguments bound
        return True, _overloadedTransactSig.bind(*args, **kwargs)
    except TypeError:
        return False, None


class ContractFunction:
    def __init__(self, function: AsyncContractFunction, chain: "Chain") -> None:
        self.__function = function
        self._chain = chain

    async def build_transaction(self, *args, **kwargs):
        tx, _ = await self._build_transaction(*args, **kwargs)
        return tx

    async def transact(self, *args, **kwargs):
        tx, account = await self._build_transaction(*args, **kwargs)
        return await self._chain.eth.send_raw_transaction(
            account.sign_transaction(tx).rawTransaction
        )

    async def _build_transaction(self, *args, **kwargs):
        match, bound = signatureMatch(_overloadedTransactSig, *args, **kwargs)
        if not match:
            return await self.__function.build_transaction(*args, **kwargs), None

        kwargs = dict(bound.arguments)
        account: Account = kwargs.pop('account')
        tx = kwargs.setdefault('transaction', {}) or {}
        tx['from'] = account.address
        tx = await fill_nonce(self._chain, tx)
        tx = await fill_gas_price(self._chain, tx)
        tx = await self.__function.build_transaction(tx)

        return tx, account

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.__function, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.__class__(self.__function(*args, **kwargs), self._chain)


class ContractFunctions:
    def __init__(self, contract: AsyncContract, chain: "Chain") -> None:
        self.__contract = contract
        self._chain = chain

    def __getattr__(self, function_name: str) -> "AsyncContractFunction":
        return ContractFunction(getattr(self.__contract.functions, function_name), self._chain)


class Contract:
    def __init__(self, contract: AsyncContract, chain: "Chain") -> None:
        self.__contract = contract
        self.functions = ContractFunctions(contract, chain)

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.__contract, name)