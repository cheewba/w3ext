# pylint: disable=no-name-in-module
from contextlib import contextmanager
from typing import TypeVar, Any, TYPE_CHECKING, ContextManager

from eth_account.signers.local import LocalAccount
from eth_account import Account as Web3Account

from .utils import construct_async_sign_and_send_raw_middleware
from .chain import Token
if TYPE_CHECKING:
    from .chain import Chain
    from .token import CurrencyAmount

__all__ = ["Account", ]

Self = TypeVar("Self")


class Account:
    """ Wrapper around Web3 Account, not bound to any ``Chain`` instance. """
    _acc: LocalAccount = None

    @classmethod
    def from_key(cls, key: str) -> 'Account':
        instance = cls()
        instance._acc = Web3Account.from_key(key)
        return instance

    def use_chain(self, chain: "Chain") -> "ChainAccount":
        """ Return account bound to the provided chain instance. """
        return ChainAccount(self, chain)

    @contextmanager
    def onchain(self, *chains: "Chain") -> ContextManager[Union["ChainAccount"|List["ChainAccount"]]]:
        """ Context manager to add account to chains

            In the current context, all provided chains will know how to
            sign transactions sent from that account's address, without
            explicit access to that account.
        """
        chains_processed = []
        try:
            for chain in chains:
                if not chain.middleware_onion.get(self.address):
                    chain.middleware_onion.add(
                        construct_async_sign_and_send_raw_middleware(self._acc),
                        self.address,
                    )
                    chains_processed.append(chain)
            bound = [self.use_chain(chain) for chain in chains]
            yield bound[0] if len(bound) == 1 else bound
        finally:
            for chain in chains_processed:
                chain.middleware_onion.remove(self.address)

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self._acc, name)

    def __str__(self) -> str:
        return self.address


class ChainAccount:
    """ Account bound to the ``Chain`` instance. """
    _account: "Account"
    _chain: "Chain"

    def __init__(self, account: "Account", chain: "Chain") -> None:
        self._account = account
        self._chain = chain

    def chain(self) -> "Chain":
        return self._chain

    async def get_balance(self, token: 'Token' = None) -> 'CurrencyAmount':
        fn = token.get_balance if isinstance(token, Token) else self._chain.get_balance
        return await fn(self.address)

    def __getattr__(self, name) -> Any:
        return getattr(self._account, name)

    def __str__(self) -> str:
        return f"{self._chain}({self._account})"
