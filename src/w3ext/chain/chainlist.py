# pylint: disable=missing-class-docstring,missing-function-docstring
"""
Chainlist-based RPC provider and cached client.

- ChainlistClient: fetches https://chainlist.org/rpcs.json once and caches it in-memory.
  Provides helpers to retrieve the first RPC URL for a given chain_id and EIP-3091 explorer URLs.
- ChainlistAsyncHTTPProvider: shared HTTP provider that lazily resolves its endpoint
  from Chainlist on the first request if no RPC has been set explicitly. It uses the first
  HTTP(S) RPC for the provided chain_id.

Usage example:
    from w3ext.chain.chainlist import get_chain_provider
    provider = get_chain_provider(1)  # Ethereum mainnet

    # Or fetch explorer base URL:
    from w3ext.chain.chainlist import get_chain_explorer
    base = await get_chain_explorer(1)

Notes:
- Only HTTP(S) endpoints are used for RPC resolution (ws/wss are skipped).
- A default timeout of 30 seconds is applied to the underlying AsyncHTTPProvider unless overridden.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import aiohttp
from web3.types import RPCEndpoint

from ..exceptions import ChainException
from .providers import HTTPProviderPool, SharedAsyncHTTPProvider

CHAINLIST_RPCS_URL = "https://chainlist.org/rpcs.json"


class ChainlistClient:
    def __init__(self) -> None:
        self._data: list[dict[str, Any]] | None = None
        # In-memory cache for Chainlist data; stales after 60 seconds
        self._expires_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()

    async def _fetch_data(self) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=60)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(CHAINLIST_RPCS_URL) as resp,
        ):
            resp.raise_for_status()
            return await resp.json()

    async def get_data(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._data is None or now >= self._expires_at:
            async with self._lock:
                if self._data is None or now >= self._expires_at:
                    self._data = await self._fetch_data()
                    self._expires_at = time.monotonic() + 60.0  # 60s TTL
        return self._data

    async def _get_first_http_rpc(self, chain_id: int | str) -> str | None:
        # Returns first HTTP(S) RPC for given chain_id
        cid = int(chain_id)
        data = await self.get_data()
        for item in data:
            if int(item.get("chainId", -1)) != cid:
                continue
            rpcs = item.get("rpc", [])
            for rpc_entry in rpcs:
                url = rpc_entry if isinstance(rpc_entry, str) else rpc_entry.get("url")
                if not isinstance(url, str):
                    continue
                if url.startswith(("http://", "https://")):
                    return url
            return None
        return None

    async def _get_http_rpcs(self, chain_id: int | str) -> list[str]:
        # Collects all HTTP(S) RPC endpoints for the given chain_id in Chainlist order
        cid = int(chain_id)
        urls: list[str] = []
        data = await self.get_data()
        for item in data:
            if int(item.get("chainId", -1)) != cid:
                continue
            for rpc_entry in item.get("rpc", []):
                url = rpc_entry if isinstance(rpc_entry, str) else rpc_entry.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    urls.append(url)
            break
        return urls

    async def _get_eip3091_explorer_base(self, chain_id: int | str) -> str | None:
        cid = int(chain_id)
        data = await self.get_data()
        for item in data:
            if int(item.get("chainId", -1)) != cid:
                continue
            for explorer in item.get("explorers", []) or []:
                standard = (explorer.get("standard") or "").upper()
                url = explorer.get("url")
                if standard == "EIP3091" and isinstance(url, str) and url:
                    return url.rstrip("/")
            return None
        return None

    def get_chain_provider(
        self,
        chain_id: int | str,
        request_kwargs: dict[str, Any] | None = None,
    ) -> "ChainlistAsyncHTTPProvider":
        return ChainlistAsyncHTTPProvider(self, chain_id, request_kwargs)

    async def get_chain_explorer(self, chain_id: int | str) -> str | None:
        return await self._get_eip3091_explorer_base(chain_id)


class ChainlistAsyncHTTPProvider(SharedAsyncHTTPProvider):
    """
    Async provider that resolves its HTTP endpoint from Chainlist on first use.

    - Provide chain_id at construction.
    - Optionally pass request_kwargs (e.g. {'timeout': 30}) for underlying AsyncHTTPProvider.
    """

    def __init__(
        self,
        client: ChainlistClient | None,
        chain_id: int | str,
        request_kwargs: dict[str, Any] | None = None,
        *,
        provider_pool: HTTPProviderPool | None = None,
        client_factory: Callable[[], ChainlistClient] | None = None,
    ) -> None:
        self._provider_pool = provider_pool or HTTPProviderPool()
        self._owns_pool = provider_pool is None
        # Store resolution context; endpoint is chosen per-request
        self._client = client
        self._client_factory = client_factory
        self._chain_id = int(chain_id)
        self._request_kwargs = dict(request_kwargs or {})
        self._ensure_lock = asyncio.Lock()
        self._resolved = False
        self._current_rpc: str | None = None
        # Default timeout unless overridden
        self._request_kwargs.setdefault("timeout", 30)
        # Initialize parent with placeholder; will switch before each request attempt
        super().__init__(
            "http://localhost",
            self._request_kwargs,
            cache_allowed_requests=True,
            cacheable_requests={"eth_chainId"},
            request_cache_validation_threshold=60 * 60,
        )
        # Disable built-in endpoint retry; rotation is handled here
        self._exception_retry_configuration = None

    async def _ensure_endpoint(self) -> None:
        # Marks provider as ready; actual endpoint selection happens per attempt
        if self._resolved:
            return
        async with self._ensure_lock:
            if self._resolved:
                return
            self._resolved = True

    async def _pick_rpc(self, failed: set[str]) -> str | None:
        # Picks an HTTP(S) RPC not in the failed set; resets when all exhausted
        assert self._client is not None
        urls = await self._client._get_http_rpcs(self._chain_id)
        if not urls:
            return None
        for url in urls:
            if url not in failed:
                return url
        failed.clear()
        return urls[0]

    async def _perform_with_rotation(
        self,
        call: Callable[[SharedAsyncHTTPProvider], Awaitable[Any]],
        is_error: Callable[[Any], bool],
        max_attempts: int = 3,
    ) -> Any:
        # Tries up to max_attempts, rotating RPC endpoint only on errors/exceptions
        failed: set[str] = set()
        last_exc: BaseException | None = None
        last_resp: Any = None
        for attempt in range(max_attempts):
            if attempt == 0 and self._current_rpc and self._current_rpc not in failed:
                rpc = self._current_rpc
            else:
                rpc = await self._pick_rpc(failed)
            if not rpc:
                break
            provider = self._provider_pool.get_provider(
                self._chain_id, rpc, self._request_kwargs
            )
            try:
                resp = await call(provider)
                if is_error(resp):
                    last_resp = resp
                    failed.add(rpc)
                    if rpc == self._current_rpc:
                        self._current_rpc = None
                    continue
                # success path: stick to this rpc for subsequent calls
                self._current_rpc = rpc
                self.endpoint_uri = rpc
                return resp
            except Exception as exc:  # noqa: BLE001 - retry after any RPC failure
                last_exc = exc
                failed.add(rpc)
                if rpc == self._current_rpc:
                    self._current_rpc = None
                continue
        if last_exc is not None:
            raise last_exc
        if last_resp is not None:
            return last_resp
        raise ChainException(
            f"No HTTP RPC found on Chainlist for chain_id={self._chain_id}"
        )

    async def make_request(self, method: str, params: Any) -> Any:
        await self._prepare_request()
        if self._client_factory is not None:
            self._client = self._client_factory()
        await self._ensure_endpoint()

        def is_error(resp: Any) -> bool:
            return isinstance(resp, dict) and "error" in resp

        return await self._perform_with_rotation(
            lambda provider: provider.make_request(RPCEndpoint(method), params),
            is_error,
            max_attempts=3,
        )

    async def make_batch_request(self, batch_requests):
        await self._prepare_request()
        if self._client_factory is not None:
            self._client = self._client_factory()
        await self._ensure_endpoint()

        def is_error(resp: Any) -> bool:
            if isinstance(resp, dict):
                return "error" in resp
            if isinstance(resp, list):
                return any(isinstance(r, dict) and "error" in r for r in resp)
            return False

        return await self._perform_with_rotation(
            lambda provider: provider.make_batch_request(batch_requests),
            is_error,
            max_attempts=3,
        )

    async def disconnect(self) -> None:
        # Standalone providers own their pool; router-provided wrappers borrow it.
        if self._owns_pool:
            await self.close()

    async def close(self) -> None:
        await super().close()
        if self._owns_pool:
            await self._provider_pool.close()


class ChainlistRouter:
    """Own pooled Chainlist providers and their HTTP transports."""

    def __init__(self, *, auto_cleanup: bool = False) -> None:
        self._auto_cleanup = auto_cleanup
        self._clients: dict[asyncio.AbstractEventLoop | None, ChainlistClient] = {}
        self._transports = HTTPProviderPool(
            auto_cleanup=auto_cleanup,
            provider_factory=lambda chain_id, endpoint_uri, request_kwargs: (
                SharedAsyncHTTPProvider(
                    endpoint_uri,
                    request_kwargs,
                    exception_retry_configuration=None,
                    cache_allowed_requests=True,
                    cacheable_requests={"eth_chainId"},
                    request_cache_validation_threshold=60 * 60,
                )
            ),
        )
        self._providers = HTTPProviderPool(
            auto_cleanup=auto_cleanup,
            provider_factory=self._make_provider,
            on_loop_closed=self._forget_client,
        )

    def _forget_client(self, loop: asyncio.AbstractEventLoop) -> None:
        self._clients.pop(loop, None)

    def _get_client(self) -> ChainlistClient:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop not in self._clients:
            self._clients[loop] = ChainlistClient()
        return self._clients[loop]

    def _make_provider(self, chain_id, endpoint_uri, request_kwargs):
        return ChainlistAsyncHTTPProvider(
            None,
            chain_id,
            request_kwargs,
            provider_pool=self._transports,
            client_factory=self._get_client,
        )

    def get_chain_provider(
        self, chain_id: int | str, request_kwargs: dict[str, Any] | None = None
    ) -> ChainlistAsyncHTTPProvider:
        return cast(
            ChainlistAsyncHTTPProvider,
            self._providers.get_provider(chain_id, "http://localhost", request_kwargs),
        )

    async def get_chain_explorer(self, chain_id: int | str) -> str | None:
        if self._auto_cleanup:
            await self._providers._ensure_lifetime()
        return await self._get_client().get_chain_explorer(chain_id)

    async def close_current_loop(self) -> None:
        """Close default-router resources before stopping a manually driven loop."""
        await self._providers.close_current_loop()
        await self._transports.close_current_loop()
        self._clients.pop(asyncio.get_running_loop(), None)

    async def close(self) -> None:
        try:
            await self._providers.close()
        finally:
            await self._transports.close()
        self._clients.clear()

    async def __aenter__(self):
        await self._providers.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()


default_chainlist_router = ChainlistRouter(auto_cleanup=True)


def get_chain_provider(
    chain_id: int | str,
    request_kwargs: dict[str, Any] | None = None,
) -> ChainlistAsyncHTTPProvider:
    return default_chainlist_router.get_chain_provider(chain_id, request_kwargs)


async def get_chain_explorer(chain_id: int | str) -> str | None:
    return await default_chainlist_router.get_chain_explorer(chain_id)


async def close_default_chainlist_router() -> None:
    """Close the current event loop's default RPC pool before loop shutdown."""
    try:
        await default_chainlist_router.close_current_loop()
    finally:
        from .routers import _clear_router_provider_cache

        _clear_router_provider_cache(default_chainlist_router)


__all__ = [
    "ChainlistAsyncHTTPProvider",
    "ChainlistClient",
    "ChainlistRouter",
    "close_default_chainlist_router",
    "default_chainlist_router",
    "get_chain_explorer",
    "get_chain_provider",
]
