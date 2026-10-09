"""Real signing of raw transactions with an offline RPC provider."""

import asyncio
from typing import cast

import pytest
from eth_account import Account as EthAccount
from eth_account._utils.legacy_transactions import Transaction
from eth_account.typed_transactions.typed_transaction import TypedTransaction
from hexbytes import HexBytes
from web3.providers import AsyncBaseProvider

from w3ext import Account, Chain, TxParams
from w3ext.utils import fill_gas_price


class CapturingProvider(AsyncBaseProvider):
    def __init__(self, chain_id, dynamic):
        super().__init__()
        self.raw_transactions = []
        self.methods = []
        self.responses = {
            "eth_chainId": hex(chain_id),
            "eth_getTransactionCount": "0x0",
            "eth_gasPrice": "0x7",
            "eth_maxPriorityFeePerGas": "0x5",
            "eth_feeHistory": {
                "oldestBlock": "0x1",
                "baseFeePerGas": ["0x64", "0x64"] if dynamic else ["0x0", "0x0"],
                "gasUsedRatio": [0.5],
            },
            "eth_getBlockByNumber": {
                "number": "0x1",
                "baseFeePerGas": "0x64" if dynamic else "0x0",
            },
        }

    async def disconnect(self):
        pass

    async def make_request(self, method, params):
        self.methods.append(method)
        if method == "eth_sendRawTransaction":
            self.raw_transactions.append(HexBytes(params[0]))
            result = "0x" + "ab" * 32
        else:
            assert method in self.responses, f"Unexpected RPC request: {method}"
            result = self.responses[method]
        return {"jsonrpc": "2.0", "id": 1, "result": result}


@pytest.mark.parametrize(
    ("chain_id", "dynamic", "fees", "expected_fees"),
    [
        (56, False, {}, {"gasPrice": 7}),
        (56, False, {"gasPrice": 19}, {"gasPrice": 19}),
        (10, True, {}, {"maxFeePerGas": 125, "maxPriorityFeePerGas": 5}),
        (8453, True, {"gasPrice": 6_000_000}, {"gasPrice": 6_000_000}),
        (137, True, {"gasPrice": 19}, {"gasPrice": 19}),
        (10, True, {"gasPrice": 19}, {"gasPrice": 19}),
        (10, True, {"type": "0x1"}, {"gasPrice": 7}),
        (10, True, {"type": 2}, {"maxFeePerGas": 125, "maxPriorityFeePerGas": 5}),
        (
            10,
            True,
            {"maxFeePerGas": 150},
            {"maxFeePerGas": 150, "maxPriorityFeePerGas": 5},
        ),
        (
            10,
            True,
            {"maxPriorityFeePerGas": 8},
            {"maxFeePerGas": 128, "maxPriorityFeePerGas": 8},
        ),
        (
            10,
            True,
            {"maxFeePerGas": 150, "maxPriorityFeePerGas": 8},
            {"maxFeePerGas": 150, "maxPriorityFeePerGas": 8},
        ),
    ],
)
def test_send_transaction_selects_fee_type_before_signing(
    chain_id, dynamic, fees, expected_fees
):
    async def check():
        provider = CapturingProvider(chain_id, dynamic)
        chain = Chain(chain_id)
        await chain.connect_rpc(provider)
        provider.methods.clear()
        account = Account.from_key("01" * 32)
        transaction = {
            "to": "0x2222222222222222222222222222222222222222",
            "value": 0,
            "data": "0x1234",
            "gas": 25200,
            **fees,
        }
        tx_hash = await chain.send_transaction(cast(TxParams, transaction), account)
        assert tx_hash == HexBytes("0x" + "ab" * 32)
        assert len(provider.raw_transactions) == 1
        raw = provider.raw_transactions[0]
        assert EthAccount.recover_transaction(raw) == account.address
        if raw[0] in (1, 2):
            decoded = TypedTransaction.from_bytes(raw).as_dict()
            assert decoded["type"] == (1 if "gasPrice" in expected_fees else 2)
            assert decoded["chainId"] == chain_id
        else:
            decoded = Transaction.from_bytes(raw).as_dict()
            assert (decoded["v"] - 35) // 2 == chain_id
        if "gasPrice" in expected_fees:
            assert "maxFeePerGas" not in decoded
            assert "maxPriorityFeePerGas" not in decoded
        else:
            assert "gasPrice" not in decoded
        assert all(decoded[name] == value for name, value in expected_fees.items())
        assert decoded["gas"] == 25200
        assert decoded["data"] == HexBytes("0x1234")

    asyncio.run(check())


@pytest.mark.parametrize(
    "fees",
    [
        {"gasPrice": 19},
        {"maxFeePerGas": 150, "maxPriorityFeePerGas": 8},
        {"type": "0x1", "gasPrice": 19},
        {"type": 2, "maxFeePerGas": 150, "maxPriorityFeePerGas": 8},
    ],
)
def test_complete_supplied_fees_do_not_request_network_defaults(fees):
    async def check():
        provider = CapturingProvider(8453, True)
        chain = Chain(8453)
        await chain.connect_rpc(provider)
        provider.methods.clear()
        transaction = fees.copy()
        try:
            assert await fill_gas_price(chain, transaction) is transaction
            assert transaction == fees
            assert provider.methods == []
        finally:
            await chain.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "fees",
    [
        {"gasPrice": 19, "maxFeePerGas": 150},
        {"gasPrice": 19, "maxPriorityFeePerGas": 8},
        {"type": 2, "gasPrice": 19},
        {"type": "0x2", "gasPrice": 19},
        {"type": 1, "maxFeePerGas": 150},
    ],
)
def test_conflicting_fee_models_fail_before_rpc_or_signing(fees):
    async def check():
        provider = CapturingProvider(8453, True)
        chain = Chain(8453)
        await chain.connect_rpc(provider)
        provider.methods.clear()
        transaction = fees.copy()
        try:
            with pytest.raises(ValueError, match="fee|gasPrice"):
                await fill_gas_price(chain, transaction)
            assert transaction == fees
            assert provider.methods == []
            assert provider.raw_transactions == []
        finally:
            await chain.close()

    asyncio.run(check())
