"""Exercise public workflows with real ABIs, signing, and local RPC responses."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from eth_abi import decode, encode
from eth_account import Account as EthAccount
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3.providers import AsyncBaseProvider

from w3ext import Account, Chain, chain_providers_router

CHAIN_ID = 31337
OWNER = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
RECIPIENT = "0x" + "33" * 20
TX_HASH = HexBytes("44" * 32)


class ContractRPC(AsyncBaseProvider):
    def __init__(self, contract_address, call_results=None):
        super().__init__()
        self.contract_address = contract_address
        self.call_results = call_results or {}
        self.submissions = []

    async def disconnect(self):
        pass

    async def make_request(self, method, params):
        if method == "eth_chainId":
            result = hex(CHAIN_ID)
        elif method == "eth_getTransactionCount":
            result = "0x7"
        elif method == "eth_sendRawTransaction":
            self.submissions.append(HexBytes(params[0]))
            result = "0x" + TX_HASH.hex()
        elif method == "eth_call":
            call = params[0]
            assert call["to"] == self.contract_address
            calldata = HexBytes(call["data"])
            assert calldata in self.call_results, f"Unexpected calldata: {call['data']}"
            result = "0x" + self.call_results[calldata].hex()
        else:
            pytest.fail(f"Unexpected RPC method: {method}")
        return {"jsonrpc": "2.0", "id": 1, "result": result}


@asynccontextmanager
async def connected_chain(provider):
    with chain_providers_router(None):
        chain = Chain(CHAIN_ID)
        try:
            await chain.connect_rpc(provider)
            yield chain
        finally:
            await chain.close()


@pytest.mark.parametrize(
    ("operation", "selector", "expected_amount"),
    [
        pytest.param("transfer", "a9059cbb", 1_250_000, id="transfer"),
        pytest.param("finite approval", "095ea7b3", 1_250_000, id="finite-approval"),
        pytest.param(
            "unlimited approval", "095ea7b3", 2**256 - 1, id="unlimited-approval"
        ),
    ],
)
def test_erc20_writes_sign_the_requested_operation(
    operation, selector, expected_amount
):
    async def check():
        rpc = ContractRPC(TOKEN)
        account = Account.from_key("01" * 32)
        async with connected_chain(rpc) as chain:
            token = await chain.load_token(
                TOKEN, name="Example Dollar", symbol="EXD", decimals=6
            )
            transaction = {
                "type": 2,
                "gas": 65_000,
                "maxFeePerGas": 16,
                "maxPriorityFeePerGas": 2,
            }
            if operation == "transfer":
                tx_hash = await token(1.25).transfer(account, RECIPIENT, tx=transaction)
            elif operation == "finite approval":
                tx_hash = await token.approve(
                    account, RECIPIENT, token(1.25), transaction=transaction
                )
            else:
                tx_hash = await token.approve(
                    account, RECIPIENT, transaction=transaction
                )

        assert tx_hash == TX_HASH
        (raw,) = rpc.submissions
        assert EthAccount.recover_transaction(raw) == account.address
        signed = TypedTransaction.from_bytes(raw).as_dict()
        assert signed["to"] == HexBytes(TOKEN)
        assert signed["chainId"] == CHAIN_ID
        assert signed["value"] == 0
        calldata = signed["data"]
        assert calldata[:4] == bytes.fromhex(selector)
        assert decode(["address", "uint256"], calldata[4:]) == (
            RECIPIENT,
            expected_amount,
        )

    asyncio.run(check())


@asynccontextmanager
async def serve_http(app):
    runner = web.AppRunner(app, shutdown_timeout=1)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        ((host, port),) = runner.addresses
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


def test_explicit_http_batch_returns_each_wallets_balance():
    async def check():
        balances = {OWNER: 10**18 + 1, RECIPIENT: 2 * 10**18 + 3}
        batches = []
        single_methods = []

        async def rpc(request):
            payload = await request.json()
            if isinstance(payload, list):
                batches.append(payload)
                responses = []
                for item in reversed(payload):
                    assert item["method"] == "eth_getBalance"
                    wallet, block = item["params"]
                    assert block == "latest"
                    responses.append(
                        {
                            "jsonrpc": "2.0",
                            "id": item["id"],
                            "result": hex(balances[wallet]),
                        }
                    )
                return web.json_response(responses)

            method = payload["method"]
            single_methods.append(method)
            assert method == "eth_chainId"
            return web.json_response(
                {"jsonrpc": "2.0", "id": payload["id"], "result": hex(CHAIN_ID)}
            )

        app = web.Application()
        app.router.add_post("/", rpc)
        async with serve_http(app) as url:
            with chain_providers_router(None):
                async with asyncio.timeout(5):
                    chain = await Chain.connect(url, CHAIN_ID)
                    try:
                        async with chain.use_batch(max_size=2):
                            first, second = await asyncio.gather(
                                chain.get_balance(OWNER), chain.get_balance(RECIPIENT)
                            )
                    finally:
                        await chain.close()

        assert "eth_chainId" in single_methods
        (batch,) = batches
        assert len(batch) == 2
        assert first.currency.symbol == second.currency.symbol == "ETH"
        assert (first.amount, second.amount) == (10**18 + 1, 2 * 10**18 + 3)

    asyncio.run(check())


def test_loaded_erc20_reads_balance_with_contract_decimals():
    async def check():
        raw_balance = 12_375_001
        rpc = ContractRPC(
            TOKEN,
            {
                HexBytes("06fdde03"): encode(["string"], ["Example Dollar"]),
                HexBytes("95d89b41"): encode(["string"], ["EXD"]),
                HexBytes("313ce567"): encode(["uint8"], [6]),
                HexBytes("70a08231") + encode(["address"], [RECIPIENT]): encode(
                    ["uint256"], [raw_balance]
                ),
            },
        )
        async with connected_chain(rpc) as chain:
            token = await chain.load_token(TOKEN)
            balance = await chain.get_balance(RECIPIENT, token)

        assert (token.name, token.symbol, token.decimals) == (
            "Example Dollar",
            "EXD",
            6,
        )
        assert balance.amount == raw_balance
        assert balance.currency == token
        assert balance.to_sigfrac(6) == "12.375001"

    asyncio.run(check())


def test_dynamic_abi_call_encodes_signature_and_decodes_result():
    async def check():
        # Solidity ABI specification example for baz(uint32,bool).
        # https://docs.soliditylang.org/en/latest/abi-spec.html#examples
        calldata = HexBytes(
            "cdcd77c0"
            "0000000000000000000000000000000000000000000000000000000000000045"
            "0000000000000000000000000000000000000000000000000000000000000001"
        )
        contract_address = "0x" + "66" * 20
        rpc = ContractRPC(contract_address, {calldata: bytes(32)})
        async with connected_chain(rpc) as chain:
            contract = chain.contract(contract_address)
            baz = contract.functions.baz[["uint32", "bool"], "bool"]
            assert await baz(69, True).call() is False

    asyncio.run(check())


def test_erc721_enumerates_exact_ids_and_refreshes_owner_and_metadata():
    async def check():
        collection_address = "0x" + "55" * 20
        token_id = 2**128 + 7
        metadata_path = f"/tokens/{token_id}"

        async def metadata(_request):
            return web.json_response(
                {
                    "name": "Night Sky",
                    "description": "A single star",
                    "image": "https://images.example/star.png",
                    "attributes": [{"trait_type": "Color", "value": "Blue"}],
                }
            )

        app = web.Application()
        app.router.add_get(metadata_path, metadata)
        async with serve_http(app) as url:
            name = HexBytes("06fdde03")
            balance_of = HexBytes("70a08231") + encode(["address"], [OWNER])
            token_of_owner_by_index = HexBytes("2f745c59")
            owner_of = HexBytes("6352211e") + encode(["uint256"], [token_id])
            token_uri = HexBytes("c87b56dd") + encode(["uint256"], [token_id])
            rpc = ContractRPC(
                collection_address,
                {
                    name: encode(["string"], ["Constellations"]),
                    balance_of: encode(["uint256"], [2]),
                    token_of_owner_by_index
                    + encode(["address", "uint256"], [OWNER, 0]): encode(
                        ["uint256"], [5]
                    ),
                    token_of_owner_by_index
                    + encode(["address", "uint256"], [OWNER, 1]): encode(
                        ["uint256"], [token_id]
                    ),
                    owner_of: encode(["address"], [RECIPIENT]),
                    token_uri: encode(["string"], [url + metadata_path]),
                },
            )
            async with asyncio.timeout(5):
                async with connected_chain(rpc) as chain:
                    collection = await chain.load_nft721(collection_address)
                    owned = await collection.get_owned_by(OWNER)
                    assert collection.name == "Constellations"
                    assert {item.id for item in owned} == {5, token_id}
                    assert len(owned) == 2
                    item = next(item for item in owned if item.id == token_id)
                    assert await item.get_owner() == OWNER
                    assert await item.get_owner(force=True) == RECIPIENT
                    assert await item.get_owner() == RECIPIENT
                    await item.refresh_metadata()

        assert item.meta.name == "Night Sky"
        assert item.meta.description == "A single star"
        assert item.meta.image == "https://images.example/star.png"
        assert item.meta.attributes == {"Color": "Blue"}

    asyncio.run(check())
