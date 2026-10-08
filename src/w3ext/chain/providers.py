"""HTTP providers whose transport lifetime belongs to a router."""

import asyncio
from collections.abc import Callable, Mapping
from contextvars import Context
from typing import Any

from web3 import AsyncHTTPProvider


def _copy_options(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_options(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_options(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_options(item) for item in value)
    return value


def _freeze_options(value: Any) -> Any:
    if isinstance(value, Mapping):
        return (
            "mapping",
            frozenset(
                (_freeze_options(key), _freeze_options(item))
                for key, item in value.items()
            ),
        )
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(_freeze_options(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return (type(value), frozenset(_freeze_options(item) for item in value))
    try:
        hash(value)
    except TypeError:
        return (type(value), id(value))
    return (type(value), value)


class SharedAsyncHTTPProvider(AsyncHTTPProvider):
    """Keep the HTTP transport open on disconnect; its router calls close."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._closed = False
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._before_request: Callable[[], Any] | None = None

    async def _prepare_request(self) -> None:
        if self._closed:
            raise RuntimeError("Router provider is closed")
        loop = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = loop
        elif self._owner_loop is not loop:
            raise RuntimeError("Router provider belongs to a different event loop")
        if self._before_request is not None:
            await self._before_request()
        if self._closed:
            raise RuntimeError("Router provider is closed")

    async def make_request(self, method, params):
        await self._prepare_request()
        return await super().make_request(method, params)

    async def make_batch_request(self, batch_requests):
        await self._prepare_request()
        return await super().make_batch_request(batch_requests)

    async def disconnect(self) -> None:
        """Release a borrow without closing the router's shared transport."""

    async def close(self) -> None:
        """Close the owned transport and prevent retained references reopening it."""
        self._closed = True
        await super().disconnect()


class HTTPProviderPool:
    """Reuse providers by loop, chain, endpoint, and HTTP options.

    Constructing a pool or looking up a provider performs no network I/O.
    Explicit pools are closed by their router. The default router enables
    automatic per-loop cleanup during asyncio.run shutdown.
    """

    def __init__(
        self,
        *,
        auto_cleanup: bool = False,
        provider_factory: Callable[
            [int | str, str, dict[str, Any]], SharedAsyncHTTPProvider
        ]
        | None = None,
        on_loop_closed: Callable[[asyncio.AbstractEventLoop], None] | None = None,
    ) -> None:
        self._on_loop_closed = on_loop_closed
        self._providers: dict[tuple[Any, ...], SharedAsyncHTTPProvider] = {}
        self._retired_providers: dict[int, SharedAsyncHTTPProvider] = {}
        self._lifetimes: dict[
            asyncio.AbstractEventLoop, tuple[asyncio.Task[None], asyncio.Event]
        ] = {}
        self._auto_cleanup = auto_cleanup
        self._provider_factory = provider_factory
        self._closed = False

    def get_provider(
        self,
        chain_id: int | str,
        endpoint_uri: str,
        request_kwargs: dict[str, Any] | None = None,
    ) -> SharedAsyncHTTPProvider:
        if self._closed:
            raise RuntimeError("Router provider pool is closed")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        options = _copy_options(request_kwargs or {})
        key = (loop, int(chain_id), endpoint_uri, _freeze_options(options))
        provider = self._providers.get(key)
        if provider is None or provider._closed:
            if provider is not None:
                self._retired_providers[id(provider)] = provider
            provider = (
                self._provider_factory(chain_id, endpoint_uri, options)
                if self._provider_factory is not None
                else SharedAsyncHTTPProvider(endpoint_uri, request_kwargs=options)
            )
            if self._auto_cleanup:
                provider._before_request = self._ensure_lifetime
            self._providers[key] = provider
        return provider

    async def _ensure_lifetime(self) -> None:
        loop = asyncio.get_running_loop()
        lifetime = self._lifetimes.get(loop)
        if lifetime is None:
            ready = asyncio.Event()

            async def cleanup_at_shutdown() -> None:
                try:
                    ready.set()
                    await asyncio.Future()
                finally:
                    try:
                        await self._close_providers(loop)
                    finally:
                        self._lifetimes.pop(loop, None)
                        if self._on_loop_closed is not None:
                            self._on_loop_closed(loop)

            task = loop.create_task(
                cleanup_at_shutdown(),
                name="w3ext-default-provider-cleanup",
                context=Context(),
            )
            lifetime = (task, ready)
            self._lifetimes[loop] = lifetime
        # Cleanup's finally must be active before any HTTP session is opened.
        await lifetime[1].wait()

    async def _close_providers(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        providers = {
            id(provider): provider
            for key, provider in self._providers.items()
            if loop is None
            or provider._owner_loop is loop
            or (key[0] is loop and provider._owner_loop is None)
        }

        providers.update(
            {
                key: provider
                for key, provider in self._retired_providers.items()
                if loop is None or provider._owner_loop is loop
            }
        )

        async def close_provider(provider: SharedAsyncHTTPProvider) -> None:
            owner = provider._owner_loop
            current = asyncio.get_running_loop()
            if owner is not None and owner is not current and owner.is_running():
                await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(provider.close(), owner)
                )
            elif (
                owner is not None
                and owner is not current
                and any(
                    not session.closed
                    for _, session in provider._request_session_manager.session_cache.items()
                )
            ):
                raise RuntimeError(
                    "Close router sessions before their event loop stops"
                )
            else:
                await provider.close()

        results = await asyncio.gather(
            *(close_provider(provider) for provider in providers.values()),
            return_exceptions=True,
        )
        closed = {
            key
            for key, result in zip(providers, results)
            if not isinstance(result, BaseException)
        }
        self._providers = {
            key: provider
            for key, provider in self._providers.items()
            if id(provider) not in closed
        }
        for key in closed:
            self._retired_providers.pop(key, None)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def close_current_loop(self) -> None:
        """Close this loop's providers without making the pool unusable."""
        loop = asyncio.get_running_loop()
        lifetime = self._lifetimes.get(loop)
        if lifetime is not None:
            task = lifetime[0]
            if task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._close_providers(loop)

    async def close(self) -> None:
        """Close the pool. Call from the loop that uses its connections."""
        self._closed = True
        await self.close_current_loop()
        await self._close_providers()

    async def __aenter__(self):
        if self._closed:
            raise RuntimeError("Router provider pool is closed")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()
