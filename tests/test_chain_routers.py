import asyncio

import pytest
from web3.providers import AsyncBaseProvider

from w3ext.chain import Chain, chain_providers_router
from w3ext.exceptions import ChainException


class StubProvider(AsyncBaseProvider):
    def __init__(self, block_number):
        super().__init__()
        self.block_number = block_number
        self.requests = []
        self.batches = []

    async def make_request(self, method, params):
        self.requests.append((method, params))
        result = "0x1" if method == "eth_chainId" else hex(self.block_number)
        return {"jsonrpc": "2.0", "id": 1, "result": result}

    async def make_batch_request(self, requests):
        self.batches.append(requests)
        return [await self.make_request(method, params) for method, params in requests]

    async def is_connected(self, show_traceback=False):
        return True

    async def disconnect(self):
        pass


class StubRouter:
    def __init__(self, provider):
        self.provider = provider
        self.calls = []

    def get_chain_provider(self, chain_id, request_kwargs=None):
        self.calls.append((chain_id, request_kwargs))
        return self.provider


def test_router_is_used_for_existing_chain_and_restores_chainlist(monkeypatch):
    fallback = StubProvider(1)
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: fallback
    )
    chain = Chain(1, request_kwargs={"timeout": 7})
    routed = StubProvider(2)
    router = StubRouter(routed)

    async def check():
        assert await chain.eth.block_number == 1
        with chain_providers_router(router):
            assert await chain.eth.block_number == 2
            assert router.calls == [(1, {"timeout": 7})]
        assert await chain.eth.block_number == 1

    asyncio.run(check())


def test_explicit_provider_wins_unless_closest_router_is_forced(monkeypatch):
    fallback = StubProvider(1)
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: fallback
    )
    chain = Chain(1)
    explicit = StubProvider(3)
    outer = StubRouter(StubProvider(4))
    inner = StubRouter(StubProvider(5))

    async def check():
        await chain.connect_rpc(explicit)
        assert await chain.eth.block_number == 3
        with chain_providers_router(outer, force=True):
            assert await chain.eth.block_number == 4
            with chain_providers_router(inner):
                assert await chain.eth.block_number == 3
            with chain_providers_router(inner, force=True):
                assert await chain.eth.block_number == 5
            assert await chain.eth.block_number == 4
        assert await chain.eth.block_number == 3
        assert inner.calls == [(1, None)]

    asyncio.run(check())


def test_router_without_provider_falls_back_to_chainlist(monkeypatch):
    fallback = StubProvider(1)
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: fallback
    )
    chain = Chain(1)
    empty = StubRouter(None)
    outer = StubRouter(StubProvider(9))

    async def check():
        with chain_providers_router(outer):
            assert await chain.eth.block_number == 9
            with chain_providers_router(empty):
                assert await chain.eth.block_number == 1
        await chain.connect_rpc(StubProvider(3))
        with chain_providers_router(empty, force=True):
            assert await chain.eth.block_number == 1
        assert empty.calls == [(1, None), (1, None)]

    asyncio.run(check())


def test_none_disables_all_routers_but_keeps_explicit_rpc(monkeypatch):
    fallback = StubProvider(1)
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: fallback
    )
    chain = Chain(1)
    outer = StubRouter(StubProvider(2))

    async def check():
        with chain_providers_router(outer, force=True):
            assert await chain.eth.block_number == 2
            with chain_providers_router(None):
                assert not await chain._web3.is_connected()
                with pytest.raises(ChainException, match="No RPC provider configured"):
                    await chain.eth.block_number
            assert await chain.eth.block_number == 2

        explicit = StubProvider(3)
        await chain.connect_rpc(explicit)
        with chain_providers_router(outer, force=True):
            with chain_providers_router(None, force=True):
                assert await chain.eth.block_number == 3
        assert outer.calls == [(1, None), (1, None)]

    asyncio.run(check())


def test_router_context_is_isolated_across_async_tasks(monkeypatch):
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: StubProvider(1)
    )
    chain = Chain(1)

    async def read(block_number):
        with chain_providers_router(StubRouter(StubProvider(block_number))):
            await asyncio.sleep(0)
            return await chain.eth.block_number

    async def check():
        assert await asyncio.gather(read(7), read(8)) == [7, 8]
        assert await chain.eth.block_number == 1

    asyncio.run(check())


def test_batch_requests_use_active_router(monkeypatch):
    fallback = StubProvider(1)
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: fallback
    )
    chain = Chain(1)
    routed = StubProvider(2)
    address = "0x0000000000000000000000000000000000000001"

    async def check():
        with chain_providers_router(StubRouter(routed)):
            async with chain.use_batch(max_size=1):
                assert await chain.eth.get_balance(address) == 2
        assert len(routed.batches) == 1
        assert routed.batches[0][0][0] == "eth_getBalance"
        assert fallback.batches == []

    asyncio.run(check())
