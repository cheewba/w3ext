from typing import List

import aiohttp

from ..nft import Nft721Collection, Nft721, NftException
from .provider import DataProvider

ALCHEMY_NFT_API = "https://{network}.g.alchemy.com/nft/v2/{alchemy_key}/getNFTs/"

NETWORKS = {
    '1': 'eth-mainnet',
    '10': 'opt-mainnet',
    '137': 'polygon-mainnet',
    '8453': 'base-mainnet',
    '42161': 'arb-mainnet',
}


class AlchemyProvider(DataProvider):
    def __init__(self, api_key: str) -> None:
        super().__init__()
        self._api_key = api_key

    async def get_nft721_owned_by(
        self,
        collection: "Nft721Collection",
        address: str
    ) -> List[Nft721]:

        async with aiohttp.ClientSession() as session:
            network = NETWORKS.get(collection.chain_id)
            if network is None:
                raise NftException(f"Alchemy doesn't support {collection.chain_id} chain")

            url = ALCHEMY_NFT_API.format(alchemy_key=self._api_key, network=network)
            url += f"?owner={address}&contractAddresses[]={collection.address}"
            async with session.get(url) as resp:
                data = await resp.json()

        return [collection.get_item(item['tokenId']) for item in data['ownedNfts']]