from contextvars import ContextVar
from web3.middleware import Web3Middleware

_middlewares_ctx_var = ContextVar("_middlewares_ctx_var", default={})


class DynamicContextMiddleware(Web3Middleware):
    def __init__(self, w3, chain_ref):
        super().__init__(w3)
        self._chain_ref = chain_ref

    async def async_wrap_make_request(self, make_request):
        async def middleware(method, params):
            store = _middlewares_ctx_var.get()
            # Get middlewares for this chain
            middlewares = store.get(id(self._chain_ref), [])

            handler = make_request

            # Apply in reverse (inner to outer)
            for m in reversed(middlewares):
                if isinstance(m, type):
                    # It's a class, assume v6 Web3Middleware
                    instance = m(self._w3)
                    if hasattr(instance, 'async_wrap_make_request'):
                        handler = await instance.async_wrap_make_request(handler)
                    elif hasattr(instance, 'wrap_make_request'):
                        handler = instance.wrap_make_request(handler)
                    else:
                        # Fallback for class-based factory
                        handler = m(handler, self._w3)
                else:
                    # It's a function/callable factory
                    handler = m(handler, self._w3)

            return await handler(method, params)
        return middleware
