import random
from typing import Dict, Optional

from eth_abi import encode as abi_encode

from ..types import StateOverride
from ..utils import to_checksum_address, keccak256, keccak
from ..token import Token, TokenAmount


def _allowance_slot(owner: str, spender: str, P: int) -> str:
    """
    Storage slot for: mapping(address => mapping(address => uint256)) allowances at base slot P.
    slot = keccak(spender, keccak(owner, P))
    """
    owner = to_checksum_address(owner)
    spender = to_checksum_address(spender)
    inner = keccak(abi_encode(["address", "uint256"], [owner, P]))
    slot = keccak(abi_encode(["address", "bytes32"], [spender, inner]))
    return "0x" + slot.hex()


def _sentinel_for(P: int, seed: int) -> str:
    """Distinct 32-byte sentinel for a given P (very low collision chance)."""
    h = keccak256(f"sentinel|{seed}|{P}")
    return "0x" + h[2:].rjust(64, "0")


async def get_allowance_state(
    token: Token,
    owner: str,
    spender: str,
    amount: Optional[TokenAmount] = None,
) -> StateOverride:
    """
    Build a StateOverride for a single token so that allowance[owner][spender] equals given amount.
    If amount is None, use token.MAX_AMOUNT (unlimited).

    How it works:
    - Probes ERC20 allowance base slot P by writing unique sentinels to candidate slots via state_override on eth_call
    - Identifies which sentinel is read back and confirms by writing a new sentinel only to the discovered slot
    - Uses Chain.use_batch for the eth_call waves when possible
    - Returns { token_address: { stateDiff: { computed_slot: value } } } suitable for eth_call/eth_estimateGas
    """
    token_addr = token.address
    owner = to_checksum_address(owner)
    spender = to_checksum_address(spender)

    # 32-byte hex for desired allowance value
    value_hex = token.MAX_AMOUNT if amount is None else (
        "0x" + int(int(amount.amount)).to_bytes(32, "big").hex()
    )

    # Probe base slot P in [0..63]
    p_candidates = range(0, 8)
    seed = random.getrandbits(32)

    # 1) Write distinct sentinel to every candidate slot and read back once
    slots: Dict[str, str] = {}
    sentinel_map: Dict[int, str] = {}
    for P in p_candidates:
        s = _sentinel_for(P, seed)
        sentinel_map[P] = s
        slot = _allowance_slot(owner, spender, P)
        slots[slot] = s

    override_probe = {token_addr: {"stateDiff": slots}}
    read_val = await token.contract.functions.allowance(owner, spender).call(
        block_identifier="latest",
        state_override=override_probe
    )

    # 2) Identify sentinel and confirm with a new sentinel only in that slot
    discovered_P: Optional[int] = None
    for P, s in sentinel_map.items():
        if read_val == int(s, 16):
            new_seed = seed ^ 0xA5A5_5A5A
            s2 = _sentinel_for(P, new_seed)
            override_confirm = {token_addr: {"stateDiff": {_allowance_slot(owner, spender, P): s2}}}
            read_val2 = await token.contract.functions.allowance(owner, spender).call(
                block_identifier="latest",
                state_override=override_confirm
            )
            discovered_P = P if read_val2 == int(s2, 16) else None
            break

    # 3) Build final override with requested value (fallback to common guesses if discovery failed)
    out_slots: Dict[str, str] = {}
    if discovered_P is not None:
        out_slots[_allowance_slot(owner, spender, discovered_P)] = value_hex
    else:
        return {}

    return {token_addr: {"stateDiff": out_slots}}
