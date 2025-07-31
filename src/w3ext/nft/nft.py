"""
NFT (Non-Fungible Token) support for w3ext.

This module provides comprehensive support for ERC-721 NFTs including:
- NFT collection management and interaction
- Individual NFT operations (transfer, metadata)
- Metadata fetching and parsing
- Integration with external data providers
- Batch operations for owned NFTs

The module supports both on-chain operations (via smart contracts) and
off-chain metadata operations (via IPFS/HTTP).

Example:
    >>> # Load NFT collection
    >>> collection = chain.load_nft721(contract_address, "My Collection")
    >>>
    >>> # Get user's NFTs
    >>> nfts = await collection.get_owned_by(user_address)
    >>>
    >>> # Work with individual NFT
    >>> nft = collection.get_item(token_id)
    >>> await nft.refresh_metadata()
    >>> print(nft.meta.name, nft.meta.attributes)
    >>>
    >>> # Transfer NFT
    >>> await nft.transfer(account, recipient_address)
"""

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
    """
    Exception raised for NFT-related errors.

    Inherits from ChainException and is raised when NFT operations
    fail, such as metadata fetching errors or invalid token operations.

    Example:
        >>> try:
        ...     await nft.refresh_metadata()
        ... except NftException as e:
        ...     print(f"Failed to fetch metadata: {e}")
    """
    pass


class Nft721Collection:
    """
    Represents an ERC-721 NFT collection.

    This class provides a high-level interface for interacting with ERC-721
    NFT collections, including querying balances, retrieving owned tokens,
    and accessing individual NFTs.

    Attributes:
        contract: Underlying Contract instance for blockchain operations
        name: Human-readable name of the collection

    Example:
        >>> collection = chain.load_nft721(contract_address, "CryptoPunks")
        >>> balance = await collection.get_balance(user_address)
        >>> nfts = await collection.get_owned_by(user_address)
        >>> nft = collection.get_item(token_id)
    """

    def __init__(self, contract: "Contract", name: str) -> None:
        """
        Initialize NFT collection.

        Args:
            contract: Contract instance for the NFT collection
            name: Human-readable name of the collection
        """
        self.contract = contract
        self.name = name

    @property
    def address(self) -> str:
        """Get the contract address of this collection."""
        return self.contract.address

    @property
    def chain_id(self) -> str:
        """Get the chain ID where this collection is deployed."""
        return self.contract.chain_id

    async def get_balance(self, address: str) -> int:
        """
        Get the number of NFTs owned by an address.

        Args:
            address: Owner address to check

        Returns:
            Number of NFTs owned by the address

        Example:
            >>> balance = await collection.get_balance("0x123...")
            >>> print(f"User owns {balance} NFTs")
        """
        return await self.contract.functions \
            .balanceOf(to_checksum_address(address)) \
            .call()

    async def get_owned_by(self, address: str,
                           provider: Optional["DataProvider"] = None) -> list["Nft721"]:
        """
        Get all NFTs owned by an address.

        Can use either an external data provider (faster) or on-chain
        enumeration (requires ERC-721 Enumerable extension).

        Args:
            address: Owner address to query
            provider: Optional external data provider for faster queries

        Returns:
            List of Nft721 instances owned by the address

        Example:
            >>> # Using on-chain enumeration
            >>> nfts = await collection.get_owned_by("0x123...")
            >>>
            >>> # Using external provider (faster)
            >>> nfts = await collection.get_owned_by("0x123...", alchemy_provider)
        """
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
        """
        Get an NFT instance by token ID.

        Args:
            _id: Token ID to retrieve

        Returns:
            Nft721 instance for the specified token

        Example:
            >>> nft = collection.get_item("123")
            >>> await nft.refresh_metadata()
            >>> print(nft.meta.name)
        """
        return Nft721(self, _id)

    def __getattr__(self, name) -> Any:
        """
        Delegate attribute access to the underlying contract.

        This allows direct access to contract functions and properties.
        """
        # let use token as a contract with predefined ABI and web3 instance
        return getattr(self.contract, name)


class Nft721:
    """
    Represents an individual ERC-721 NFT.

    This class provides operations for individual NFTs including metadata
    fetching, ownership queries, and transfers. Metadata is cached after
    the first fetch and can be refreshed as needed.

    Example:
        >>> nft = collection.get_item(123)
        >>> await nft.refresh_metadata()
        >>> print(f"NFT: {nft.meta.name}")
        >>> print(f"Attributes: {nft.meta.attributes}")
        >>>
        >>> # Transfer to another address
        >>> await nft.transfer(account, "0x456...")
    """

    def __init__(self, collection: Nft721Collection, _id: int, owner: Optional[str] = None) -> None:
        """
        Initialize NFT instance.

        Args:
            collection: The collection this NFT belongs to
            _id: Token ID
            owner: Optional known owner address
        """
        # The NFT collection this token belongs to
        self.collection: Nft721Collection = collection
        # Token ID of this NFT
        self.id: int = int(_id)
        # Cached owner address (if known)
        self._owner: Optional[str] = owner
        # Cached metadata (if fetched)
        self._meta: Optional[AttrDict[str, Any]] = None

    @classmethod
    def parse_attributes(cls, attrs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Parse NFT attributes from metadata format to a flat dictionary.

        Converts the standard NFT metadata attributes array into a more
        convenient flat dictionary format for easier access.

        Args:
            attrs: List of attribute dictionaries from NFT metadata

        Returns:
            Flattened dictionary of attributes

        Example:
            >>> attrs = [
            ...     {"trait_type": "Color", "value": "Blue"},
            ...     {"trait_type": "Rarity", "value": "Common"},
            ...     {"value": "Special"}  # No trait_type
            ... ]
            >>> parsed = Nft721.parse_attributes(attrs)
            >>> # Result: {"Color": "Blue", "Rarity": "Common", "Special": True}
        """
        prepared = {}
        for item in attrs:
            if 'trait_type' not in item:
                prepared[item['value']] = True
                continue
            prepared[item['trait_type']] = item['value']
        return prepared

    @property
    def meta(self) -> AttrDict:
        """
        Get cached metadata for this NFT.

        Returns:
            Cached metadata as AttrDict

        Raises:
            NftException: If metadata hasn't been fetched yet

        Example:
            >>> await nft.refresh_metadata()
            >>> print(nft.meta.name)
            >>> print(nft.meta.description)
            >>> print(nft.meta.attributes)
        """
        if self._meta is None:
            raise NftException("Metadata not found. Try to refresh it by `refresh_metadata` call")
        return self._meta

    async def get_owner(self: Self, force: bool = False) -> Optional[str]:
        """
        Get the current owner of this NFT.

        Args:
            force: If True, always query the blockchain even if owner is cached

        Returns:
            Owner address or None if not found

        Example:
            >>> owner = await nft.get_owner()
            >>> print(f"NFT owned by: {owner}")
            >>>
            >>> # Force refresh from blockchain
            >>> current_owner = await nft.get_owner(force=True)
        """
        if force or not self._owner:
            self._owner = await self.collection.functions.ownerOf(self.id).call()
        return self._owner

    async def transfer(self: Self, account: "Account", to: str, *, tx: Optional[TxParams] = None) -> None:
        """
        Transfer this NFT to another address.

        Uses safeTransferFrom to ensure the recipient can handle ERC-721 tokens.

        Args:
            account: Account to sign and send the transaction
            to: Recipient address
            tx: Optional transaction parameters

        Returns:
            Transaction hash

        Example:
            >>> # Simple transfer
            >>> tx_hash = await nft.transfer(account, "0x456...")
            >>>
            >>> # Transfer with custom gas settings
            >>> tx_hash = await nft.transfer(
            ...     account, "0x456...",
            ...     tx={"gas": 100000}
            ... )
        """
        return await self.collection.functions \
            .safeTransferFrom(account.address, to_checksum_address(to), self.id) \
            .transact(account, tx)

    async def refresh_metadata(self):
        """
        Fetch and cache metadata for this NFT.

        Retrieves the token URI from the contract and fetches the metadata
        from the URI (typically IPFS or HTTP). Parses attributes into a
        more convenient format.

        Raises:
            NftException: If metadata fetching fails

        Example:
            >>> await nft.refresh_metadata()
            >>> print(f"Name: {nft.meta.name}")
            >>> print(f"Description: {nft.meta.description}")
            >>> print(f"Image: {nft.meta.image}")
            >>> print(f"Attributes: {nft.meta.attributes}")
        """
        uri = await self.collection.functions.tokenURI(self.id).call()
        async with aiohttp.ClientSession() as session:
            async with session.get(uri) as resp:
                meta = await resp.json()
                meta["attributes"] = self.parse_attributes(meta.pop('attributes', {}))
        self._meta = AttrDict(meta)
