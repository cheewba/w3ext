import asyncio
from unittest.mock import AsyncMock

from hexbytes import HexBytes

from w3ext.account import Account
from w3ext.chain import Chain


def test_chain_metadata_and_transaction_scan_url():
    chain = Chain(1, currency="ETH", name="Ethereum", scan="https://scan.example/")
    tx_hash = HexBytes("0x" + "ab" * 32)

    assert chain.chain_id == "1"
    assert str(chain) == "Ethereum"
    assert chain.ETH is chain.currency
    assert chain.get_tx_scan(tx_hash) == f"https://scan.example/tx/0x{tx_hash.hex()}"

    chain.scan = None
    assert chain.get_tx_scan(tx_hash) is tx_hash
    assert str(Chain(10)) == "Chain#10"


def test_account_context_is_scoped_by_chain_and_restores_outer_account():
    first = Chain(1)
    second = Chain(10)
    alice = Account.from_key("0x" + "01" * 32)
    bob = Account.from_key("0x" + "02" * 32)

    with first.use_account(alice):
        assert first._get_active_accounts() == {alice.address: alice._acc}
        assert second._get_active_accounts() == {}
        with first.use_account(bob), second.use_account(bob):
            assert first._get_active_accounts() == {
                alice.address: alice._acc,
                bob.address: bob._acc,
            }
            assert second._get_active_accounts() == {bob.address: bob._acc}
        assert first._get_active_accounts() == {alice.address: alice._acc}
        assert second._get_active_accounts() == {}
    assert first._get_active_accounts() == {}


def test_erc20_abi_is_loaded_once_per_chain():
    async def check():
        chain = Chain(1)
        chain._load_abi = AsyncMock(return_value=[{"name": "balanceOf"}])

        first = await chain.erc20_abi()
        second = await chain.erc20_abi()
        assert first is second
        chain._load_abi.assert_awaited_once_with("erc20.json")

    asyncio.run(check())
