import pytest

from conftest import FakeCtx
from gies_bot.models.football_points import fair_prob


def event(team, state="pre", score="0", total=None, spread=None, favorite=True, name="STATUS_SCHEDULED", eid="1"):
    comp = {"competitors": [{"team": {"displayName": team}, "score": score, "homeAway": "away"},
                            {"team": {"displayName": "Opponent"}, "score": "0", "homeAway": "home"}]}
    if total is not None:
        comp["odds"] = [{"overUnder": total, "spread": spread, "awayTeamOdds": {"favorite": favorite},
                         "homeTeamOdds": {"favorite": not favorite}}]
    return {"events": [{"id": eid, "shortName": "X @ Y", "competitions": [comp],
                        "status": {"type": {"state": state, "name": name}}}]}


ILL = {"league": "college-football", "team": "Illinois Fighting Illini", "date": "2026-10-10", "sign": 1}
ORE = {**ILL, "date": "2026-10-24", "fallback_implied": 21.0}
BEARS = {"league": "nfl", "team": "Chicago Bears", "date": "2026-10-11", "sign": -1}
COMPARE = {"threshold": 1, "terms": [ILL, BEARS]}


def test_compare_pregame_matches_hand_calc(at):
    ctx = FakeCtx({"20261010": event("Illinois Fighting Illini", total=47.5, spread=2.5),
                   "20261011": event("Chicago Bears", total=45.5, spread=2.5)}, at(2026, 10, 8, 12))
    result = fair_prob({}, COMPARE, ctx)
    assert result.data_ok and result.confidence == "high"
    assert result.p_yes == pytest.approx(0.514, abs=0.005)  # implied 25.0 vs 24.0


def test_underdog_gets_the_smaller_share(at):
    ctx = FakeCtx({"20261010": event("Illinois Fighting Illini", total=47.5, spread=2.5, favorite=False),
                   "20261011": event("Chicago Bears", total=45.5, spread=2.5)}, at(2026, 10, 8, 12))
    assert fair_prob({}, COMPARE, ctx).inputs["games"][0]["value"] == 22.5


def test_total_uses_fallback_line_with_low_confidence(at):
    ctx = FakeCtx({"20261010": event("Illinois Fighting Illini", total=47.5, spread=2.5),
                   "20261024": event("Illinois Fighting Illini", eid="2")}, at(2026, 10, 8, 12))
    result = fair_prob({}, {"threshold": 35, "terms": [ILL, ORE]}, ctx)
    assert result.confidence == "low"
    assert result.p_yes == pytest.approx(0.776, abs=0.005)


def test_frozen_while_a_game_is_in_progress(at):
    ctx = FakeCtx({"20261010": event("Illinois Fighting Illini", state="in", score="14"),
                   "20261011": event("Chicago Bears", total=45.5, spread=2.5)}, at(2026, 10, 10, 15))
    result = fair_prob({}, COMPARE, ctx)
    assert not result.data_ok and "in progress" in result.notes


def test_conditions_on_a_final_score(at):
    ctx = FakeCtx({"20261010": event("Illinois Fighting Illini", state="post", score="31", name="STATUS_FINAL"),
                   "20261011": event("Chicago Bears", total=45.5, spread=2.5)}, at(2026, 10, 10, 20))
    result = fair_prob({}, COMPARE, ctx)
    assert result.p_yes == pytest.approx(0.742, abs=0.005)  # P(Bears <= 30), mean 24, sd 10
    assert result.events == ["Final score: Illinois Fighting Illini in X @ Y scored 31"]


def test_both_final_is_certain_and_a_tie_is_no(at):
    def ctx(bears):
        return FakeCtx({"20261010": event("Illinois Fighting Illini", state="post", score="24", name="STATUS_FINAL"),
                        "20261011": event("Chicago Bears", state="post", score=bears, name="STATUS_FINAL")},
                       at(2026, 10, 11, 20))
    assert fair_prob({}, COMPARE, ctx("23")).p_yes == 1.0
    assert fair_prob({}, COMPARE, ctx("24")).p_yes == 0.0


def test_line_is_remembered_after_it_disappears(at):
    routes = {"20261010": event("Illinois Fighting Illini", total=47.5, spread=2.5),
              "20261011": event("Chicago Bears", total=45.5, spread=2.5)}
    ctx = FakeCtx(routes, at(2026, 10, 8, 12))
    fair_prob({}, COMPARE, ctx)
    routes["20261011"] = event("Chicago Bears")
    result = fair_prob({}, COMPARE, ctx)
    assert result.data_ok and result.confidence == "med"


def test_postponed_stale_or_missing_is_not_ok(at):
    good = event("Chicago Bears", total=45.5, spread=2.5)
    for illinois in (event("Illinois Fighting Illini", state="post", name="STATUS_POSTPONED"),
                     (event("Illinois Fighting Illini", total=47.5, spread=2.5), 3 * 3600),
                     event("Someone Else", total=47.5, spread=2.5),
                     event("Illinois Fighting Illini")):
        ctx = FakeCtx({"20261010": illinois, "20261011": good}, at(2026, 10, 8, 12))
        assert not fair_prob({}, COMPARE, ctx).data_ok
    assert not fair_prob({}, COMPARE, FakeCtx({}, at(2026, 10, 8, 12))).data_ok
