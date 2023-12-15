from typing import List

import aiohttp

from ..nft import Nft721Collection, Nft721, NftException
from .provider import DataProvider

GET_ACCOUNT_NFTS = "https://api.opensea.io/api/v2/chain/{chain}/account/{address}/nfts"
GET_COLLECTION_INFO = "https://api.opensea.io/api/v2/chain/{chain}/contract/{address}"

NETWORKS = {
    '1': 'ethereum',
    '10': 'optimism',
    '56': 'bsc',
    '137': 'matic',
    '8453': 'base',
    '42161': 'arbitrum',
}


class OpenseaProvider(DataProvider):
    def __init__(self, api_key: str) -> None:
        super().__init__()
        self._api_key = api_key

    async def get_nft721_owned_by(
        self,
        collection: "Nft721Collection",
        address: str
    ) -> List[Nft721]:

        headers = { 'x-api-key': self._api_key }
        async with aiohttp.ClientSession() as session:
            network = NETWORKS.get(collection.chain_id)
            if network is None:
                raise NftException(f"Opensea doesn't support {collection.chain_id} chain")

            c_url = GET_COLLECTION_INFO.format(chain=network, address=collection.address)
            async with session.get(c_url, headers=headers) as resp:
                if not resp.status == 200:
                    raise NftException(f"Opensea can't find {collection} collection")
                data = await resp.json()
                collection_name = data["collection"]

            url = GET_ACCOUNT_NFTS.format(chain=network, address=address)
            result, _next, _limit = [], None, 200
            while True:
                url += f"?collection={collection_name}&limit={_limit}"
                if _next is not None:
                    url += f"&next={_next}"
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json()
                    for item in data['nfts']:
                        result.append(collection.get_item(item['identifier']))

                if not (_next := data.get('next')):
                    break

        return result