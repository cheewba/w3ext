# pylint: disable=no-name-in-module
import os
from contextlib import contextmanager
from functools import wraps
from typing import TypeVar, Any, TYPE_CHECKING

from eth_account.signers.local import LocalAccount
from eth_account import Account as Web3Account

from .utils import construct_async_sign_and_send_raw_middleware
from .chain import Token
if TYPE_CHECKING:
    from .chain import Chain, CurrencyAmount

__all__ = ["Account", ]

Self = TypeVar("Self")

ABI_PATH = os.path.join(os.path.dirname(__file__), 'abi')


def _onchain_required(fn):
    @wraps(fn)
    def inner(self: "Account", *args, **kwargs):
        assert self._chain is not None, f"Chain context is required for {fn.__name__}"
        return fn(self, *args, **kwargs)
    return inner


class Account:
    __acc: LocalAccount = None
    _chain: "Chain" = None

    @classmethod
    def from_key(cls, key: str) -> 'Account':
        instance = Account()
        instance.__acc = Web3Account.from_key(key)
        return instance

    @_onchain_required
    async def get_balance(self, token: 'Token' = None) -> 'CurrencyAmount':
        return await self._chain.get_balance(self.address, token)

    @contextmanager
    def onchain(self, chain: "Chain") -> Self:
        added_middleware = False
        try:
            old, self._chain = self._chain, chain
            if not chain.middleware_onion.get(self.address):
                chain.middleware_onion.add(
                    construct_async_sign_and_send_raw_middleware(self.__acc),
                    self.address,
                )
                added_middleware = True
            yield self
        finally:
            self._chain = old
            if added_middleware:
                chain.middleware_onion.remove(self.address)

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.__acc, name)

    def __str__(self) -> str:
        return self.address