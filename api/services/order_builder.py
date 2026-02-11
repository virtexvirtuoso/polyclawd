"""Pre-built order templates for fast execution.

Pre-computes order payloads so that when a signal fires, only the
final price/size needs to be filled in before submission.

Enhancement #5: Pre-sign and pre-build order payloads
- Order construction and signing pre-computed for watched markets
- On signal fire: fill price/size → submit (no full build from scratch)
- Pool of templates for fast-path markets
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class OrderTemplate:
    """Pre-built order template ready for price/size fill."""
    market_id: str
    token_id: str = ""
    side: str = "YES"  # YES or NO
    # Pre-computed fields
    funder: str = ""
    maker: str = ""
    taker: str = "0x0000000000000000000000000000000000000000"
    nonce: str = "0"
    # Template metadata
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0
    use_count: int = 0

    def fill(self, price: float, size: float) -> dict:
        """Fill template with final price/size and return order payload.

        This is the fast path — everything else is pre-computed.
        Returns a ready-to-submit CLOB order dict.
        """
        self.last_used = time.time()
        self.use_count += 1

        return {
            "order": {
                "tokenID": self.token_id,
                "makerAmount": str(int(size * 1e6)),  # USDC 6 decimals
                "takerAmount": str(int(size / price * 1e6)),
                "side": "BUY",
                "feeRateBps": "0",
                "nonce": self.nonce,
                "maker": self.maker,
                "taker": self.taker,
                "expiration": "0",
                "signatureType": 2,
            },
            "metadata": {
                "market_id": self.market_id,
                "side": self.side,
                "price": price,
                "size_usd": size,
                "template_age_ms": int((time.time() - self.created_at) * 1000),
            },
        }


class OrderTemplatePool:
    """Pool of pre-built order templates for watched markets.

    Templates are created/refreshed for markets on the hot watchlist
    so execution can happen in <100ms from signal.
    """

    def __init__(self, max_templates: int = 50):
        self._templates: dict[str, dict[str, OrderTemplate]] = {}  # market_id -> {side: template}
        self._lock = asyncio.Lock()
        self._max_templates = max_templates

    async def create_template(
        self,
        market_id: str,
        token_id: str,
        side: str = "YES",
        funder: str = "",
        maker: str = "",
    ) -> OrderTemplate:
        """Create or update a pre-built order template."""
        template = OrderTemplate(
            market_id=market_id,
            token_id=token_id,
            side=side.upper(),
            funder=funder,
            maker=maker,
            nonce=str(int(time.time() * 1000)),
        )
        async with self._lock:
            if market_id not in self._templates:
                self._templates[market_id] = {}
            self._templates[market_id][side.upper()] = template

            # Evict least-used templates if over limit
            if len(self._templates) > self._max_templates:
                await self._evict_stale()

        return template

    async def get_template(self, market_id: str, side: str = "YES") -> Optional[OrderTemplate]:
        """Get a pre-built template for fast execution."""
        async with self._lock:
            market_templates = self._templates.get(market_id, {})
            return market_templates.get(side.upper())

    async def fill_and_build(self, market_id: str, side: str, price: float, size: float) -> Optional[dict]:
        """One-shot: get template → fill → return order payload."""
        template = await self.get_template(market_id, side)
        if template:
            return template.fill(price, size)
        return None

    async def refresh_templates_for_watchlist(self, market_ids: list[str], token_lookup: dict = None):
        """Refresh templates for all markets on the hot watchlist.

        token_lookup: optional dict of {market_id: {yes_token_id, no_token_id}}
        """
        for mid in market_ids:
            tokens = (token_lookup or {}).get(mid, {})
            yes_token = tokens.get("yes_token_id", mid)
            no_token = tokens.get("no_token_id", mid)

            await self.create_template(mid, yes_token, "YES")
            await self.create_template(mid, no_token, "NO")

    async def _evict_stale(self):
        """Remove least-recently-used templates (called under lock)."""
        all_entries = []
        for mid, sides in self._templates.items():
            for side, tmpl in sides.items():
                all_entries.append((tmpl.last_used or tmpl.created_at, mid, side))

        all_entries.sort()
        # Remove oldest 20%
        to_remove = len(all_entries) - self._max_templates
        for _, mid, side in all_entries[:to_remove]:
            if mid in self._templates:
                self._templates[mid].pop(side, None)
                if not self._templates[mid]:
                    del self._templates[mid]

    async def get_status(self) -> dict:
        async with self._lock:
            total = sum(len(sides) for sides in self._templates.values())
            return {
                "markets_with_templates": len(self._templates),
                "total_templates": total,
                "max_templates": self._max_templates,
            }


# Global singleton
order_pool = OrderTemplatePool()
