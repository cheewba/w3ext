from typing import List

from ..nft import Nft721Collection, Nft721


class DataProvider:
    async def get_nft721_owned_by(
        self,
        collection: "Nft721Collection",
        address: str
    ) -> List[Nft721]:
        raise NotImplementedError