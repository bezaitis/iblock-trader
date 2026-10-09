import httpx
import pytest

from conftest import CFG, FakeCtx
from gies_bot import alerts
from gies_bot.cycle import Cycle
from gies_bot.executor import LIVE_ENV, Book, Order, check_quote, plan, resolve_mode
from gies_bot.platform import Platform


def market(mid, yes):
    return {"id": mid, "title": f"Market {mid}", "status": "open", "outcome": None, "yesPrice": yes,
            "noPrice": 1 - yes, "tradingFeeRate": 0.02}


def position(mid, outcome, tokens, cost):
    return {"marketId": mid, "outcome": outcome, "tokenAmount": tokens, "avgCostBasis": cost,
            "currentValue": tokens * cost, "unrealizedPnl": 0}


class FakePlatform:
    """Quotes move the price 1 point per 100 GIES and cost a 2% fee; impact is 0.1% per GIES."""

    def __init__(self, markets, positions=(), balance=1000, impact_per_gies=0.1, trade_error=None, warnings=()):
        self._markets, self._positions, self.balance = markets, list(positions), balance
        self.impact_per_gies, self.trade_error, self.warnings = impact_per_gies, trade_error, list(warnings)
        self.quotes, self.trades = [], []

    def markets(self):
        return self._markets

    def positions(self):
        return self._positions

    def me(self):
        return {"giesBalance": self.balance}

    def quote(self, order):
        self.quotes.append(order)
        m = next(m for m in self._markets if m["id"] == order["marketId"])
        spot = m["yesPrice"] if order["outcome"] == "yes" else m["noPrice"]
        buy = order["side"] == "buy"
        gies = order["amount"] if buy else order["amount"] * spot
        fill = spot * (1.02 if buy else 0.98)
        after = spot + (gies / 10000 if buy else -gies / 10000)
        yes_after = after if order["outcome"] == "yes" else 1 - after
        return {"tokenAmount": gies / fill, "giesCost": gies if buy else -gies, "pricePerToken": fill,
                "priceImpact": gies * self.impact_per_gies, "newYesPrice": yes_after, "newNoPrice": 1 - yes_after,
                "safeguardWarnings": self.warnings, "isBlocked": False}

    def trade(self, order):
        self.trades.append(order)
        if self.trade_error:
            raise self.trade_error
        q = self.quote(order)
        self.quotes.pop()
        return {"trade": {"tokenAmount": q["tokenAmount"], "giesCost": abs(q["giesCost"]), "pricePerToken": q["pricePerToken"]},
                "newYesPrice": q["newYesPrice"], "newGiesBalance": self.balance - q["giesCost"]}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(alerts, "send", lambda level, title, body, tag=None: out.append((level, title, body)))
    return out


def run(ctx, platform, tmp_path, mode="live", p=None, trade=True):
    p = p or {7: 0.68}
    ctx.cfg["markets"] = {mid: {"name": f"M{mid}", "trade": trade, "model": "manual",
                                "params": {"p_yes": v, "confidence": "high"}} for mid, v in p.items()}
    cycle = Cycle(ctx.cfg, ctx.store, platform, ctx, mode, tmp_path / "STOP")
    cycle.run()
    return ctx.store.get("state")["orders"]


def row(yes=0.40, p_yes=0.68, holdings=(), **extra):
    fair, price, side = (p_yes, yes, "YES") if p_yes > yes else (1 - p_yes, 1 - yes, "NO")
    return {"id": 7, "name": "M7", "yes": yes, "no": 1 - yes, "data_ok": True, "p_yes": p_yes, "side": side,
            "fair": fair, "price": price, "edge": fair - price * 1.02, "actionable": True,
            "holdings": list(holdings), **extra}


def held(outcome, tokens, cost):
    return {"outcome": outcome, "tokens": tokens, "cost": cost}


def test_live_needs_the_environment_phrase(monkeypatch):
    monkeypatch.delenv(LIVE_ENV, raising=False)
    assert resolve_mode({"mode": "live"})[0] == "dry_run"
    monkeypatch.setenv(LIVE_ENV, "yes")
    assert resolve_mode({"mode": "live"})[0] == "dry_run"
    monkeypatch.setenv(LIVE_ENV, "I_UNDERSTAND")
    assert resolve_mode({"mode": "live"}) == ("live", None)
    assert resolve_mode({"mode": "yolo"})[0] == "monitor"
    assert resolve_mode({}) == ("monitor", None)


def test_plan_buys_the_cheap_side_within_every_cap():
    order, _ = plan(row(), CFG, Book(cash=1000, account_value=1000, basis={}))
    assert (order.side, order.outcome, order.amount, order.amount_type) == ("buy", "yes", 100, "gies")
    assert order.limit == pytest.approx(0.63)
    assert plan(row(), CFG, Book(1000, 1000, {7: 70}))[0].amount == 30          # 10% per market
    assert plan(row(), CFG, Book(1000, 1000, {8: 90, 9: 90, 10: 90, 11: 90}))[0].amount == 40  # 40% overall
    assert plan(row(), CFG, Book(20, 1000, {}))[0].amount == 20                 # cash
    assert plan(row(), CFG, Book(1000, 1000, {7: 98}))[0] is None               # under the minimum order
    assert plan(row(yes=0.90), CFG, Book(1000, 1000, {}))[0].outcome == "no"


def test_plan_declines_without_edge_data_or_when_holding_the_other_side():
    book = Book(1000, 1000, {})
    assert plan(row(actionable=False), CFG, book) == (None, "no edge")
    assert plan(row(data_ok=False), CFG, book) == (None, "no fair value")
    # A cheap side means the opposite holding is rich, so it is sold first...
    assert plan(row(holdings=[held("NO", 50, 0.5)]), CFG, book)[0].side == "sell"
    # ...and if the sell rule is set looser than the buy rule, the bot still never holds both sides.
    patient = {**CFG, "take_profit_buffer": 0.5}
    assert plan(row(holdings=[held("NO", 50, 0.5)]), patient, book) == (None, "holding the other side")


def test_plan_sells_a_rich_holding_before_anything_else():
    order, _ = plan(row(yes=0.80, holdings=[held("YES", 500, 0.31)]), CFG, Book(1000, 1000, {7: 155}))
    assert (order.side, order.outcome, order.amount_type) == ("sell", "yes", "tokens")
    assert order.amount == pytest.approx(125) and order.limit == 0.68  # 100 GIES worth, not below fair
    assert plan(row(yes=0.80, holdings=[held("YES", 20, 0.31)]), CFG, Book(1000, 1000, {}))[0].amount == 20
    assert plan(row(yes=0.74, holdings=[held("YES", 500, 0.31)]), CFG, Book(1000, 1000, {7: 155}))[0] is None


def test_check_quote_rejections():
    buy, sell = Order(7, "buy", "yes", 100, "gies", 0.68, 0.63), Order(7, "sell", "yes", 100, "tokens", 0.68, 0.68)
    good = {"tokenAmount": 240, "pricePerToken": 0.41, "priceImpact": 5.0, "newYesPrice": 0.45, "newNoPrice": 0.55,
            "safeguardWarnings": [], "isBlocked": False}
    assert check_quote(buy, good, CFG) is None
    assert "block" in check_quote(buy, {**good, "isBlocked": True}, CFG)
    assert check_quote(buy, {**good, "safeguardWarnings": ["Elevated price impact: 8.8%"]}, CFG) is None
    assert "impact" in check_quote(buy, {**good, "priceImpact": 13.5}, CFG)
    assert "past the 63¢ limit" in check_quote(buy, {**good, "newYesPrice": 0.64}, CFG)
    assert "past the 63¢ limit" in check_quote(buy, {**good, "pricePerToken": 0.64}, CFG)
    assert "malformed" in check_quote(buy, {**good, "pricePerToken": None}, CFG)
    assert "malformed" in check_quote(buy, {**good, "tokenAmount": 0}, CFG)
    assert check_quote(sell, {**good, "pricePerToken": 0.78, "newYesPrice": 0.79}, CFG) is None
    assert "under fair value" in check_quote(sell, {**good, "pricePerToken": 0.67, "newYesPrice": 0.70}, CFG)
    assert "under fair value" in check_quote(sell, {**good, "pricePerToken": 0.70, "newYesPrice": 0.67}, CFG)


def test_dry_run_quotes_records_and_alerts_but_never_trades(at, sent, tmp_path):
    ctx, platform = FakeCtx({}, at(2026, 10, 8, 7)), FakePlatform([market(7, 0.40)])
    orders = run(ctx, platform, tmp_path, mode="dry_run")
    run(ctx, platform, tmp_path, mode="dry_run")
    assert platform.trades == [] and len(platform.quotes) == 2
    assert orders[0]["status"] == "dry_run"
    assert [t for _, t, _ in sent].count("M7: would buy YES (dry run)") == 1
    statuses = [r[0] for r in ctx.store.db.execute("SELECT status FROM orders ORDER BY rowid")]
    assert statuses == ["quoted", "dry_run"] * 2


def test_live_places_one_order_and_reports_it(at, sent, tmp_path):
    ctx, platform = FakeCtx({}, at(2026, 10, 8, 7)), FakePlatform([market(7, 0.40)])
    orders = run(ctx, platform, tmp_path)
    assert platform.trades == [{"marketId": 7, "side": "buy", "outcome": "yes", "amount": 100, "amountType": "gies"}]
    assert orders[0]["status"] == "filled"
    title, body = next((t, b) for _, t, b in sent if t == "M7: bought YES")
    assert "Buy 245 YES at 41¢ (100 GIES)" in body and "Model says 68%" in body


def test_live_sells_a_rich_holding(at, sent, tmp_path):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    platform = FakePlatform([market(7, 0.80)], [position(7, "yes", 500, 0.31)])
    run(ctx, platform, tmp_path)
    assert platform.trades == [{"marketId": 7, "side": "sell", "outcome": "yes", "amount": 125.0, "amountType": "tokens"}]
    assert any(t == "M7: sold YES" for _, t, _ in sent)


def test_order_is_halved_until_the_quote_passes(at, sent, tmp_path):
    ctx, platform = FakeCtx({}, at(2026, 10, 8, 7)), FakePlatform([market(7, 0.40)], impact_per_gies=0.4)
    run(ctx, platform, tmp_path)
    assert [q["amount"] for q in platform.quotes] == [100, 50, 25]  # 40%, 20%, then 10% impact
    assert platform.trades[0]["amount"] == 25


def test_gives_up_when_no_size_passes_but_trades_through_a_platform_warning(at, sent, tmp_path):
    ctx, platform = FakeCtx({}, at(2026, 10, 8, 7)), FakePlatform([market(7, 0.40)], impact_per_gies=10)
    assert run(ctx, platform, tmp_path)[0]["status"] == "none"
    assert len(platform.quotes) == 4 and platform.trades == []
    warned = FakePlatform([market(7, 0.40)], warnings=["Elevated price impact: 8.8%"])
    assert run(FakeCtx({}, at(2026, 10, 8, 7)), warned, tmp_path)[0]["status"] == "filled"
    assert len(warned.quotes) == 1 and warned.trades[0]["amount"] == 100
    assert "Platform note: Elevated price impact: 8.8%." in next(b for _, t, b in sent if t == "M7: bought YES")


def test_markets_without_the_trade_flag_are_left_alone(at, sent, tmp_path):
    ctx, platform = FakeCtx({}, at(2026, 10, 8, 7)), FakePlatform([market(7, 0.40)])
    assert run(ctx, platform, tmp_path, trade=False) == []
    assert platform.quotes == [] and platform.trades == []


def test_monitor_mode_never_reaches_the_executor(at, sent, tmp_path):
    ctx, platform = FakeCtx({}, at(2026, 10, 8, 7)), FakePlatform([market(7, 0.40)])
    assert run(ctx, platform, tmp_path, mode="monitor") == []
    assert platform.quotes == []


def test_stop_file_is_a_kill_switch(at, sent, tmp_path):
    (tmp_path / "STOP").touch()
    ctx, platform = FakeCtx({}, at(2026, 10, 8, 7)), FakePlatform([market(7, 0.40)])
    assert run(ctx, platform, tmp_path)[0]["status"] == "halted"
    assert platform.quotes == [] and platform.trades == []


def test_loss_limit_halts_until_resumed(at, sent, tmp_path):
    ctx, platform = FakeCtx({}, at(2026, 10, 8, 7)), FakePlatform([market(7, 0.66)], balance=1000)
    run(ctx, platform, tmp_path)
    platform.balance, platform._markets = 840, [market(7, 0.40)]
    assert run(ctx, platform, tmp_path)[0]["status"] == "halted"
    platform.balance = 1000
    assert run(ctx, platform, tmp_path)[0]["status"] == "halted"
    assert platform.trades == [] and [t for _, t, _ in sent].count("Trading halted: loss limit") == 1
    ctx.store.set("trading_halted", None)
    ctx.store.set("account_values", [])
    assert run(ctx, platform, tmp_path)[0]["status"] == "filled"


def test_unanswered_trade_is_never_retried_and_ends_the_cycle(at, sent, tmp_path):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    platform = FakePlatform([market(7, 0.40), market(8, 0.40)], trade_error=httpx.ReadTimeout("timed out"))
    orders = run(ctx, platform, tmp_path, p={7: 0.68, 8: 0.68})
    assert len(platform.trades) == 1 and len(orders) == 1 and orders[0]["status"] == "error"
    assert any(t == "M7: order status unknown" for _, t, _ in sent)


def test_refused_trade_is_reported_and_other_markets_continue(at, sent, tmp_path):
    response = httpx.Response(400, text='{"error":"Insufficient balance"}', request=httpx.Request("POST", "http://x"))
    refused = httpx.HTTPStatusError("400", request=response.request, response=response)
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    platform = FakePlatform([market(7, 0.40), market(8, 0.40)], trade_error=refused)
    orders = run(ctx, platform, tmp_path, p={7: 0.68, 8: 0.68})
    assert len(platform.trades) == 2 and [o["status"] for o in orders] == ["error", "error"]
    assert "Insufficient balance" in next(b for _, t, b in sent if t == "M7: order refused")


def test_caps_hold_across_markets_within_one_cycle(at, sent, tmp_path):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    ctx.cfg["trading"]["max_total_exposure_pct"] = 0.15
    platform = FakePlatform([market(7, 0.40), market(8, 0.40)])
    run(ctx, platform, tmp_path, p={7: 0.68, 8: 0.68})
    assert [t["amount"] for t in platform.trades] == [100, 50]


def test_client_refuses_to_trade_unless_built_for_it_and_sends_a_trade_once():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(500)

    def client():
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x")

    with pytest.raises(RuntimeError):
        Platform("t", client(), min_interval=0).trade({})
    assert calls == []
    with pytest.raises(httpx.HTTPStatusError):
        Platform("t", client(), min_interval=0, allow_trading=True).trade({})
    assert calls == ["/api/trades"]
