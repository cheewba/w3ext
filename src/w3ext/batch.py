# pylint: disable=no-name-in-module
import asyncio
import time
from contextvars import ContextVar
from functools import wraps
from typing import Callable, TYPE_CHECKING

from web3 import AsyncWeb3
from web3.method import Method, RPC_METHODS_UNSUPPORTED_DURING_BATCH


if TYPE_CHECKING:
    from .chain import Chain


_batch_request_processed = ContextVar[bool]('_batch_request_processed', default=False)


def to_batch_aware_method(chain: "Chain", method: Callable):
    @wraps(method)
    async def wrapper(*args, **kwargs):
        result = await method(*args, **kwargs)
        if chain._is_batching and not _batch_request_processed.get():
            # in case of batching, we need to collect all calls to batch.
            # instead of returning request data, return future
            _batch_request_processed.set(True)
            try:
                return await chain._add_to_batch_request_info(result)
            finally:
                _batch_request_processed.set(False)
        return result
    return wrapper


def is_batch_method(instance, attrname):
    def hasspecialmethod(obj, name):
        return any(name in klass.__dict__ for klass in type(obj).__mro__)
    for klass in type(instance).__mro__:
        if attrname in klass.__dict__:
            descriptor = klass.__dict__[attrname]
            if not (hasspecialmethod(descriptor, '__get__') or
                    hasspecialmethod(descriptor, '__set__') or
                    hasspecialmethod(descriptor, '__delete__')):
                # Attribute isn't a descriptor
                return False
            if (attrname in instance.__dict__ and
                not hasspecialmethod(descriptor, '__set__') and
                not hasspecialmethod(descriptor, '__delete__')):
                # Would be handled by the descriptor, but the descriptor isn't
                # a data descriptor and the object has a dict entry overriding
                # it.
                return False
            return (
                isinstance(descriptor, Method)
                and descriptor.json_rpc_method not in RPC_METHODS_UNSUPPORTED_DURING_BATCH
                # for now skip properties
                and not descriptor.is_property
            )
    return False


class Batch:
    def __init__(
        self,
        web3: AsyncWeb3,
        *,
        max_size: int = None,
        max_wait: float = None
    ) -> None:
        self._futures = []
        self._max_size = max_size
        self._max_wait = max_wait
        self._web3 = web3

        self._batcher = None
        self._batch_started = None
        self._validator: asyncio.Task = None
        self._lock = asyncio.Lock()

    async def _add_request_info(self, request_info):
        async def wrapper():
            return request_info

        (req := asyncio.Future()).set_result(request_info)
        self._batcher.add(req)
        self._futures.append(fut := asyncio.Future())
        if self._batch_started is None:
            self._batch_started = time.time()
        return await fut

    async def _validator_task(self):
        while True:
            await asyncio.sleep(0.1)
            await self._validate_batching()

    async def _validate_batching(self, cancel: bool = False):
        if self._batcher is not None:
            if (cancel
                    or (self._max_size and len(self._futures) >= self._max_size)
                    or (self._max_wait
                            and self._batch_started
                            and time.time() - self._batch_started >= self._max_wait)):
                await self._process_batch()
                await self._batcher.__aexit__(None, None, None)

                self._batcher = None
                self._batch_started = None

        if not cancel and self._batcher is None:
            self._batcher = self._web3.batch_requests()
            await self._batcher.__aenter__()

    async def __aenter__(self):
        await self._validate_batching()
        self._validator = asyncio.create_task(self._validator_task())
        return self

    async def __aexit__(self, *args, **kwargs):
        if self._validator:
            self._validator.cancel()
        await self._validate_batching(cancel=True)

    async def _process_batch(self):
        if not len(self._futures):
            return

        try:
            # Execute batch
            responses = await self._batcher.async_execute()

            # Process results
            for future, response in zip(self._futures, responses):
                if isinstance(response, Exception):
                    future.set_exception(response)
                else:
                    future.set_result(response)

        except Exception as e:
            # If batch fails, fail all futures
            for future in self._futures:
                if not future.done():
                    future.set_exception(e)

        self._futures = []