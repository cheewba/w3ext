"""Public synthetic keys; characterization on unchanged b36a2cf8."""

import asyncio
import json

import pytest
from eth_account import Account as EthAccount
from eth_account.messages import encode_defunct, encode_typed_data
from hexbytes import HexBytes

from w3ext import Account

KEY = "01" * 32
TYPED = {
    "types": {
        "EIP712Domain": [{"name": "chainId", "type": "uint256"}],
        "Note": [{"name": "text", "type": "string"}],
    },
    "primaryType": "Note",
    "domain": {"chainId": 1},
    "message": {"text": "public test"},
}


def test_old_key_access_normalization_and_sync_transaction_signature():
    a, b = Account.from_key(KEY), Account.from_key("0x" + KEY)
    assert a.address == b.address == EthAccount.from_key(KEY).address
    assert a.key == b.key == HexBytes(KEY)
    signed = a.sign_transaction(
        {
            "chainId": 1,
            "nonce": 2,
            "gasPrice": 7,
            "gas": 21000,
            "to": "0x" + "22" * 20,
            "value": 3,
        }
    )
    assert EthAccount.recover_transaction(signed.raw_transaction) == a.address
    assert str(a) == a.address
    with pytest.raises(ValueError):
        Account.from_key("invalid")


@pytest.mark.parametrize(
    "data,encoded",
    [
        ("hello", encode_defunct(text="hello")),
        (b"hello", encode_defunct(b"hello")),
        ("0x68656c6c6f", encode_defunct(hexstr="0x68656c6c6f")),
        (TYPED, encode_typed_data(full_message=TYPED)),
        (json.dumps(TYPED), encode_typed_data(full_message=TYPED)),
        (json.dumps(TYPED).encode(), encode_typed_data(full_message=TYPED)),
    ],
)
def test_personal_and_typed_signatures_recover_offline(data, encoded):
    account = Account.from_key(KEY)
    full = asyncio.run(account.sign(data, hex_only=False))
    assert (
        EthAccount.recover_message(encoded, signature=full.signature) == account.address
    )
    assert asyncio.run(account.sign(data)) == full.signature
    assert isinstance(full.signature, HexBytes)


def test_sign_rejects_unsupported_data():
    with pytest.raises(TypeError, match="Signing data"):
        asyncio.run(Account.from_key(KEY).sign(42))
