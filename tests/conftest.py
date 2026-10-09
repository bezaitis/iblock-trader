import copy
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gies_bot.models import Ctx, FetchError
from gies_bot.store import Store

CFG = {
    "timezone": "America/Chicago", "stale_minutes": 120, "stale_alert_hours": 2,
    "min_edge": 0.10, "min_edge_low_confidence": 0.15, "take_profit_buffer": 0.08,
    "alert_edge_change": 0.05, "alert_repeat_hours": 6, "daily_summary_hour": 8, "markets": {},
    "trading": {"buy_buffer": 0.05, "max_order_gies": 100, "min_order_gies": 5, "max_orders_per_cycle": 4,
                "max_per_market_pct": 0.10, "max_total_exposure_pct": 0.40, "max_price_impact": 0.13,
                "daily_loss_limit_pct": 0.15},
}


class FakeCtx(Ctx):
    """Serves canned responses: routes maps a URL substring to a body, a dict/list, or (body, age)."""

    def __init__(self, routes, now, cfg=None):
        self.cfg, self.store, self.now, self.routes = copy.deepcopy(cfg or CFG), Store(":memory:"), now, routes

    def get_text(self, url, max_age):
        for fragment, body in self.routes.items():
            if fragment in url:
                body, age = body if isinstance(body, tuple) else (body, 0.0)
                return (body if isinstance(body, str) else json.dumps(body)), age
        raise FetchError(url)


@pytest.fixture
def at():
    return lambda *parts: datetime(*parts, tzinfo=ZoneInfo("America/Chicago"))
