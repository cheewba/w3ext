"""Context-scoped provider selection for :class:`~w3ext.chain.Chain`."""

import asyncio
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, cast

from web3._utils.caching.caching_utils import generate_cache_key
from web3._utils.validation import raise_error_for_batch_response
from web3.manager import RequestManager
from web3.providers import AsyncBaseProvider
from web3.providers.async_base import AsyncJSONBaseProvider
from web3.providers.persistent import PersistentConnectionProvider
from web3.types import RPCEndpoint

from ..exceptions import ChainException
from .chainlist import get_chain_provider
from .middlewares import DynamicContextMiddleware, _middlewares_ctx_var


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
_router_provider_cache: ContextVar[
    dict[
        tuple[int, weakref.ReferenceType[asyncio.Task[Any]] | None],
        AsyncBaseProvider | None,
    ]
    | None
] = ContextVar("chain_router_provider_cache", default=None)


@contextmanager
def chain_providers_router(
    router: ChainProviderRouter | None, *, force: bool = False
) -> Iterator[None]:
    """Use the closest router in this async context.

    An explicit RPC takes precedence unless ``force=True``. If the router has no
    provider for a chain, Chainlist is used. Passing ``None`` disables all
    routers, including Chainlist, while preserving explicitly connected RPCs.
    A task reuses its first provider selection for each chain until this context
    exits.
    """
    token = _router_context.set(_RouterContext(router, force))
    cache_token = _router_provider_cache.set({})
    try:
        yield
    finally:
        _router_provider_cache.reset(cache_token)
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
        self._collecting_batch_info: ContextVar[bool] = ContextVar(
            f"chain_collecting_batch_info_{id(self)}", default=False
        )
        self._executing_batch: ContextVar[bool] = ContextVar(
            f"chain_executing_batch_{id(self)}", default=False
        )
        self._processing_responses_directly: ContextVar[bool] = ContextVar(
            f"chain_processing_responses_directly_{id(self)}", default=False
        )
        self._bypass_context_request_processor: ContextVar[bool] = ContextVar(
            f"chain_bypass_context_request_processor_{id(self)}", default=False
        )

    @contextmanager
    def use_provider(self, provider: AsyncBaseProvider) -> Iterator[None]:
        """Pin a provider while processing a queued batch or verifying an RPC."""
        token = self._provider_override.set(provider)
        try:
            yield
        finally:
            self._provider_override.reset(token)

    @contextmanager
    def collect_batch_info(self) -> Iterator[None]:
        """Keep Web3 from caching persistent requests before a batch exists."""
        token = self._collecting_batch_info.set(True)
        try:
            yield
        finally:
            self._collecting_batch_info.reset(token)

    @contextmanager
    def execute_batch(self) -> Iterator[None]:
        """Let middleware's nested RPCs run outside request collection."""
        token = self._executing_batch.set(True)
        try:
            yield
        finally:
            self._executing_batch.reset(token)

    @contextmanager
    def process_responses_directly(self) -> Iterator[None]:
        """Apply Web3 response middleware without a persistent cache entry."""
        token = self._processing_responses_directly.set(True)
        try:
            yield
        finally:
            self._processing_responses_directly.reset(token)

    @contextmanager
    def bypass_context_request_processor(self) -> Iterator[None]:
        """Avoid wrapping a request twice when the manager runs its full handler."""
        token = self._bypass_context_request_processor.set(True)
        try:
            yield
        finally:
            self._bypass_context_request_processor.reset(token)

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
            cache = _router_provider_cache.get() or {}
            try:
                task = asyncio.current_task()
            except RuntimeError:
                task = None
            key = (id(self), weakref.ref(task) if task is not None else None)
            if key in cache:
                provider = cache[key]
            else:
                provider = context.router.get_chain_provider(
                    self.chain_id, self.request_kwargs
                )
                # Copy before writing: child tasks inherit context values.
                _router_provider_cache.set({**cache, key: provider})
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


class RoutingRequestManager(RequestManager):
    """Expose persistent providers directly to Web3's subscription machinery."""

    _routing_provider: RoutingProvider

    @property
    def _provider(self) -> AsyncBaseProvider:
        if self._routing_provider._collecting_batch_info.get():
            return self._routing_provider
        selected = self._routing_provider._selected_provider()
        if isinstance(selected, PersistentConnectionProvider):
            return selected
        return self._routing_provider

    @_provider.setter
    def _provider(self, provider: AsyncBaseProvider) -> None:
        self._routing_provider.explicit_provider = provider

    @property
    def _request_processor(self):  # pyright: ignore[reportIncompatibleVariableOverride]
        provider = self._provider
        if not isinstance(provider, PersistentConnectionProvider):
            raise ChainException("A persistent RPC provider is not selected")
        return provider._request_processor

    async def socket_request(self, method, params, response_formatters=None) -> Any:
        chain = cast(Any, self.w3)._chain
        active = (_middlewares_ctx_var.get() or {}).get(id(chain))
        if not active:
            return await super().socket_request(method, params, response_formatters)

        provider = self._provider
        if not isinstance(provider, PersistentConnectionProvider):
            return await super().socket_request(method, params, response_formatters)
        formatters = cast(Any, response_formatters or ((), (), ()))
        sent_ids = []

        async def make_request(next_method, next_params):
            with (
                self._routing_provider.use_provider(provider),
                self._routing_provider.bypass_context_request_processor(),
                self._routing_provider.process_responses_directly(),
            ):
                rpc_request = await self.send(next_method, next_params)
                request_id = rpc_request.get("id")
                if request_id is None:
                    raise ChainException("Persistent RPC request has no ID")
                provider._request_processor.cache_request_information(
                    request_id,
                    rpc_request.get("method", next_method),
                    rpc_request.get("params", next_params),
                    formatters,
                )
                try:
                    recv_func = await provider.recv_func(
                        cast(Any, self.w3), cast(Any, self.middleware_onion)
                    )
                    response = await recv_func(rpc_request)
                except BaseException:
                    provider._request_processor.pop_cached_request_information(
                        generate_cache_key(request_id)
                    )
                    raise
                sent_ids.append(request_id)
                return response

        dynamic = DynamicContextMiddleware(self.w3, chain)
        handler = await dynamic._wrap_active_middlewares(make_request)
        with self._routing_provider.use_provider(provider):
            try:
                response = await handler(method, params)
            except BaseException:
                for request_id in sent_ids:
                    provider._request_processor.pop_cached_request_information(
                        generate_cache_key(request_id)
                    )
                raise

            final_id = response.get("id")
            for request_id in sent_ids:
                if request_id != final_id:
                    provider._request_processor.pop_cached_request_information(
                        generate_cache_key(request_id)
                    )
            if final_id in sent_ids:
                return await self._process_response(response)
            with self._routing_provider.process_responses_directly():
                middleware_stack = self.middleware_onion.as_tuple_of_middleware()
                position = next(
                    (
                        index
                        for index, item in enumerate(middleware_stack)
                        if isinstance(item, type)
                        and issubclass(item, DynamicContextMiddleware)
                    ),
                    len(middleware_stack),
                )
                for item in reversed(middleware_stack[position + 1 :]):
                    response = await cast(Any, item)(self.w3).async_response_processor(
                        method, response
                    )
            return self._format_batched_response(
                ((method, params), formatters), response
            )

    async def _async_make_batch_request(self, requests_info):
        provider = self._provider
        if not (
            self._routing_provider._executing_batch.get()
            and isinstance(provider, PersistentConnectionProvider)
        ):
            return await super()._async_make_batch_request(requests_info)

        # Web3 normally looks up persistent batch formatters by a predicted RPC ID.
        # Middleware can issue an RPC before the batch is encoded, consuming that ID.
        # Keep each formatter with its original request and format the returned list.
        request_func = await provider.batch_request_func(
            cast(Any, self.w3), cast(Any, self.middleware_onion)
        )
        unpacked = await asyncio.gather(*requests_info)
        with self._routing_provider.process_responses_directly():
            response = await request_func(
                [(method, params) for (method, params), _ in unpacked]
            )
        if not isinstance(response, list):
            raise_error_for_batch_response(response, self.logger)
        if len(response) != len(unpacked):
            raise ChainException("Batch response count does not match requests")
        return [
            self._format_batched_response(info, item)
            for info, item in zip(unpacked, response)
        ]


__all__ = ["ChainProviderRouter", "chain_providers_router"]
