from contextvars import ContextVar
from typing import Any

from web3.middleware import Web3Middleware
from web3.types import RPCEndpoint

_middlewares_ctx_var: ContextVar[dict[int, list] | None] = ContextVar(
    "_middlewares_ctx_var", default=None
)


class DynamicContextMiddleware(Web3Middleware):
    def __init__(self, w3, chain_ref):
        super().__init__(w3)
        self._chain_ref = chain_ref

    async def _wrap_active_middlewares(self, make_request):
        store = _middlewares_ctx_var.get() or {}
        middlewares = store.get(id(self._chain_ref), [])
        handler = make_request

        for middleware in reversed(middlewares):
            if isinstance(middleware, type):
                instance = middleware(self._w3)
                if hasattr(instance, "async_wrap_make_request"):
                    handler = await instance.async_wrap_make_request(handler)
                elif hasattr(instance, "wrap_make_request"):
                    handler = instance.wrap_make_request(handler)
                else:
                    handler = middleware(handler, self._w3)
            else:
                handler = middleware(handler, self._w3)
        return handler

    async def async_request_processor(
        self, method: RPCEndpoint, params: Any
    ) -> tuple[RPCEndpoint, Any]:
        # Persistent providers call request processors instead of request wrappers.
        forwarded = None

        async def capture(next_method, next_params):
            nonlocal forwarded
            forwarded = (next_method, next_params)
            return {"jsonrpc": "2.0", "id": 0, "result": None}

        handler = await self._wrap_active_middlewares(capture)
        await handler(method, params)
        if forwarded is None:
            raise RuntimeError("Context middleware did not forward the RPC request")
        return forwarded

    async def async_wrap_make_request(self, make_request):
        async def middleware(method, params):
            handler = await self._wrap_active_middlewares(make_request)
            return await handler(method, params)

        return middleware
