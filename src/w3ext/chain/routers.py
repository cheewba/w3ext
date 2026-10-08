"""Context-scoped provider selection for :class:`~w3ext.chain.Chain`."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from web3.providers import AsyncBaseProvider
from web3.providers.async_base import AsyncJSONBaseProvider

from ..exceptions import ChainException
from .chainlist import get_chain_provider


class ChainProviderRouter(Protocol):
    """Return a provider for a chain ID, or ``None`` to use Chainlist."""

    def get_chain_provider(
        self, chain_id: int | str, request_kwargs: dict[str, Any] | None = None
    ) -> AsyncBaseProvider | None: ...


@dataclass(frozen=True)
class _RouterContext:
    router: ChainProviderRouter | None
    force: bool


_router_context: ContextVar[_RouterContext | None] = ContextVar(
    "chain_providers_router", default=None
)


@contextmanager
def chain_providers_router(
    router: ChainProviderRouter | None, *, force: bool = False
) -> Iterator[None]:
    """Use the closest router in this async context.

    An explicit RPC takes precedence unless ``force=True``. If the router has no
    provider for a chain, Chainlist is used. Passing ``None`` disables all
    routers, including Chainlist, while preserving explicitly connected RPCs.
    """
    token = _router_context.set(_RouterContext(router, force))
    try:
        yield
    finally:
        _router_context.reset(token)


class RoutingProvider(AsyncJSONBaseProvider):
    """Keep Web3's middleware bound to one provider while routing each request."""

    def __init__(
        self, chain_id: int | str, request_kwargs: dict[str, Any] | None = None
    ) -> None:
        super().__init__()
        self.chain_id = chain_id
        self.request_kwargs = dict(request_kwargs) if request_kwargs is not None else None
        self.chainlist_provider = get_chain_provider(chain_id, self.request_kwargs)
        self.explicit_provider: AsyncBaseProvider | None = None

    def _selected_provider(self) -> AsyncBaseProvider | None:
        context = _router_context.get()
        if context is not None and context.router is None:
            return self.explicit_provider
        if self.explicit_provider is not None and not (context and context.force):
            return self.explicit_provider
        if context is not None:
            provider = context.router.get_chain_provider(
                self.chain_id, self.request_kwargs
            )
            if provider is not None:
                if not isinstance(provider, AsyncBaseProvider):
                    raise TypeError("Router must return an AsyncBaseProvider or None")
                return provider
        return self.chainlist_provider

    def _require_provider(self) -> AsyncBaseProvider:
        provider = self._selected_provider()
        if provider is None:
            raise ChainException(f"No RPC provider configured for chain_id={self.chain_id}")
        return provider

    async def make_request(self, method: str, params: Any) -> Any:
        return await self._require_provider().make_request(method, params)

    async def make_batch_request(self, requests: Any) -> Any:
        return await self._require_provider().make_batch_request(requests)

    async def is_connected(self, show_traceback: bool = False) -> bool:
        provider = self._selected_provider()
        return (
            await provider.is_connected(show_traceback) if provider is not None else False
        )

    async def disconnect(self) -> None:
        provider = self._selected_provider()
        if provider is not None:
            await provider.disconnect()


__all__ = ["ChainProviderRouter", "chain_providers_router"]
