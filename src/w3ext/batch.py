"""
Batch processing module for w3ext.

This module provides efficient batch processing of blockchain RPC calls to improve
performance when making multiple requests. It automatically groups requests and
executes them in batches to reduce network overhead and improve throughput.

The batching system works by:
1. Intercepting individual RPC calls when batching is enabled
2. Collecting calls until batch size or time limits are reached
3. Executing all collected calls in a single batch request
4. Distributing results back to the original callers

Classes:
    Batch: Main batch processor with configurable size and timing limits

Functions:
    to_batch_aware_method: Decorator to make methods batch-aware
    is_batch_method: Check if a method supports batching

Example:
    >>> async with chain.use_batch() as batch:
    ...     # These calls will be batched together
    ...     balance1 = await token1.get_balance(address1)
    ...     balance2 = await token2.get_balance(address2)
    ...     balance3 = await token3.get_balance(address3)
    >>> # Batch executed here, all results available
"""

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
    """Dummy validation function to bypass web3's batch validation."""
    pass


def to_batch_aware_method(chain: "Chain", method: Callable):
    """
    Decorator to make a method batch-aware.

    When batching is enabled on the chain, this decorator intercepts method calls
    and adds them to the batch queue instead of executing immediately. The method
    returns a future that will be resolved when the batch is executed.

    Args:
        chain: Chain instance that manages batching state
        method: Method to make batch-aware

    Returns:
        Wrapped method that supports batching

    Example:
        >>> @to_batch_aware_method(chain, original_method)
        >>> async def get_balance(address):
        ...     # This will be batched when chain._is_batching is True
        ...     return await original_method(address)
    """
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
    """
    Check if a method/attribute supports batch processing.

    Determines whether a given attribute on an instance is a Web3 method
    that can be safely batched. This excludes properties, unsupported RPC
    methods, and non-descriptor attributes.

    Args:
        instance: Object instance to check
        attrname: Name of the attribute to check

    Returns:
        True if the attribute supports batching, False otherwise

    Example:
        >>> web3_instance = AsyncWeb3(...)
        >>> is_batch_method(web3_instance.eth, 'get_balance')  # True
        >>> is_batch_method(web3_instance.eth, 'accounts')     # False (property)
    """
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
    """
    Batch processor for efficient RPC request handling.

    This class manages the collection and execution of multiple RPC requests
    in batches to improve performance. It automatically triggers batch execution
    when size or time limits are reached.

    Attributes:
        _requests: List of pending request/future pairs
        _max_size: Maximum number of requests per batch
        _max_wait: Maximum time to wait before executing batch (seconds)
        _timeout: Timeout for individual request completion
        _web3: AsyncWeb3 instance for executing requests
        _batch_started: Timestamp when current batch was started
        _validator: Background task for time-based batch validation
        _lock: Async lock for thread-safe request management

    Example:
        >>> batch = Batch(web3, max_size=50, max_wait=0.2)
        >>> async with batch:
        ...     # Add requests to batch
        ...     result1 = await batch._add_request_info(request1)
        ...     result2 = await batch._add_request_info(request2)
        >>> # Batch automatically executed on exit
    """

    def __init__(
        self,
        web3: AsyncWeb3,
        *,
        max_size: int = 20,
        max_wait: float = 0.1,
        timeout: Optional[float] = 60
    ) -> None:
        """
        Initialize a new Batch processor.

        Args:
            web3: AsyncWeb3 instance for executing requests
            max_size: Maximum requests per batch (default: 20)
            max_wait: Maximum wait time in seconds before executing batch (default: 0.1)
            timeout: Timeout for individual request completion in seconds (default: 60)
        """
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
        """
        Add a request to the batch queue.

        Creates a future for the request result and adds it to the batch queue.
        If this is the first request, starts the batch timer. Returns a future
        that will be resolved when the batch is executed.

        Args:
            request_info: RPC request information to add to batch

        Returns:
            Future that will contain the request result

        Raises:
            asyncio.TimeoutError: If request times out
        """
        (req := asyncio.Future()).set_result(request_info)
        async with self._lock:
            self._requests.append((req, fut := asyncio.Future()))
        if self._batch_started is None:
            self._batch_started = time.time()

        if self._timeout is not None:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        return await fut

    async def _validator_task(self):
        """
        Background task that periodically checks if batch should be executed.

        Runs continuously while the batch context is active, checking every
        100ms if the batch should be executed based on time limits.
        """
        while True:
            await asyncio.sleep(0.1)
            await self._validate_batching()

    async def _validate_batching(self, cancel: bool = False):
        """
        Check if batch should be executed and execute if needed.

        Executes the batch if any of these conditions are met:
        - cancel=True (forced execution)
        - Batch size reaches max_size limit
        - Batch age reaches max_wait time limit

        Args:
            cancel: If True, force batch execution regardless of limits
        """
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
        """
        Enter batch context and start background validator task.

        Returns:
            Self for use in async with statement
        """
        self._validator = asyncio.create_task(self._validator_task())
        return self

    async def __aexit__(self, *args, **kwargs):
        """
        Exit batch context, cancel validator task, and execute remaining requests.

        Ensures all pending requests are executed before exiting the context.
        """
        if self._validator:
            self._validator.cancel()
        await self._validate_batching(cancel=True)

    async def _process_batch(self):
        """
        Execute all pending requests in batches.

        Groups pending requests into batches of max_size and executes them
        concurrently. Uses a semaphore to limit concurrent batch execution
        and properly handles both successful responses and exceptions.

        The method:
        1. Groups requests into batches of max_size
        2. Creates Web3 batch requests for each group
        3. Executes batches concurrently (max 3 concurrent batches)
        4. Distributes results/exceptions back to original futures

        Note:
            Uses a dummy validator to bypass Web3's internal batching checks
            since we manage batching state externally.
        """
        semaphore = asyncio.Semaphore(3)

        async def process(requests, futures):
            """
            Process a single batch of requests.

            Args:
                requests: List of request futures containing request info
                futures: List of result futures to resolve with responses
            """
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

        # Process all requests in batches
        tasks = []
        while len(self._requests):
            requests, futures = list(zip(*self._requests[:self._max_size]))
            self._requests = self._requests[len(requests):]
            tasks.append(process(requests, futures))

        await asyncio.gather(*tasks)