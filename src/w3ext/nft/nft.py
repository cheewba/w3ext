# pylint: disable=no-name-in-module
import asyncio
from typing import Optional, Any, TYPE_CHECKING, Self, List, Dict

import aiohttp
from web3.types import TxParams

from ..utils import to_checksum_address, AttrDict
from ..exceptions import ChainException
if TYPE_CHECKING:
    from .providers import DataProvider
    from ..account import Account
    from ..contract import Contract


class NftException(ChainException):
    pass


class Nft721Collection:
    def __init__(self, contract: "Contract", name: str) -> None:
        self.contract = contract
        self.name = name

    @property
    def address(self) -> str:
        return self.contract.address

    @property
    def chain_id(self) -> str:
        return self.contract.chain_id

    async def get_balance(self, address: str) -> int:
        return await self.contract.functions \
            .balanceOf(to_checksum_address(address)) \
            .call()

    async def get_owned_by(self, address: str,
                           provider: Optional["DataProvider"] = None) -> list["Nft721"]:
        if provider is not None:
            return await provider.get_nft721_owned_by(self, address)

        # only for the ones that support enumeration extension for ERC-721
        total = await self.get_balance(address)
        ids = await asyncio.gather(
            *[self.contract.functions.tokenOfOwnerByIndex(to_checksum_address(address), idx).call()
              for idx in range(total)]
        )
        return [Nft721(self, _id, address) for _id in ids]

    def get_item(self, _id: str) -> "Nft721":
        return Nft721(self, _id)

    def __getattr__(self, name) -> Any:
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.contract, name)


class Nft721:
    _owner: Optional[str] = None
    _meta: Optional[AttrDict[str, Any]] = None

    def __init__(self, collection: Nft721Collection, _id: int, owner: Optional[str] = None) -> None:
        self.collection = collection
        self.id = int(_id)
        self._owner = owner

    @classmethod
    def parse_attributes(self, attrs: List[Dict[str, Any]]) -> Dict[str, Any]:
        prepared = {}
        for item in attrs:
            if 'trait_type' not in item:
                prepared[item['value']] = True
                continue
            prepared[item['trait_type']] = item['value']
        return prepared

    @property
    def meta(self) -> AttrDict:
        if self._meta is None:
            raise NftException("Metadata not found. Try to refresh it by `refresh_metadata` call")
        return self._meta

    async def get_owner(self: Self, force: bool = False) -> Optional[str]:
        if force or not self._owner:
            self._owner = await self.collection.functions.ownerOf(self.id).call()
        return self._owner

    async def transfer(self: Self, account: "Account", to: str, *, tx: Optional[TxParams] = None) -> None:
        return await self.collection.functions \
            .safeTransferFrom(account.address, to_checksum_address(to), self.id) \
            .transact(account, tx)

    async def refresh_metadata(self):
        uri = await self.collection.functions.tokenURI(self.id).call()
        async with aiohttp.ClientSession() as session:
            async with session.get(uri) as resp:
                meta = await resp.json()
                meta["attributes"] = self.parse_attributes(meta.pop('attributes', {}))
        self._meta = AttrDict(meta)
