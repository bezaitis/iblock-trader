from datetime import timedelta

import pytest

from conftest import CFG, FakeCtx
from gies_bot import alerts
from gies_bot.cycle import Cycle, best_edge
from gies_bot.platform import AuthError


def market(mid, yes, status="open"):
    return {"id": mid, "title": f"Market {mid}", "status": status, "outcome": None, "yesPrice": yes,
            "noPrice": 1 - yes, "tradingFeeRate": 0.02, "resolutionCriteria": "criteria text"}


class FakePlatform:
    def __init__(self, markets, positions=(), fail=False):
        self._markets, self._positions, self.fail = markets, list(positions), fail

    def markets(self):
        if self.fail:
            raise AuthError("/api/markets")
        return self._markets

    def positions(self):
        return self._positions

    def me(self):
        return {"giesBalance": 1000}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(alerts, "send", lambda level, title, body, tag=None: out.append((level, title, body)))
    return out


def run(ctx, platform, p_yes=None, confidence="high"):
    ctx.cfg["markets"] = {} if p_yes is None else {
        7: {"name": "Temp swing", "model": "manual", "params": {"p_yes": p_yes, "confidence": confidence}}}
    return Cycle(ctx.cfg, ctx.store, platform, ctx).run()


def test_best_edge_takes_the_fee_off():
    side, fair, price, edge = best_edge(market(7, 0.40), 0.68)
    assert (side, fair, price) == ("YES", 0.68, 0.40) and edge == pytest.approx(0.68 - 0.408)
    assert best_edge(market(7, 0.90), 0.68)[0] == "NO"


def test_edge_alert_fires_once_then_on_a_move_or_after_six_hours(at, sent):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    platform = FakePlatform([market(7, 0.40)])
    run(ctx, platform, 0.68)
    run(ctx, platform, 0.68)
    assert [t for _, t, _ in sent] == ["Temp swing: YES looks cheap (+27)"]
    platform._markets = [market(7, 0.33)]
    run(ctx, platform, 0.68)
    assert len(sent) == 2
    ctx.now += timedelta(hours=6, minutes=1)
    run(ctx, platform, 0.68)
    assert len(sent) == 4 and sent[3][0] == "quiet"  # third edge alert, plus the 8am summary


def test_small_edges_and_low_confidence_edges_stay_quiet(at, sent):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    run(ctx, FakePlatform([market(7, 0.60)]), 0.68)
    run(ctx, FakePlatform([market(7, 0.55)]), 0.68, confidence="low")
    assert sent == []
    run(ctx, FakePlatform([market(7, 0.50)]), 0.68, confidence="low")
    assert len(sent) == 1


def test_unmapped_market_alerts_once_with_the_criteria(at, sent):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    run(ctx, FakePlatform([market(11, 0.50)]))
    run(ctx, FakePlatform([market(11, 0.50)]))
    assert len(sent) == 1 and sent[0][0] == "loud" and "criteria text" in sent[0][2]


def test_resolved_market_in_config_gets_one_removal_reminder(at, sent):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    for _ in range(2):
        run(ctx, FakePlatform([market(7, 0.99, status="resolved")]), 0.68)
    assert [t for _, t, _ in sent] == ["Temp swing: market resolved"]


def test_sell_signal_for_a_held_side_trading_above_fair(at, sent):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    held = [{"marketId": 7, "outcome": "yes", "tokenAmount": 700, "avgCostBasis": 0.31,
             "currentValue": 560, "unrealizedPnl": 340}]
    run(ctx, FakePlatform([market(7, 0.74)], held), 0.68)
    assert sent == []
    run(ctx, FakePlatform([market(7, 0.80)], held), 0.68)
    assert any(t == "Temp swing: consider selling YES" for _, t, _ in sent)


def test_expired_token_alerts_once_and_stops(at, sent):
    ctx = FakeCtx({}, at(2026, 10, 8, 7))
    assert run(ctx, FakePlatform([], fail=True), 0.68) == []
    run(ctx, FakePlatform([], fail=True), 0.68)
    assert [t for _, t, _ in sent] == ["Login expired"]


def test_broken_model_alerts_after_two_hours_without_stopping_others(at, sent):
    ctx = FakeCtx({}, at(2026, 10, 8, 5))
    platform = FakePlatform([market(7, 0.40), market(8, 0.40)])

    def go():
        ctx.cfg["markets"] = {7: {"model": "no_such_model"}, 8: {"model": "manual", "params": {"p_yes": 0.9, "confidence": "high"}}}
        Cycle(ctx.cfg, ctx.store, platform, ctx).run()

    go()
    assert [t for _, t, _ in sent] == ["Market 8: YES looks cheap (+49)"]
    ctx.now += timedelta(hours=2, minutes=1)
    go()
    go()
    assert sum("no fair value for 2 hours" in t for _, t, _ in sent) == 1


def test_daily_summary_once_per_day_after_eight(at, sent):
    ctx = FakeCtx({}, at(2026, 10, 8, 7, 45))
    platform = FakePlatform([market(7, 0.66)])
    run(ctx, platform, 0.68)
    assert sent == []
    ctx.now += timedelta(minutes=30)
    run(ctx, platform, 0.68)
    run(ctx, platform, 0.68)
    assert len(sent) == 1 and sent[0][1] == "Daily summary, Thu Oct 8"
    assert sent[0][2] == "Temp swing\n  YES 66¢ · model 68% · no edge\n\nCash 1,000 · Account 1,000"
