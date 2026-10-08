"""Real HTTP-session lifetime coverage without network calls."""

import asyncio
import gc
import weakref
from unittest.mock import AsyncMock

import pytest
from web3 import AsyncHTTPProvider, WebSocketProvider

from w3ext import Chain, chain_providers_router


class SessionProvider(AsyncHTTPProvider):
    def __init__(self, chain_id=1, *, started=None, release=None, sessions=None):
        super().__init__(f"https://rpc-{chain_id}.example")
        self.chain_id = chain_id
        self.started = started
        self.release = release
        self.sessions = [] if sessions is None else sessions
        self.disconnect_calls = 0
        self.disconnect_error = None

    async def make_request(self, method, params):
        session = await self._request_session_manager.async_cache_and_return_session(
            self.endpoint_uri
        )
        if session not in self.sessions:
            self.sessions.append(session)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        result = self.chain_id if method == "eth_chainId" else 42
        return {"jsonrpc": "2.0", "id": 1, "result": hex(result)}

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error
        await super().disconnect()


class Router:
    def __init__(self, factory):
        self.factory = factory
        self.providers = []

    def get_chain_provider(self, chain_id, request_kwargs=None):
        provider = self.factory(int(chain_id))
        self.providers.append(provider)
        return provider


@pytest.fixture
def fallback(monkeypatch):
    provider = SessionProvider()
    monkeypatch.setattr(
        "w3ext.chain.routers.default_chainlist_router.get_chain_provider",
        lambda *_: provider,
    )
    return provider


def test_cancelled_router_call_keeps_sessions_reachable_for_chain_close(fallback):
    async def check():
        started = asyncio.Event()
        chain = Chain(1)
        provider_refs = []
        sessions = []

        class EphemeralRouter:
            def get_chain_provider(self, chain_id, request_kwargs=None):
                provider = SessionProvider(
                    started=started, release=asyncio.Event(), sessions=sessions
                )
                provider_refs.append(weakref.ref(provider))
                return provider

        async def call():
            with chain_providers_router(EphemeralRouter()):
                await chain.eth.block_number

        task = asyncio.create_task(call())
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gc.collect()
            assert provider_refs[0]() is not None
            assert sessions and not sessions[0].closed
            await chain.close()
            assert all(session.closed for session in sessions)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await chain.close()
            for session in sessions:
                await session.close()

    asyncio.run(check())


def test_close_covers_child_tasks_nested_scopes_and_explicit_rpc(fallback):
    async def check():
        chain = Chain(1)
        explicit = SessionProvider()
        router = Router(lambda chain_id: SessionProvider(chain_id))
        inner = Router(lambda chain_id: SessionProvider(chain_id))
        try:
            assert await chain.eth.block_number == 42
            await chain.connect_rpc(explicit)
            with chain_providers_router(router, force=True):
                assert await chain.eth.block_number == 42
                assert await asyncio.gather(
                    chain.eth.block_number, chain.eth.block_number
                ) == [42, 42]
                with chain_providers_router(inner, force=True):
                    assert await chain.eth.block_number == 42
            providers = [fallback, explicit, *router.providers, *inner.providers]
            assert len(router.providers) == 3
            assert all(not provider.sessions[0].closed for provider in providers)
            await chain.close()
            assert all(provider.sessions[0].closed for provider in providers)
            assert all(provider.disconnect_calls == 1 for provider in providers)
        finally:
            for provider in [fallback, explicit, *router.providers, *inner.providers]:
                await AsyncHTTPProvider.disconnect(provider)

    asyncio.run(check())


def test_close_deduplicates_shared_providers_and_does_not_select_a_new_one(fallback):
    async def check():
        chain = Chain(1)
        router = Router(lambda _: fallback)
        try:
            await chain.connect_rpc(fallback)
            with chain_providers_router(router, force=True):
                assert await chain.eth.block_number == 42
                assert await asyncio.gather(
                    chain.eth.block_number, chain.eth.block_number
                ) == [42, 42]
                selected = len(router.providers)
                await chain.close()
                assert len(router.providers) == selected
                assert fallback.disconnect_calls == 1
                assert fallback.sessions[0].closed
                # Closing releases ownership; using a cached selection is safe
                # and must register it again for the next close.
                assert await chain.eth.block_number == 42
            await chain.close()
            assert fallback.disconnect_calls == 2
            assert all(session.closed for session in fallback.sessions)

            unused = Router(lambda _: SessionProvider())
            with chain_providers_router(unused, force=True):
                await chain.close()
            assert unused.providers == []
        finally:
            await AsyncHTTPProvider.disconnect(fallback)

    asyncio.run(check())


def test_close_attempts_other_providers_when_one_disconnect_fails(fallback):
    async def check():
        chain = Chain(1)
        router = Router(lambda chain_id: SessionProvider(chain_id))
        try:
            with chain_providers_router(router):
                assert await chain.eth.block_number == 42
                assert await asyncio.create_task(chain.eth.block_number) == 42
            first, second = router.providers
            first.disconnect_error = RuntimeError("disconnect failed")
            with pytest.raises(RuntimeError, match="disconnect failed"):
                await chain.close()
            assert first.disconnect_calls == 1
            assert second.disconnect_calls == 1
            assert second.sessions[0].closed
            first.disconnect_error = None
            await chain.close()
            assert first.sessions[0].closed
            assert first.disconnect_calls == 2
            assert second.disconnect_calls == 1
        finally:
            for provider in [fallback, *router.providers]:
                await AsyncHTTPProvider.disconnect(provider)

    asyncio.run(check())


def test_close_covers_routed_sessions_with_a_persistent_explicit_provider(fallback):
    async def check():
        chain = Chain(1)
        explicit = WebSocketProvider("ws://localhost:12345")
        explicit.disconnect = AsyncMock()
        chain._routing_provider.explicit_provider = explicit
        router = Router(lambda chain_id: SessionProvider(chain_id))
        try:
            with chain_providers_router(router, force=True):
                assert await chain.eth.block_number == 42
            await chain.close()
            explicit.disconnect.assert_awaited_once()
            assert router.providers[0].sessions[0].closed
        finally:
            for provider in [fallback, *router.providers]:
                await AsyncHTTPProvider.disconnect(provider)

    asyncio.run(check())
