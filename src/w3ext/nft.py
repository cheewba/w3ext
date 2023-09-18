# pylint: disable=no-name-in-module
import asyncio
from typing import Optional, Any, TYPE_CHECKING

from web3.types import TxParams

from .utils import to_checksum_address
if TYPE_CHECKING:
    from .account import Account
    from .contract import Contract


class Nft721Collection:
    def __init__(self, contract: Contract, name: str) -> None:
        self.contract = contract
        self.name = name

    async def get_balance(self, address: str) -> int:
        return await self.contract.functions \
            .balanceOf(to_checksum_address(address)) \
            .call()

    async def get_owned_by(self, address: str) -> list["Nft721"]:
        # only for the onces that support enumeration extension for ERC-721
        total = await self.get_balance(address)
        ids = await asyncio.gather(
            *[self.contract.functions.tokenOfOwnerByIndex(to_checksum_address(address), idx).call()
              for idx in range(total)]
        )
        return [Nft721(self, _id, address) for _id in ids]

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.contract, name)


class Nft721:
    _owner: Optional[str] = None

    def __init__(self, collection: Nft721Collection, _id: str, owner: Optional[str] = None) -> None:
        self.collection = collection
        self.id = _id
        self._owner = owner

    async def get_owner(self, force: bool = False) -> str:
        if force or not self._owner:
            self._owner = await self.collection.functions.ownerOf(self.id).call()
        return self._owner

    async def transfer(self, account: "Account", to: str, *, tx: Optional[TxParams] = None) -> None:
        return await self.collection.functions \
            .safeTransferFrom(account.address, to_checksum_address(to), self.id) \
            .transact(account, tx)
