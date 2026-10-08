"""Context-scoped provider selection for :class:`~w3ext.chain.Chain`."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from web3.providers import AsyncBaseProvider
from web3.providers.async_base import AsyncJSONBaseProvider
from web3.types import RPCEndpoint

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
        self.request_kwargs = (
            dict(request_kwargs) if request_kwargs is not None else None
        )
        self.chainlist_provider = get_chain_provider(chain_id, self.request_kwargs)
        self.explicit_provider: AsyncBaseProvider | None = None
        self._provider_override: ContextVar[AsyncBaseProvider | None] = ContextVar(
            f"chain_provider_override_{id(self)}", default=None
        )

    @contextmanager
    def use_provider(self, provider: AsyncBaseProvider) -> Iterator[None]:
        """Pin a provider while processing a queued batch or verifying an RPC."""
        token = self._provider_override.set(provider)
        try:
            yield
        finally:
            self._provider_override.reset(token)

    def _selected_provider(self) -> AsyncBaseProvider | None:
        override = self._provider_override.get()
        if override is not None:
            return override
        context = _router_context.get()
        if context is not None and context.router is None:
            return self.explicit_provider
        if self.explicit_provider is not None and not (context and context.force):
            return self.explicit_provider
        if context is not None and context.router is not None:
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
            raise ChainException(
                f"No RPC provider configured for chain_id={self.chain_id}"
            )
        return provider

    # Web3 declares these policies as class attributes. The wrapper must expose
    # the active provider's values, including when a context overrides the RPC.
    @property
    def global_ccip_read_enabled(self) -> bool:
        return self._require_provider().global_ccip_read_enabled

    @global_ccip_read_enabled.setter
    def global_ccip_read_enabled(self, enabled: bool) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        self._require_provider().global_ccip_read_enabled = enabled

    @property
    def ccip_read_max_redirects(self) -> int:
        return self._require_provider().ccip_read_max_redirects

    @ccip_read_max_redirects.setter
    def ccip_read_max_redirects(self, max_redirects: int) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        self._require_provider().ccip_read_max_redirects = max_redirects

    async def request_func(self, async_w3, middleware_onion):
        # Let the selected provider compose its own middleware with Web3's.
        return await self._require_provider().request_func(async_w3, middleware_onion)

    async def batch_request_func(self, async_w3, middleware_onion):
        return await self._require_provider().batch_request_func(
            async_w3, middleware_onion
        )

    async def make_request(self, method: RPCEndpoint, params: Any) -> Any:
        return await self._require_provider().make_request(method, params)

    async def make_batch_request(self, requests: Any) -> Any:
        return await self._require_provider().make_batch_request(requests)

    async def is_connected(self, show_traceback: bool = False) -> bool:
        provider = self._selected_provider()
        return (
            await provider.is_connected(show_traceback)
            if provider is not None
            else False
        )

    async def disconnect(self) -> None:
        provider = self._selected_provider()
        if provider is not None:
            await provider.disconnect()


__all__ = ["ChainProviderRouter", "chain_providers_router"]
