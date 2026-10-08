"""Real signing of raw transactions with an offline RPC provider."""

import asyncio

import pytest
from eth_account import Account as EthAccount
from eth_account._utils.legacy_transactions import Transaction
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3.providers import AsyncBaseProvider

from w3ext import Account, Chain


class CapturingProvider(AsyncBaseProvider):
    def __init__(self, chain_id, dynamic):
        super().__init__()
        self.raw_transactions = []
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

    async def make_request(self, method, params):
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
        account = Account.from_key("01" * 32)
        transaction = {
            "to": "0x2222222222222222222222222222222222222222",
            "value": 0,
            "data": "0x1234",
            "gas": 25200,
            **fees,
        }
        tx_hash = await chain.send_transaction(transaction, account)
        assert tx_hash == HexBytes("0x" + "ab" * 32)
        assert len(provider.raw_transactions) == 1
        raw = provider.raw_transactions[0]
        assert EthAccount.recover_transaction(raw) == account.address
        if dynamic:
            decoded = TypedTransaction.from_bytes(raw).as_dict()
            assert decoded["type"] == 2
            assert decoded["chainId"] == chain_id
            assert "gasPrice" not in decoded
        else:
            decoded = Transaction.from_bytes(raw).as_dict()
            assert (decoded["v"] - 35) // 2 == chain_id
            assert "maxFeePerGas" not in decoded
            assert "maxPriorityFeePerGas" not in decoded
        assert all(decoded[name] == value for name, value in expected_fees.items())
        assert decoded["gas"] == 25200
        assert decoded["data"] == HexBytes("0x1234")

    asyncio.run(check())
