from ..nft import Nft721, Nft721Collection


class DataProvider:
    async def get_nft721_owned_by(
        self, collection: "Nft721Collection", address: str
    ) -> list[Nft721]:
        raise NotImplementedError
