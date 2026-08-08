from .generic import GenericProvider
from .apify import ApifyProvider


class ProviderFactory:

    @staticmethod
    def get(provider_slug: str):

        if provider_slug == "apify":
            return ApifyProvider()

        return GenericProvider()