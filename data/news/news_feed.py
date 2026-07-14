"""
NewsFeed — provides economic calendar events to the orchestrator.
Sources: ForexFactory RSS (free), NewsAPI (production).
"""
from __future__ import annotations
from datetime import datetime, timedelta
from loguru import logger

from swarm_trading.core.models import NewsEvent, NewsImpact, Symbol


SYMBOL_CURRENCY_MAP = {
    "XAUUSD": ["USD", "XAU"],
    "PLTR":   ["USD"],
    "NAS100": ["USD"],
    "US100":  ["USD"],
    "OIL":    ["USD"],
}

# Static high-impact events as fallback/demo
DEMO_EVENTS: list[dict] = [
    {"title": "FOMC Rate Decision", "impact": NewsImpact.HIGH, "currency": "USD"},
    {"title": "Non-Farm Payrolls",   "impact": NewsImpact.HIGH, "currency": "USD"},
    {"title": "CPI m/m",             "impact": NewsImpact.HIGH, "currency": "USD"},
    {"title": "GDP q/q",             "impact": NewsImpact.MEDIUM, "currency": "USD"},
]


class NewsFeed:
    def __init__(self, backend: str = "demo"):
        self.backend = backend
        self._events: list[NewsEvent] = []
        self._last_fetch: datetime | None = None

    async def get_upcoming(self, symbol: Symbol, horizon_hours: int = 2) -> list[NewsEvent]:
        await self._refresh_if_stale()
        currencies = SYMBOL_CURRENCY_MAP.get(symbol.value, ["USD"])
        now = datetime.utcnow()
        return [
            e for e in self._events
            if e.currency in currencies
            and (e.timestamp - now).total_seconds() <= horizon_hours * 3600
        ]

    async def is_blackout(self, symbol: Symbol, blackout_min: int = 5) -> bool:
        events = await self.get_upcoming(symbol)
        now = datetime.utcnow()
        for e in events:
            if e.impact != NewsImpact.HIGH:
                continue
            delta_min = abs((e.timestamp - now).total_seconds() / 60)
            if delta_min <= blackout_min:
                return True
        return False

    async def _refresh_if_stale(self) -> None:
        if self._last_fetch and (datetime.utcnow() - self._last_fetch).seconds < 300:
            return
        if self.backend == "demo":
            self._load_demo_events()
        elif self.backend == "forexfactory":
            await self._fetch_forexfactory()
        self._last_fetch = datetime.utcnow()

    def _load_demo_events(self) -> None:
        now = datetime.utcnow()
        self._events = [
            NewsEvent(
                timestamp=now + timedelta(hours=i + 1),
                title=ev["title"],
                impact=ev["impact"],
                currency=ev["currency"],
            )
            for i, ev in enumerate(DEMO_EVENTS)
        ]

    async def _fetch_forexfactory(self) -> None:
        """Parse ForexFactory calendar RSS feed."""
        try:
            import feedparser
            feed = feedparser.parse("https://www.forexfactory.com/ffcal_week_this.xml")
            events = []
            for entry in feed.entries:
                events.append(NewsEvent(
                    timestamp=datetime.utcnow(),  # parse entry.published properly
                    title=entry.get("title", ""),
                    impact=NewsImpact.HIGH,
                    currency=entry.get("ff_country", "USD"),
                ))
            self._events = events
        except Exception as e:
            logger.warning(f"[NewsFeed] ForexFactory fetch failed: {e}")
            self._load_demo_events()
