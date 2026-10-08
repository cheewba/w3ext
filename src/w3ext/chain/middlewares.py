import asyncio
from contextvars import ContextVar
from typing import Any

from web3.middleware import Web3Middleware
from web3.providers.persistent import PersistentConnectionProvider
from web3.types import RPCEndpoint

_middlewares_ctx_var: ContextVar[dict[int, list] | None] = ContextVar(
    "_middlewares_ctx_var", default=None
)


class _ForwardedRequest(BaseException):
    def __init__(self, method, params):
        self.method = method
        self.params = params


class _ShortCircuitResponse(BaseException):
    def __init__(self, response):
        self.response = response


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
        if self._chain_ref._routing_provider._bypass_context_request_processor.get():
            return method, params

        # Persistent providers call request processors instead of request wrappers.
        async def capture(next_method, next_params):
            raise _ForwardedRequest(next_method, next_params)

        handler = await self._wrap_active_middlewares(capture)
        try:
            response = await handler(method, params)
        except _ForwardedRequest as forwarded:
            return forwarded.method, forwarded.params
        raise _ShortCircuitResponse(response)

    async def async_wrap_make_batch_request(self, make_batch_request):
        async def middleware(requests):
            outgoing = asyncio.Queue()
            running = []
            forwarded = [False] * len(requests)
            try:
                for index, (method, params) in enumerate(requests):

                    async def capture(next_method, next_params, *, _index=index):
                        forwarded[_index] = True
                        response = asyncio.get_running_loop().create_future()
                        await outgoing.put((next_method, next_params, response))
                        return await response

                    handler = await self._wrap_active_middlewares(capture)
                    running.append(asyncio.create_task(handler(method, params)))

                # Let immediately ready handlers form one wire batch. If another
                # handler is blocked by a semaphore held across make_request, send
                # the ready requests so it can release that semaphore.
                await asyncio.sleep(0)
                while not outgoing.empty() or any(not task.done() for task in running):
                    for task in running:
                        if task.done() and not task.cancelled():
                            error = task.exception()
                            if error is not None:
                                raise error

                    ready = []
                    if outgoing.empty():
                        getter = asyncio.create_task(outgoing.get())
                        pending = [task for task in running if not task.done()]
                        done, _ = await asyncio.wait(
                            (getter, *pending), return_when=asyncio.FIRST_COMPLETED
                        )
                        if getter in done:
                            ready.append(getter.result())
                        else:
                            getter.cancel()
                            await asyncio.gather(getter, return_exceptions=True)
                    while not outgoing.empty():
                        ready.append(outgoing.get_nowait())
                    if not ready:
                        continue

                    actual_responses = await make_batch_request(
                        [(method, params) for method, params, _ in ready]
                    )
                    if not isinstance(actual_responses, list):
                        return actual_responses
                    if len(actual_responses) != len(ready):
                        raise RuntimeError(
                            "Batch response count does not match forwarded requests"
                        )
                    for (_, _, response), actual in zip(ready, actual_responses):
                        response.set_result(actual)
                    await asyncio.sleep(0)

                results = list(await asyncio.gather(*running))
                if isinstance(self._w3.provider, PersistentConnectionProvider):
                    middleware_stack = (
                        self._w3.middleware_onion.as_tuple_of_middleware()
                    )
                    position = next(
                        (
                            index
                            for index, item in enumerate(middleware_stack)
                            if item is type(self)
                        ),
                        len(middleware_stack),
                    )
                    for index, ((method, _), was_forwarded) in enumerate(
                        zip(requests, forwarded)
                    ):
                        if not was_forwarded:
                            response = results[index]
                            for item in reversed(middleware_stack[position + 1 :]):
                                response = await item(
                                    self._w3
                                ).async_response_processor(method, response)
                            results[index] = response
                return results
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
