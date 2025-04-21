# pylint: disable=no-name-in-module
import asyncio
import logging
import time
from contextvars import ContextVar
from functools import wraps
from typing import Callable, TYPE_CHECKING, Optional

from web3 import AsyncWeb3
from web3.method import Method, RPC_METHODS_UNSUPPORTED_DURING_BATCH


if TYPE_CHECKING:
    from .chain import Chain

logger = logging.getLogger(__name__)


_batch_request_processed = ContextVar[bool]('_batch_request_processed', default=False)


def _dummy_checker(self):
    pass


def to_batch_aware_method(chain: "Chain", method: Callable):
    @wraps(method)
    async def wrapper(*args, **kwargs):
        if chain._is_batching and not _batch_request_processed.get():
            # in case of batching, we need to collect all calls to batch.
            # instead of returning request data, return future
            token = _batch_request_processed.set(True)
            try:
                chain._web3.provider._is_batching = True
                return await chain._add_to_batch_request_info(
                    await method(*args, **kwargs)
                )
            finally:
                chain._web3.provider._is_batching = False
                _batch_request_processed.reset(token)

        return await method(*args, **kwargs)

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
        max_size: int = 20,
        max_wait: float = 0.1,
        timeout: Optional[float] = 60
    ) -> None:
        self._requests = []
        self._max_size = max_size
        self._max_wait = max_wait
        self._timeout = timeout
        self._web3 = web3

        self._batcher = None
        self._batch_started = None
        self._validator: asyncio.Task = None
        self._lock = asyncio.Lock()

    async def _add_request_info(self, request_info):
        (req := asyncio.Future()).set_result(request_info)
        async with self._lock:
            self._requests.append((req, fut := asyncio.Future()))
        if self._batch_started is None:
            self._batch_started = time.time()

        if self._timeout is not None:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        return await fut

    async def _validator_task(self):
        while True:
            await asyncio.sleep(0.1)
            await self._validate_batching()

    async def _validate_batching(self, cancel: bool = False):
        async with self._lock:
            # if self._batcher is not None:
            if (cancel
                    or (self._max_size and len(self._requests) >= self._max_size)
                    or (self._max_wait
                            and self._batch_started
                            and time.time() - self._batch_started >= self._max_wait)):
                await self._process_batch()
                self._batch_started = None

    async def __aenter__(self):
        self._validator = asyncio.create_task(self._validator_task())
        return self

    async def __aexit__(self, *args, **kwargs):
        if self._validator:
            self._validator.cancel()
        await self._validate_batching(cancel=True)

    async def _process_batch(self):
        semaphore = asyncio.Semaphore(3)
        async def process(requests, futures):
            # since we need standart web3 batching to generate requests info only
            # reset _is_batching flag to the value we expect to see, instead of True
            # to not break upgraded batching logic
            # batching = self._web3.provider._is_batching
            batcher = self._web3.batch_requests()
            # self._web3.provider._is_batching = batching
            batcher._validate_is_batching = _dummy_checker.__get__(batcher, batcher.__class__)

            for request in requests:
                batcher.add(request)

            try:
                # Execute batch
                async with semaphore:
                    responses = await batcher.async_execute()
                # Process results
                for future, response in zip(futures, responses):
                    if isinstance(response, Exception):
                        future.set_exception(response)
                    else:
                        future.set_result(response)
            except Exception as e:
                # If batch fails, fail all futures
                logger.exception(e)
                for future in futures:
                    if not future.done():
                        future.set_exception(e)

        tasks = []
        while len(self._requests):
            requests, futures = list(zip(*self._requests[:self._max_size]))
            self._requests = self._requests[len(requests):]
            tasks.append(process(requests, futures))

        await asyncio.gather(*tasks)