"""Pick the chain source from DATA_MODE.

mock -> MockChainProvider (synthetic, no network).
live -> SchwabChainClient backed by the shared token.

Both expose the same tiny interface used by the server:
    get_expirations(ticker) -> List[str]
    get_chain(ticker, expiry, strike_count=None) -> (List[StrikeRow], spot)
"""
from __future__ import annotations

import logging

from app.config import settings

log = logging.getLogger("factory")


def build_provider():
    if settings.live:
        from app.providers.schwab import SchwabChainClient
        from app.providers.token import SharedTokenProvider

        token = SharedTokenProvider()
        if not settings.token_share_key:
            log.warning("DATA_MODE=live but SCHWAB_TOKEN_SHARE_KEY (or MM_API_KEY) is unset — "
                        "Schwab chain calls will fail until the shared token is configured.")
        log.info("Chain source: LIVE Schwab (shared token via %s)", settings.token_url)
        return SchwabChainClient(token_fn=token.get_token, invalidate_fn=token.invalidate)

    from app.providers.mock import MockChainProvider
    log.info("Chain source: MOCK (synthetic chain)")
    return MockChainProvider()
