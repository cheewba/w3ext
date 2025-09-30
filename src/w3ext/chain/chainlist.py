# pylint: disable=missing-class-docstring,missing-function-docstring
"""
Chainlist-based RPC provider and cached client.

- ChainlistClient: fetches https://chainlist.org/rpcs.json once and caches it in-memory.
  Provides helpers to retrieve the first RPC URL for a given chain_id and EIP-3091 explorer URLs.
- ChainlistAsyncHTTPProvider: AsyncHTTPProvider that lazily resolves the HTTP RPC endpoint
  from Chainlist on the first request if no RPC has been set explicitly. It uses the first
  HTTP(S) RPC for the provided chain_id.

Usage example:
    from w3ext.chain.chainlist import get_chain_provider
    provider = await get_chain_provider(1)  # Ethereum mainnet

    # Or fetch explorer base URL:
    from w3ext.chain.chainlist import get_chain_explorer
    base = await get_chain_explorer(1)

Notes:
- Only HTTP(S) endpoints are used for RPC resolution (ws/wss are skipped).
- A default timeout of 30 seconds is applied to the underlying AsyncHTTPProvider unless overridden.
"""
import asyncio
import time
from typing import Any, Dict, List, Optional, Union, Set, Callable, Awaitable

import aiohttp
from web3 import AsyncHTTPProvider

from ..exceptions import ChainException


CHAINLIST_RPCS_URL = "https://chainlist.org/rpcs.json"


class ChainlistClient:
    def __init__(self) -> None:
        self._data: Optional[List[Dict[str, Any]]] = None
        # In-memory cache for Chainlist data; stales after 60 seconds
        self._expires_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()

    async def _fetch_data(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(CHAINLIST_RPCS_URL) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def get_data(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if self._data is None or now >= self._expires_at:
            async with self._lock:
                if self._data is None or now >= self._expires_at:
                    self._data = await self._fetch_data()
                    self._expires_at = time.monotonic() + 60.0  # 60s TTL
        return self._data

    async def _get_first_http_rpc(self, chain_id: Union[int, str]) -> Optional[str]:
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
                if url.startswith("http://") or url.startswith("https://"):
                    return url
            return None
        return None

    async def _get_http_rpcs(self, chain_id: Union[int, str]) -> List[str]:
        # Collects all HTTP(S) RPC endpoints for the given chain_id in Chainlist order
        cid = int(chain_id)
        urls: List[str] = []
        data = await self.get_data()
        for item in data:
            if int(item.get("chainId", -1)) != cid:
                continue
            for rpc_entry in item.get("rpc", []):
                url = rpc_entry if isinstance(rpc_entry, str) else rpc_entry.get("url")
                if isinstance(url, str) and (url.startswith("http://") or url.startswith("https://")):
                    urls.append(url)
            break
        return urls

    async def _get_eip3091_explorer_base(self, chain_id: Union[int, str]) -> Optional[str]:
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
        chain_id: Union[int, str],
        request_kwargs: Optional[Dict[str, Any]] = None,
    ) -> "ChainlistAsyncHTTPProvider":
        return ChainlistAsyncHTTPProvider(self, chain_id, request_kwargs)

    async def get_chain_explorer(self, chain_id: Union[int, str]) -> Optional[str]:
        return await self._get_eip3091_explorer_base(chain_id)


class ChainlistAsyncHTTPProvider(AsyncHTTPProvider):
    """
    Async provider that resolves its HTTP endpoint from Chainlist on first use.

    - Provide chain_id at construction.
    - Optionally pass request_kwargs (e.g. {'timeout': 30}) for underlying AsyncHTTPProvider.
    """

    def __init__(
        self,
        client: ChainlistClient,
        chain_id: Union[int, str],
        request_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Store resolution context; endpoint is chosen per-request
        self._client = client
        self._chain_id = int(chain_id)
        self._request_kwargs = dict(request_kwargs or {})
        self._ensure_lock = asyncio.Lock()
        self._resolved = False
        self._current_rpc: Optional[str] = None
        # Default timeout unless overridden
        self._request_kwargs.setdefault("timeout", 30)
        # Initialize parent with placeholder; will switch before each request attempt
        super().__init__("http://localhost", self._request_kwargs)
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

    async def _pick_rpc(self, failed: Set[str]) -> Optional[str]:
        # Picks an HTTP(S) RPC not in the failed set; resets when all exhausted
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
        call: Callable[[], Awaitable[Any]],
        is_error: Callable[[Any], bool],
        max_attempts: int = 3,
    ) -> Any:
        # Tries up to max_attempts, rotating RPC endpoint only on errors/exceptions
        failed: Set[str] = set()
        last_exc: Optional[BaseException] = None
        last_resp: Any = None
        for attempt in range(max_attempts):
            if attempt == 0 and self._current_rpc and self._current_rpc not in failed:
                rpc = self._current_rpc
            else:
                rpc = await self._pick_rpc(failed)
            if not rpc:
                break
            self.endpoint_uri = rpc
            try:
                resp = await call()
                if is_error(resp):
                    last_resp = resp
                    failed.add(rpc)
                    if rpc == self._current_rpc:
                        self._current_rpc = None
                    continue
                # success path: stick to this rpc for subsequent calls
                self._current_rpc = rpc
                return resp
            except Exception as exc:
                last_exc = exc
                failed.add(rpc)
                if rpc == self._current_rpc:
                    self._current_rpc = None
                continue
        if last_exc is not None:
            raise last_exc
        if last_resp is not None:
            return last_resp
        raise ChainException(f"No HTTP RPC found on Chainlist for chain_id={self._chain_id}")

    async def make_request(self, method: str, params: Any) -> Any:
        await self._ensure_endpoint()

        def is_error(resp: Any) -> bool:
            return isinstance(resp, dict) and "error" in resp

        # Capture base method here to avoid zero-arg super() inside lambda
        base_make_request = super(ChainlistAsyncHTTPProvider, self).make_request

        return await self._perform_with_rotation(
            lambda: base_make_request(method, params),
            is_error,
            max_attempts=3,
        )

    async def make_batch_request(self, batch_requests):
        await self._ensure_endpoint()

        def is_error(resp: Any) -> bool:
            if isinstance(resp, dict):
                return "error" in resp
            if isinstance(resp, list):
                return any(isinstance(r, dict) and "error" in r for r in resp)
            return False

        # Capture base method here to avoid zero-arg super() inside lambda
        base_make_batch = super(ChainlistAsyncHTTPProvider, self).make_batch_request

        return await self._perform_with_rotation(
            lambda: base_make_batch(batch_requests),
            is_error,
            max_attempts=3,
        )



# Module-level singleton client instance and exported helpers
_client = ChainlistClient()


def get_chain_provider(
    chain_id: Union[int, str],
    request_kwargs: Optional[Dict[str, Any]] = None,
) -> ChainlistAsyncHTTPProvider:
    return _client.get_chain_provider(chain_id, request_kwargs)


async def get_chain_explorer(chain_id: Union[int, str]) -> Optional[str]:
    return await _client.get_chain_explorer(chain_id)


__all__ = [
    "ChainlistClient",
    "ChainlistAsyncHTTPProvider",
    "get_chain_provider",
    "get_chain_explorer",
]