"""Router-owned HTTP transports, exercised without outbound requests."""

import asyncio
import gc
import json
import weakref

import pytest
from web3 import AsyncHTTPProvider

from w3ext.chain.providers import HTTPProviderPool, SharedAsyncHTTPProvider


@pytest.fixture
def sessions(monkeypatch):
    captured = []

    async def request(provider, method, data):
        session = (
            await provider._request_session_manager.async_cache_and_return_session(
                provider.endpoint_uri
            )
        )
        if session not in captured:
            captured.append(session)
        payload = json.loads(data)
        return json.dumps(
            {"jsonrpc": "2.0", "id": payload["id"], "result": "0x2a"}
        ).encode()

    monkeypatch.setattr(AsyncHTTPProvider, "_make_request", request)
    return captured


def test_pool_reuses_matching_options_and_copies_nested_settings():
    pool = HTTPProviderPool()
    options = {"timeout": 7, "headers": {"Authorization": "initial"}}
    provider = pool.get_provider(1, "https://rpc.example", options)
    options["headers"]["Authorization"] = "modified"
    assert provider is pool.get_provider(
        "1",
        "https://rpc.example",
        {"headers": {"Authorization": "initial"}, "timeout": 7},
    )
    assert provider._request_kwargs["headers"] == {"Authorization": "initial"}
    assert provider is not pool.get_provider(2, "https://rpc.example", options)
    assert provider is not pool.get_provider(1, "https://other.example", options)
    assert provider is not pool.get_provider(1, "https://rpc.example", {"timeout": 8})
    asyncio.run(pool.close())


def test_shared_disconnect_preserves_session_until_owner_close(sessions):
    async def check():
        pool = HTTPProviderPool()
        provider = pool.get_provider(1, "https://rpc.example")
        try:
            responses = await asyncio.gather(
                *(provider.make_request("eth_blockNumber", []) for _ in range(8))
            )
            assert all(response["result"] == "0x2a" for response in responses)
            assert len(sessions) == 1
            await provider.disconnect()
            assert not sessions[0].closed
            await pool.close()
            assert sessions[0].closed
            with pytest.raises(RuntimeError, match="closed"):
                await provider.make_request("eth_blockNumber", [])
            with pytest.raises(RuntimeError, match="closed"):
                await provider.make_batch_request([("eth_blockNumber", [])])
            with pytest.raises(RuntimeError, match="closed"):
                pool.get_provider(1, "https://rpc.example")
        finally:
            await pool.close()
            for session in sessions:
                await session.close()

    asyncio.run(check())


def test_pool_keeps_transports_separate_across_event_loops(sessions):
    pool = HTTPProviderPool()
    providers = []

    async def check():
        provider = pool.get_provider(1, "https://rpc.example")
        providers.append(provider)
        try:
            await provider.make_request("eth_blockNumber", [])
        finally:
            await pool.close_current_loop()

    asyncio.run(check())
    asyncio.run(check())
    assert providers[0] is not providers[1]
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    asyncio.run(pool.close())


def test_retained_provider_rejects_another_event_loop():
    provider = SharedAsyncHTTPProvider("https://rpc.example")
    asyncio.run(provider._prepare_request())

    async def check():
        with pytest.raises(RuntimeError, match="different event loop"):
            await provider.make_request("eth_blockNumber", [])
        await provider.close()

    asyncio.run(check())


@pytest.mark.parametrize("auto_cleanup", [False, True])
def test_pool_close_attempts_every_transport_and_retries_failure(
    sessions, auto_cleanup
):
    class FailingProvider(SharedAsyncHTTPProvider):
        def __init__(self, endpoint, request_kwargs):
            super().__init__(endpoint, request_kwargs)
            self.fail_close = endpoint.endswith("first.example")
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            if self.fail_close:
                raise RuntimeError("transport close failed")
            await super().close()

    async def check():
        pool = HTTPProviderPool(
            auto_cleanup=auto_cleanup,
            provider_factory=lambda chain_id, endpoint, options: FailingProvider(
                endpoint, options
            ),
        )
        first = pool.get_provider(1, "https://first.example")
        second = pool.get_provider(1, "https://second.example")
        try:
            await first.make_request("eth_blockNumber", [])
            await second.make_request("eth_blockNumber", [])
            with pytest.raises(RuntimeError, match="transport close failed"):
                await pool.close()
            assert first.close_calls == second.close_calls == 1
            assert not sessions[0].closed
            assert sessions[1].closed
            first.fail_close = False
            await pool.close()
            assert first.close_calls == 2
            assert second.close_calls == 1
            assert all(session.closed for session in sessions)
        finally:
            first.fail_close = False
            await pool.close()
            for session in sessions:
                await session.close()

    asyncio.run(check())


def test_auto_cleanup_is_ready_before_opening_a_session(sessions):
    pool = HTTPProviderPool(auto_cleanup=True)
    providers = []

    async def check():
        provider = pool.get_provider(1, "https://rpc.example")
        providers.append(provider)
        await provider.make_request("eth_blockNumber", [])
        assert len(pool._lifetimes) == 1
        assert not sessions[0].closed

    asyncio.run(check())
    assert sessions[0].closed
    assert providers[0]._closed
    assert not pool._lifetimes
    assert not pool._providers
    asyncio.run(pool.close())


def test_pool_does_not_share_sessions_with_different_request_settings(sessions):
    async def check():
        pool = HTTPProviderPool()
        first = pool.get_provider(1, "https://rpc.example", {"timeout": 7})
        second = pool.get_provider(1, "https://rpc.example", {"timeout": 8})
        try:
            await asyncio.gather(
                first.make_request("eth_blockNumber", []),
                second.make_request("eth_blockNumber", []),
            )
            assert len(sessions) == 2
            assert sessions[0] is not sessions[1]
        finally:
            await pool.close()
            for session in sessions:
                await session.close()

    asyncio.run(check())


@pytest.mark.parametrize("retry_method", ["close", "close_current_loop"])
def test_failed_closed_provider_remains_owned_after_replacement(sessions, retry_method):
    class FailingProvider(SharedAsyncHTTPProvider):
        fail_close = False
        close_calls = 0

        async def close(self):
            self.close_calls += 1
            if self.fail_close:
                self._closed = True
                raise RuntimeError("transport close failed")
            await super().close()

    async def check():
        pool = HTTPProviderPool(
            auto_cleanup=True,
            provider_factory=lambda chain_id, endpoint, options: FailingProvider(
                endpoint, options
            ),
        )
        old = pool.get_provider(1, "https://rpc.example")
        old_ref = weakref.ref(old)
        try:
            old.fail_close = True
            await old.make_request("eth_blockNumber", [])
            with pytest.raises(RuntimeError, match="transport close failed"):
                await pool.close_current_loop()
            assert old._closed
            assert not sessions[0].closed
            assert not pool._lifetimes
            old.fail_close = False
            replacement = pool.get_provider(1, "https://rpc.example")
            assert replacement is not old
            del old
            gc.collect()
            assert old_ref() is not None
            await replacement.make_request("eth_blockNumber", [])
            assert len(sessions) == 2
            await getattr(pool, retry_method)()
            assert all(session.closed for session in sessions)
            assert replacement.close_calls == 1
            assert not pool._retired_providers
            assert not pool._providers
        finally:
            retained = old_ref()
            if retained is not None:
                retained.fail_close = False
            await pool.close()
            for session in sessions:
                await session.close()

    asyncio.run(check())
