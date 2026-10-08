import asyncio
from unittest.mock import AsyncMock

import pytest
from hexbytes import HexBytes
from web3 import AsyncIPCProvider, WebSocketProvider
from web3.exceptions import MethodNotSupported
from web3.middleware import Web3Middleware, async_combine_middleware
from web3.providers import AsyncBaseProvider

from w3ext.chain import Chain, chain_providers_router
from w3ext.exceptions import ChainException


class StubProvider(AsyncBaseProvider):
    def __init__(self, block_number, chain_id=1):
        super().__init__()
        self.block_number = block_number
        self.chain_id = chain_id
        self.requests = []
        self.batches = []

    async def make_request(self, method, params):
        self.requests.append((method, params))
        result = hex(self.chain_id) if method == "eth_chainId" else hex(self.block_number)
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


class NormalizeBlockMiddleware(Web3Middleware):
    async def async_wrap_make_request(self, make_request):
        async def middleware(method, params):
            if method == "eth_getBalance":
                params = (params[0], int(params[1], 16))
            return await make_request(method, params)

        return middleware


class MiddlewareProvider(StubProvider):
    async def request_func(self, async_w3, middleware_onion):
        middleware = middleware_onion.as_tuple_of_middleware() + (
            NormalizeBlockMiddleware,
        )
        return await async_combine_middleware(
            middleware, async_w3, self.make_request
        )

    async def make_request(self, method, params):
        if method == "eth_getBalance":
            assert params[1] == 5
        return await super().make_request(method, params)


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
        assert outer.calls == [(1, None)]

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


def test_shared_router_context_selects_provider_per_task(monkeypatch):
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: StubProvider(1)
    )
    chain = Chain(1)
    providers = {"first": StubProvider(7), "second": StubProvider(8)}

    class TaskRouter:
        def get_chain_provider(self, chain_id, request_kwargs=None):
            return providers[asyncio.current_task().get_name()]

    async def read():
        await asyncio.sleep(0)
        return await chain.eth.block_number

    async def check():
        with chain_providers_router(TaskRouter()):
            first = asyncio.create_task(read(), name="first")
            second = asyncio.create_task(read(), name="second")
            assert await asyncio.gather(first, second) == [7, 8]

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


def test_batch_keeps_each_callers_route_and_groups_by_provider(monkeypatch):
    fallback = StubProvider(1)
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: fallback
    )
    chain = Chain(1)
    first = StubProvider(7)
    second = StubProvider(8)
    address = "0x0000000000000000000000000000000000000001"

    async def read(router):
        with chain_providers_router(router):
            return await chain.eth.get_balance(address)

    async def check():
        async with chain.use_batch(max_size=3):
            results = await asyncio.gather(
                read(StubRouter(first)),
                read(StubRouter(second)),
                read(StubRouter(first)),
            )
        assert results == [7, 8, 7]
        assert [len(batch) for batch in first.batches] == [2]
        assert [len(batch) for batch in second.batches] == [1]
        assert fallback.batches == []

    asyncio.run(check())


def test_none_inside_batch_does_not_send_to_chainlist(monkeypatch):
    fallback = StubProvider(1)
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: fallback
    )
    chain = Chain(1)
    address = "0x0000000000000000000000000000000000000001"

    async def check():
        async with chain.use_batch(max_size=1):
            with chain_providers_router(None):
                with pytest.raises(ChainException, match="No RPC provider configured"):
                    await chain.eth.get_balance(address)
        assert fallback.batches == []

    asyncio.run(check())


def test_connect_rpc_verifies_supplied_provider_under_forced_router():
    chain = Chain(1)
    routed = StubProvider(4, chain_id=1)
    wrong = StubProvider(5, chain_id=2)

    async def check():
        with chain_providers_router(StubRouter(routed), force=True):
            with pytest.raises(ChainException, match="2 vs expected 1"):
                await chain.connect_rpc(wrong)
        assert wrong.requests == [("eth_chainId", ())]
        assert routed.requests == []
        assert chain._routing_provider.explicit_provider is None

    asyncio.run(check())


def test_selected_provider_request_middleware_is_used():
    chain = Chain(1)
    provider = MiddlewareProvider(7)
    address = "0x0000000000000000000000000000000000000001"

    async def check():
        await chain.connect_rpc(provider)
        assert await chain.eth.get_balance(address, 5) == 7
        assert provider.requests[-1][1][-1] == 5

    asyncio.run(check())


def test_ccip_policy_follows_selected_provider():
    chain = Chain(1)
    disabled = StubProvider(1)
    disabled.global_ccip_read_enabled = False
    disabled.ccip_read_max_redirects = 9
    enabled = StubProvider(2)

    async def check():
        await chain.connect_rpc(enabled)
        eth = chain._web3.eth
        eth._call = AsyncMock(return_value=HexBytes("0x12"))
        eth._durin_call = AsyncMock(return_value=HexBytes("0x34"))
        with chain_providers_router(StubRouter(disabled), force=True):
            assert chain._web3.provider.ccip_read_max_redirects == 9
            assert await eth.call({"to": "0x0000000000000000000000000000000000000001"}) == HexBytes("0x12")
        eth._call.assert_awaited_once()
        eth._durin_call.assert_not_awaited()
        assert chain._web3.provider.global_ccip_read_enabled is True

    asyncio.run(check())


@pytest.mark.parametrize(
    ("provider_class", "endpoint"),
    [
        (WebSocketProvider, "ws://localhost:12345"),
        (AsyncIPCProvider, "/tmp/w3ext-nonexistent.ipc"),
    ],
)
def test_explicit_persistent_provider_supports_subscriptions(provider_class, endpoint):
    async def check():
        chain = Chain(1)
        provider = provider_class(endpoint)
        chain._web3.manager.socket_request = AsyncMock(return_value=1)

        await chain.connect_rpc(provider)
        chain._web3.manager.socket_request.assert_awaited_once()
        assert chain._web3.provider is provider
        assert chain._web3.manager._request_processor is provider._request_processor

        subscribe = AsyncMock(return_value="0xabc")
        first_manager = chain._web3.subscription_manager
        first_socket = chain._web3.socket
        first_manager.subscribe = subscribe
        assert await chain.eth.subscribe("newHeads") == "0xabc"
        subscribe.assert_awaited_once()

        other = provider_class(endpoint)
        with chain_providers_router(StubRouter(other), force=True):
            assert chain._web3.provider is other
            assert chain._web3.manager._request_processor is other._request_processor
            assert chain._web3.subscription_manager._provider is other
            assert chain._web3.subscription_manager is not first_manager
            assert chain._web3.socket.provider is other
            assert chain._web3.socket is not first_socket
        assert chain._web3.subscription_manager is first_manager
        assert chain._web3.socket is first_socket

        with chain_providers_router(StubRouter(StubProvider(2)), force=True):
            with pytest.raises(MethodNotSupported):
                await chain.eth.subscribe("newHeads")
        with chain_providers_router(None):
            assert chain._web3.provider is provider

    asyncio.run(check())
