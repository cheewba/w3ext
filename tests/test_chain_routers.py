import asyncio
from unittest.mock import AsyncMock

import pytest
from eth_typing import HexStr
from hexbytes import HexBytes
from web3 import AsyncIPCProvider, WebSocketProvider
from web3._utils.caching import generate_cache_key
from web3.exceptions import MethodNotSupported, SubscriptionProcessingFinished
from web3.types import RPCEndpoint
from web3.middleware import Web3Middleware, async_combine_middleware
from web3.providers import AsyncBaseProvider
from web3.providers.persistent.request_processor import RequestInformation
from web3.utils.subscriptions import NewHeadsSubscription

from w3ext.account import Account
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


def test_child_task_does_not_inherit_parent_provider_selection(monkeypatch):
    monkeypatch.setattr(
        "w3ext.chain.routers.get_chain_provider", lambda *_: StubProvider(1)
    )
    chain = Chain(1)
    parent_provider = StubProvider(7)
    child_provider = StubProvider(8)

    class TaskRouter:
        def get_chain_provider(self, chain_id, request_kwargs=None):
            return (
                child_provider
                if asyncio.current_task().get_name() == "child"
                else parent_provider
            )

    async def check():
        with chain_providers_router(TaskRouter()):
            assert await chain.eth.block_number == 7
            child = asyncio.create_task(chain.eth.block_number, name="child")
            assert await child == 8
            assert await chain.eth.block_number == 7

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


def test_retained_subscription_manager_uses_its_provider_in_child_task():
    chain = Chain(1)
    first = WebSocketProvider("ws://localhost:12345")
    second = WebSocketProvider("ws://localhost:12346")
    seen = []
    subscription_id = HexStr("0xabc")

    async def handler(context):
        seen.append((context.result, chain._web3.provider))

    async def check():
        with chain_providers_router(StubRouter(first)):
            manager = chain._web3.subscription_manager
            subscription = NewHeadsSubscription(handler=handler)
            subscription.manager = manager
            subscription._id = subscription_id
            manager._validate_and_normalize_label(subscription)
            manager._add_subscription(subscription)

            processor = first._request_processor
            processor._request_information_cache.cache(
                generate_cache_key(subscription_id),
                RequestInformation(
                    RPCEndpoint("eth_subscribe"),
                    ["newHeads"],
                    ((), (), ()),
                    subscription_id=subscription_id,
                ),
            )
            queue = processor._handler_subscription_queue
            await queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "eth_subscription",
                    "params": {"subscription": subscription_id, "result": "0x2a"},
                }
            )
            await queue.put(SubscriptionProcessingFinished())

        async def unsubscribe(subscription_id):
            assert chain._web3.provider is first
            return True

        chain._web3.eth._unsubscribe = AsyncMock(side_effect=unsubscribe)
        with chain_providers_router(StubRouter(second), force=True):
            await asyncio.create_task(manager.handle_subscriptions())
            assert await manager.unsubscribe(subscription)
        assert seen == [("0x2a", first)]
        chain._web3.eth._unsubscribe.assert_awaited_once_with(subscription_id)

    asyncio.run(check())


def test_retained_socket_uses_its_original_provider():
    chain = Chain(1)
    first = WebSocketProvider("ws://localhost:12345")
    second = WebSocketProvider("ws://localhost:12346")
    seen = []

    async def send(method, params):
        seen.append(("send", chain._web3.provider))

    async def recv():
        seen.append(("recv", chain._web3.provider))
        return {"result": "0x1"}

    async def next_message():
        seen.append(("subscription", chain._web3.provider))
        return {"result": "0x2"}

    async def make_request(method, params):
        seen.append(("request", chain._web3.provider))
        return {"result": "0x3"}

    async def check():
        with chain_providers_router(StubRouter(first)):
            socket = chain._web3.socket
        chain._web3.manager.send = send
        chain._web3.manager.recv = recv
        chain._web3.manager._get_next_message = next_message
        first.make_request = make_request

        with chain_providers_router(StubRouter(second), force=True):
            assert socket.provider is first
            await socket.send(RPCEndpoint("eth_blockNumber"), [])
            assert await socket.recv() == {"result": "0x1"}
            assert await socket.make_request(RPCEndpoint("eth_blockNumber"), []) == {
                "result": "0x3"
            }
            assert await anext(socket.process_subscriptions()) == {"result": "0x2"}
        await socket.send(RPCEndpoint("eth_blockNumber"), [])
        assert [provider for _, provider in seen] == [first] * 5

    asyncio.run(check())


@pytest.mark.parametrize(
    ("provider_class", "endpoint"),
    [
        (WebSocketProvider, "ws://localhost:12345"),
        (AsyncIPCProvider, "/tmp/w3ext-nonexistent.ipc"),
    ],
)
def test_persistent_batch_formats_multiple_results(
    provider_class, endpoint
):
    chain = Chain(1)
    provider = provider_class(endpoint)
    provider.make_batch_request = AsyncMock(
        return_value=[
            {"jsonrpc": "2.0", "id": 0, "result": "0x2a"},
            {"jsonrpc": "2.0", "id": 1, "result": "0x2b"},
        ]
    )
    address = "0x0000000000000000000000000000000000000001"

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_batch(max_size=2):
                balances = await asyncio.wait_for(
                    asyncio.gather(
                        chain.eth.get_balance(address), chain.eth.get_balance(address)
                    ),
                    2,
                )
                assert balances == [42, 43]
        provider.make_batch_request.assert_awaited_once()

    asyncio.run(check())


def test_persistent_batch_applies_poa_response_middleware():
    chain = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    extra_data = "0x" + "ab" * 33
    block = {"number": "0x1", "extraData": extra_data, "transactions": []}
    provider.make_batch_request = AsyncMock(
        return_value=[{"jsonrpc": "2.0", "id": 0, "result": block}]
    )

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_batch(max_size=1):
                result = await chain.eth.get_block(1)
        assert result["proofOfAuthorityData"] == HexBytes(extra_data)
        assert "extraData" not in result

    asyncio.run(check())


def test_shared_persistent_provider_formats_batches_for_each_chain():
    first = Chain(1)
    second = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    extra_data = "0x" + "ab" * 33
    block = {"number": "0x1", "extraData": extra_data, "transactions": []}
    provider.make_batch_request = AsyncMock(
        return_value=[{"jsonrpc": "2.0", "id": 0, "result": block}]
    )

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with first.use_batch(max_size=1), second.use_batch(max_size=1):
                results = await asyncio.gather(
                    first.eth.get_block(1), second.eth.get_block(1)
                )
        assert [item["proofOfAuthorityData"] for item in results] == [
            HexBytes(extra_data),
            HexBytes(extra_data),
        ]

    asyncio.run(check())


def test_persistent_batch_chunks_do_not_overlap():
    chain = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    active = 0
    maximum_active = 0
    batches = []

    async def make_batch_request(requests):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        batches.append(requests)
        try:
            if len(batches) == 1:
                await asyncio.sleep(0.05)
            return [
                {"jsonrpc": "2.0", "id": index, "result": hex(int(params[0], 16))}
                for index, (_, params) in enumerate(requests)
            ]
        finally:
            active -= 1

    provider.make_batch_request = make_batch_request

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_batch(max_size=2):
                balances = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            chain.eth.get_balance(f"0x{index:040x}")
                            for index in range(1, 5)
                        )
                    ),
                    2,
                )
        assert balances == [1, 2, 3, 4]
        assert len(batches) == 2
        assert maximum_active == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    ("method", "result", "expected"),
    [
        ("call", "0x1234", HexBytes("0x1234")),
        ("estimate_gas", "0x5208", 21000),
    ],
)
def test_persistent_batch_formats_results_after_nested_chain_id_rpc(
    method, result, expected
):
    chain = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    nested_ids = []
    batch_ids = []
    address = "0x0000000000000000000000000000000000000001"

    async def socket_request(rpc_method, params, response_formatters=None):
        assert rpc_method == "eth_chainId"
        nested_ids.append(provider.form_request(rpc_method, params)["id"])
        return 1

    async def make_batch_request(requests):
        encoded = [
            provider.form_request(rpc_method, params)
            for rpc_method, params in requests
        ]
        batch_ids.extend(request["id"] for request in encoded)
        return [
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
            for request in encoded
        ]

    chain._web3.manager.socket_request = socket_request
    provider.make_batch_request = make_batch_request

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_batch(max_size=1):
                actual = await getattr(chain.eth, method)(
                    {"to": address, "chainId": 1}
                )
                assert actual == expected
        assert nested_ids
        assert batch_ids[0] > nested_ids[0]

    asyncio.run(check())


def test_persistent_batch_formats_contract_call_after_nested_chain_id_rpc():
    chain = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    address = "0x0000000000000000000000000000000000000001"
    abi = [
        {
            "type": "function",
            "name": "value",
            "stateMutability": "view",
            "inputs": [],
            "outputs": [{"name": "", "type": "uint256"}],
        }
    ]
    contract = chain.contract(address, abi)
    nested_ids = []
    batch_ids = []

    async def socket_request(method, params, response_formatters=None):
        assert method == "eth_chainId"
        nested_ids.append(provider.form_request(method, params)["id"])
        return 1

    async def make_batch_request(requests):
        encoded = [provider.form_request(method, params) for method, params in requests]
        batch_ids.extend(request["id"] for request in encoded)
        return [
            {"jsonrpc": "2.0", "id": request["id"], "result": "0x" + "0" * 62 + "2a"}
            for request in encoded
        ]

    chain._web3.manager.socket_request = socket_request
    provider.make_batch_request = make_batch_request

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_batch(max_size=1):
                assert await contract.functions.value().call({"chainId": 1}) == 42
        assert batch_ids[0] > nested_ids[0]

    asyncio.run(check())


@pytest.mark.parametrize("flush_on_exit", [False, True])
def test_batch_validation_middleware_queries_chain_id_outside_collection(
    flush_on_exit,
):
    chain = Chain(1)
    provider = StubProvider(1)
    address = "0x0000000000000000000000000000000000000001"

    async def check():
        await chain.connect_rpc(provider)
        if flush_on_exit:
            async with chain.use_batch(max_size=20, max_wait=0) as batcher:
                call = asyncio.create_task(
                    chain.eth.call({"to": address, "chainId": 1})
                )
                while not batcher._requests:
                    if call.done():
                        await call
                    await asyncio.sleep(0)
            result = await call
        else:
            async with chain.use_batch(max_size=1):
                result = await chain.eth.call({"to": address, "chainId": 1})
        assert result == HexBytes("0x01")
        assert any(method == "eth_chainId" for method, _ in provider.requests)

    asyncio.run(check())


def test_context_response_middleware_uses_actual_http_batch_response():
    chain = Chain(1)
    provider = StubProvider(7)
    seen = []
    address = "0x0000000000000000000000000000000000000001"

    def increment(make_request, _w3):
        async def middleware(method, params):
            seen.append("request")
            response = await make_request(method, params)
            seen.append("response")
            return {**response, "result": hex(int(response["result"], 16) + 1)}

        return middleware

    async def check():
        await chain.connect_rpc(provider)
        async with chain.use_middlewares(increment):
            async with chain.use_batch(max_size=1):
                assert await chain.eth.get_balance(address) == 8
        assert len(provider.batches) == 1
        assert seen == ["request", "response"]

    asyncio.run(check())


def test_context_batch_middleware_releases_semaphore_between_wire_batches():
    chain = Chain(1)
    provider = StubProvider(7)
    semaphore = asyncio.Semaphore(1)
    address = "0x0000000000000000000000000000000000000001"

    def limit(make_request, _w3):
        async def middleware(method, params):
            async with semaphore:
                return await make_request(method, params)

        return middleware

    async def check():
        await chain.connect_rpc(provider)
        async with chain.use_middlewares(limit):
            async with chain.use_batch(max_size=2):
                balances = await asyncio.wait_for(
                    asyncio.gather(
                        chain.eth.get_balance(address), chain.eth.get_balance(address)
                    ),
                    2,
                )
        assert balances == [7, 7]
        assert [len(batch) for batch in provider.batches] == [1, 1]

    asyncio.run(check())


@pytest.mark.parametrize("persistent", [False, True])
def test_context_middleware_can_short_circuit_batch_requests(persistent):
    chain = Chain(1)
    provider = (
        WebSocketProvider("ws://localhost:12345") if persistent else StubProvider(7)
    )
    sent_batches = []
    cached = "0x0000000000000000000000000000000000000001"
    uncached = "0x0000000000000000000000000000000000000002"

    if persistent:
        async def make_batch_request(requests):
            sent_batches.append(requests)
            return [
                {
                    "jsonrpc": "2.0",
                    "id": provider.form_request(method, params)["id"],
                    "result": "0x7",
                }
                for method, params in requests
            ]

        provider.make_batch_request = make_batch_request
    else:
        sent_batches = provider.batches

    def cache(make_request, _w3):
        async def middleware(method, params):
            if method == "eth_getBalance" and params[0].lower() == cached.lower():
                return {"jsonrpc": "2.0", "id": 99, "result": "0xa"}
            return await make_request(method, params)

        return middleware

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_middlewares(cache):
                async with chain.use_batch(max_size=2):
                    results = await asyncio.gather(
                        chain.eth.get_balance(cached), chain.eth.get_balance(uncached)
                    )
                assert results == [10, 7]

                async with chain.use_batch(max_size=2):
                    cached_results = await asyncio.gather(
                        chain.eth.get_balance(cached), chain.eth.get_balance(cached)
                    )
                assert cached_results == [10, 10]
        assert [len(batch) for batch in sent_batches] == [1]

    asyncio.run(check())


def test_context_middleware_can_short_circuit_persistent_single_request():
    chain = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    provider.send_request = AsyncMock(side_effect=AssertionError("RPC was sent"))
    address = "0x0000000000000000000000000000000000000001"
    extra_data = "0x" + "ab" * 33

    def cache(make_request, _w3):
        async def middleware(method, params):
            if method == "eth_getBalance":
                result = "0xa"
            elif method == "eth_getBlockByNumber":
                result = {"number": "0x1", "extraData": extra_data, "transactions": []}
            else:
                raise AssertionError(f"Unexpected method: {method}")
            return {"jsonrpc": "2.0", "id": 99, "result": result}

        return middleware

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_middlewares(cache):
                assert await chain.eth.get_balance(address) == 10
                block = await chain.eth.get_block(1)
                assert block["proofOfAuthorityData"] == HexBytes(extra_data)
        provider.send_request.assert_not_awaited()

    asyncio.run(check())


def test_persistent_context_middleware_resumes_with_response_and_can_retry():
    chain = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    sent = []
    events = []
    address = "0x0000000000000000000000000000000000000001"

    async def send_request(method, params):
        request = provider.form_request(method, params)
        sent.append(request)
        events.append("send")
        return request

    async def recv_for_request(request):
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": "0x0" if len(sent) == 1 else "0x7",
        }

    provider.send_request = send_request
    provider.recv_for_request = recv_for_request

    def retry_and_increment(make_request, _w3):
        async def middleware(method, params):
            try:
                response = await make_request(method, params)
                if response["result"] == "0x0":
                    response = await make_request(method, params)
                events.append("response")
                return {
                    **response,
                    "result": hex(int(response["result"], 16) + 1),
                }
            finally:
                events.append("finally")

        return middleware

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_middlewares(retry_and_increment):
                assert await chain.eth.get_balance(address) == 8
        assert len(sent) == 2
        assert events == ["send", "send", "response", "finally"]

    asyncio.run(check())


def test_cached_persistent_batch_block_runs_poa_middleware():
    chain = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    provider.make_batch_request = AsyncMock(side_effect=AssertionError("RPC was sent"))
    extra_data = "0x" + "ab" * 33

    def cache(make_request, _w3):
        async def middleware(method, params):
            assert method == "eth_getBlockByNumber"
            return {
                "jsonrpc": "2.0",
                "id": 99,
                "result": {
                    "number": "0x1",
                    "extraData": extra_data,
                    "transactions": [],
                },
            }

        return middleware

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_middlewares(cache):
                async with chain.use_batch(max_size=1):
                    block = await chain.eth.get_block(1)
        assert block["proofOfAuthorityData"] == HexBytes(extra_data)
        provider.make_batch_request.assert_not_awaited()

    asyncio.run(check())


def test_persistent_route_applies_context_request_middleware():
    chain = Chain(1)
    provider = WebSocketProvider("ws://localhost:12345")
    sent = []

    async def send_request(method, params):
        sent.append((method, params))
        return {"jsonrpc": "2.0", "id": len(sent), "method": method, "params": params}

    provider.send_request = send_request

    def rewrite(make_request, _w3):
        async def middleware(method, params):
            return await make_request(RPCEndpoint("custom_method"), params)

        return middleware

    async def check():
        with chain_providers_router(StubRouter(provider)):
            async with chain.use_middlewares(rewrite):
                await chain._web3.manager.send(RPCEndpoint("eth_blockNumber"), [])
            await chain._web3.manager.send(RPCEndpoint("eth_blockNumber"), [])
        assert [method for method, _ in sent] == ["custom_method", "eth_blockNumber"]

    asyncio.run(check())


def test_send_transaction_signs_with_persistent_route(monkeypatch):
    chain = Chain(1)
    account = Account.from_key("0x" + "01" * 32)
    provider = WebSocketProvider("ws://localhost:12345")
    sent = []

    async def send_request(method, params):
        sent.append((method, params))
        return {"jsonrpc": "2.0", "id": len(sent), "method": method, "params": params}

    provider.send_request = send_request
    chain._web3.manager.recv_for_request = AsyncMock(return_value=HexBytes("0x" + "ab" * 32))
    monkeypatch.setattr("w3ext.utils.common.is_eip1559", AsyncMock(return_value=False))

    async def check():
        with chain_providers_router(StubRouter(provider)):
            await chain.send_transaction(
                {
                    "to": "0x0000000000000000000000000000000000000001",
                    "value": 1,
                    "nonce": 0,
                    "gas": 21000,
                    "gasPrice": 1,
                },
                account,
            )
        assert sent[-1][0] == "eth_sendRawTransaction"
        assert sent[-1][1][0].startswith("0x")

    asyncio.run(check())
