import asyncio
from contextvars import ContextVar
from typing import Any

from web3.middleware import Web3Middleware
from web3.types import RPCEndpoint

_middlewares_ctx_var: ContextVar[dict[int, list] | None] = ContextVar(
    "_middlewares_ctx_var", default=None
)


class _ForwardedRequest(BaseException):
    def __init__(self, method, params):
        self.method = method
        self.params = params


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
        async def capture(next_method, next_params):
            raise _ForwardedRequest(next_method, next_params)

        handler = await self._wrap_active_middlewares(capture)
        try:
            await handler(method, params)
        except _ForwardedRequest as forwarded:
            return forwarded.method, forwarded.params
        raise RuntimeError("Context middleware did not forward the RPC request")

    async def async_wrap_make_batch_request(self, make_batch_request):
        async def middleware(requests):
            prepared = []
            running = []
            try:
                for method, params in requests:
                    forwarded = asyncio.get_running_loop().create_future()
                    response = asyncio.get_running_loop().create_future()

                    async def capture(
                        next_method,
                        next_params,
                        *,
                        _forwarded=forwarded,
                        _response=response,
                    ):
                        if _forwarded.done():
                            raise RuntimeError(
                                "Context middleware forwarded an RPC request twice"
                            )
                        _forwarded.set_result((next_method, next_params))
                        return await _response

                    handler = await self._wrap_active_middlewares(capture)
                    task = asyncio.create_task(handler(method, params))
                    running.append(task)
                    await asyncio.wait(
                        (forwarded, task), return_when=asyncio.FIRST_COMPLETED
                    )
                    if not forwarded.done():
                        # A cache middleware may complete this request locally.
                        await task
                        prepared.append((None, None, task))
                    else:
                        prepared.append((forwarded.result(), response, task))

                forwarded_requests = [
                    request for request, _, _ in prepared if request is not None
                ]
                if forwarded_requests:
                    actual_responses = await make_batch_request(forwarded_requests)
                    if not isinstance(actual_responses, list):
                        return actual_responses
                    if len(actual_responses) != len(forwarded_requests):
                        raise RuntimeError(
                            "Batch response count does not match forwarded requests"
                        )

                    actual = iter(actual_responses)
                    for request, response, _ in prepared:
                        if request is not None:
                            assert response is not None
                            response.set_result(next(actual))
                return await asyncio.gather(*(task for _, _, task in prepared))
            finally:
                pending = [task for task in running if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

        return middleware

    async def async_wrap_make_request(self, make_request):
        async def middleware(method, params):
            handler = await self._wrap_active_middlewares(make_request)
            return await handler(method, params)

        return middleware
