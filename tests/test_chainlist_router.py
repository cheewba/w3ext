"""Pooled Chainlist routing and shutdown with real offline HTTP sessions."""

import asyncio
import json

import pytest
from web3 import AsyncHTTPProvider

from w3ext import (
    Chain,
    ChainlistRouter,
    chain_providers_router,
    close_default_chainlist_router,
    default_chainlist_router,
)
from w3ext.chain.chainlist import (
    ChainlistAsyncHTTPProvider,
    ChainlistClient,
    get_chain_explorer,
)


@pytest.fixture
def offline_rpc(monkeypatch):
    sessions = []

    async def catalog(client, chain_id):
        return ["https://first.example", "https://second.example"]

    async def request(provider, method, data):
        session = (
            await provider._request_session_manager.async_cache_and_return_session(
                provider.endpoint_uri
            )
        )
        if session not in sessions:
            sessions.append(session)
        payload = json.loads(data)
        result = "0x1" if method == "eth_chainId" else "0x2a"
        return json.dumps(
            {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        ).encode()

    monkeypatch.setattr(ChainlistClient, "_get_http_rpcs", catalog)
    monkeypatch.setattr(AsyncHTTPProvider, "_make_request", request)
    return sessions


def test_chains_and_tasks_share_router_sessions_until_router_close(offline_rpc):
    async def check():
        router = ChainlistRouter()
        first, second = Chain(1), Chain(1)
        try:
            with chain_providers_router(router):
                assert await first.eth.block_number == 42
                provider = first._routing_provider._selected_provider()
            with chain_providers_router(router):
                assert (
                    await asyncio.gather(*(second.eth.block_number for _ in range(8)))
                    == [42] * 8
                )
                assert second._routing_provider._selected_provider() is provider
                assert len(offline_rpc) == 1
                await first.close()
                assert not offline_rpc[0].closed
                assert await second.eth.block_number == 42
            await second.close()
            assert not offline_rpc[0].closed
            await router.close()
            assert all(session.closed for session in offline_rpc)
            with pytest.raises(RuntimeError, match="closed"):
                await provider.make_request("eth_blockNumber", [])
        finally:
            await first.close()
            await second.close()
            await router.close()
            for session in offline_rpc:
                await session.close()

    asyncio.run(check())


def test_default_router_closes_sessions_on_asyncio_run_shutdown(offline_rpc):
    loops = []
    providers = []

    async def check():
        loops.append(asyncio.get_running_loop())
        chain = Chain(1)
        assert await chain.eth.block_number == 42
        providers.append(chain._routing_provider._selected_provider())
        await chain.close()
        assert not offline_rpc[-1].closed

    asyncio.run(check())
    assert offline_rpc and all(session.closed for session in offline_rpc)
    assert providers[0]._closed
    assert loops[0] not in default_chainlist_router._providers._lifetimes
    assert loops[0] not in default_chainlist_router._transports._lifetimes
    assert loops[0] not in default_chainlist_router._clients
    asyncio.run(check())
    assert providers[0] is not providers[1]
    assert all(session.closed for session in offline_rpc)


def test_manual_loop_shutdown_uses_default_router_helper(offline_rpc):
    loop = asyncio.new_event_loop()

    async def check():
        chain = Chain(1)
        assert await chain.eth.block_number == 42
        await chain.close()
        assert not offline_rpc[0].closed

    try:
        loop.run_until_complete(check())
        assert not offline_rpc[0].closed
        loop.run_until_complete(close_default_chainlist_router())
        assert all(session.closed for session in offline_rpc)
        assert loop not in default_chainlist_router._clients
        assert not asyncio.all_tasks(loop)
    finally:
        try:
            loop.run_until_complete(close_default_chainlist_router())
        finally:
            for session in offline_rpc:
                loop.run_until_complete(session.close())
            loop.close()


def test_closing_unused_default_loop_is_safe_and_router_remains_usable(offline_rpc):
    async def check():
        await close_default_chainlist_router()
        await close_default_chainlist_router()
        chain = Chain(1)
        try:
            assert await chain.eth.block_number == 42
        finally:
            await chain.close()
            await close_default_chainlist_router()

    asyncio.run(check())
    assert offline_rpc and all(session.closed for session in offline_rpc)


@pytest.mark.parametrize("failure", ["rpc_error", "exception"])
def test_concurrent_failover_keeps_each_transport_endpoint_fixed(
    offline_rpc, monkeypatch, failure
):
    async def check():
        router = ChainlistRouter()
        provider = router.get_chain_provider(1)
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        endpoints = []

        async def interleaved_request(transport, method, data):
            before = str(transport.endpoint_uri)
            session = (
                await transport._request_session_manager.async_cache_and_return_session(
                    transport.endpoint_uri
                )
            )
            if session not in offline_rpc:
                offline_rpc.append(session)
            payload = json.loads(data)
            name = payload["params"][0]
            if name == "slow":
                slow_started.set()
                await release_slow.wait()
            elif before == "https://first.example":
                if failure == "exception":
                    raise TimeoutError("first endpoint unavailable")
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32000, "message": "failed"},
                    }
                ).encode()
            else:
                release_slow.set()
                await asyncio.sleep(0)
            after = str(transport.endpoint_uri)
            endpoints.append((name, before, after))
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": "0x1" if name == "slow" else "0x2",
                }
            ).encode()

        monkeypatch.setattr(AsyncHTTPProvider, "_make_request", interleaved_request)
        slow = asyncio.create_task(provider.make_request("eth_getBalance", ["slow"]))
        try:
            await asyncio.wait_for(slow_started.wait(), 2)
            fast = await asyncio.wait_for(
                provider.make_request("eth_getBalance", ["fast"]), 2
            )
            assert fast["result"] == "0x2"
            assert (await asyncio.wait_for(slow, 2))["result"] == "0x1"
            assert sorted(endpoints) == [
                ("fast", "https://second.example", "https://second.example"),
                ("slow", "https://first.example", "https://first.example"),
            ]
            assert len(offline_rpc) == 2
        finally:
            release_slow.set()
            slow.cancel()
            await asyncio.gather(slow, return_exceptions=True)
            await router.close()
            for session in offline_rpc:
                await session.close()

    asyncio.run(check())


def test_default_shutdown_clears_cached_selection_for_reuse(offline_rpc):
    async def check():
        chain = Chain(1)
        try:
            assert await chain.eth.block_number == 42
            first = chain._routing_provider._selected_provider()
            await close_default_chainlist_router()
            assert offline_rpc[0].closed
            assert await chain.eth.block_number == 42
            second = chain._routing_provider._selected_provider()
            assert second is not first
            assert len(offline_rpc) == 2
            assert not offline_rpc[1].closed
        finally:
            await chain.close()
            await close_default_chainlist_router()
            for session in offline_rpc:
                await session.close()

    asyncio.run(check())


def test_cancelled_request_retains_default_session_until_shutdown(
    offline_rpc, monkeypatch
):
    async def check():
        started = asyncio.Event()

        async def waiting_request(transport, method, data):
            session = (
                await transport._request_session_manager.async_cache_and_return_session(
                    transport.endpoint_uri
                )
            )
            offline_rpc.append(session)
            started.set()
            await asyncio.Future()

        monkeypatch.setattr(AsyncHTTPProvider, "_make_request", waiting_request)
        chain = Chain(1)
        pending = asyncio.create_task(chain.eth.block_number)
        try:
            await asyncio.wait_for(started.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await chain.close()
            assert not offline_rpc[0].closed
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(check())
    assert offline_rpc and all(session.closed for session in offline_rpc)


def test_default_provider_created_before_loop_uses_loop_owned_cleanup(offline_rpc):
    provider = default_chainlist_router.get_chain_provider(1)
    loops = []
    assert provider._client is None
    assert None not in default_chainlist_router._clients

    async def check():
        loop = asyncio.get_running_loop()
        loops.append(loop)
        assert (await provider.make_request("eth_blockNumber", []))["result"] == "0x2a"
        assert provider._owner_loop is loop
        assert loop in default_chainlist_router._clients
        assert not offline_rpc[0].closed

    asyncio.run(check())
    assert offline_rpc and all(session.closed for session in offline_rpc)
    assert provider._closed
    assert provider not in default_chainlist_router._providers._providers.values()
    assert loops[0] not in default_chainlist_router._clients
    assert None not in default_chainlist_router._clients


def test_explorer_only_lookup_cleans_default_client_metadata(monkeypatch):
    loops = []

    async def catalog(client):
        return [
            {
                "chainId": 1,
                "explorers": [
                    {"standard": "EIP3091", "url": "https://explorer.example/"}
                ],
            }
        ]

    monkeypatch.setattr(ChainlistClient, "_fetch_data", catalog)

    async def check():
        loop = asyncio.get_running_loop()
        loops.append(loop)
        assert await get_chain_explorer(1) == "https://explorer.example"
        assert loop in default_chainlist_router._clients
        assert loop in default_chainlist_router._providers._lifetimes

    asyncio.run(check())
    assert loops[0] not in default_chainlist_router._clients
    assert loops[0] not in default_chainlist_router._providers._lifetimes
    assert None not in default_chainlist_router._clients


@pytest.mark.parametrize("factory", ["constructor", "client"])
def test_direct_chainlist_provider_disconnect_closes_owned_transports(
    offline_rpc, factory
):
    async def check():
        client = ChainlistClient()
        provider = (
            ChainlistAsyncHTTPProvider(client, 1)
            if factory == "constructor"
            else client.get_chain_provider(1)
        )
        try:
            assert provider._owns_pool
            assert (await provider.make_request("eth_blockNumber", []))[
                "result"
            ] == "0x2a"
            assert not offline_rpc[0].closed
            await provider.disconnect()
            assert provider._closed
            assert all(session.closed for session in offline_rpc)
            with pytest.raises(RuntimeError, match="closed"):
                await provider.make_request("eth_blockNumber", [])
        finally:
            await provider.close()
            for session in offline_rpc:
                await session.close()

    asyncio.run(check())
