import re

from conftest import FakeCtx
from gies_bot.models.temperature_swing import fair_prob

PARAMS = {"station": "KCMI", "iem_station": "CMI", "iem_network": "IL_ASOS", "lat": 40.0, "lon": -88.0,
          "start": "2026-10-07", "end": "2026-10-28", "threshold": 54, "clim_years": [1990, 2025]}


def cli(*days):
    return {"results": [{"valid": d, "high": h, "low": l} for d, h, l in days]}


def daily_for(coldest_low, asos_2026=()):
    """IEM daily responses: every past October is 65/45 except one day at coldest_low."""
    def body(year):
        if year == 2026:
            return {"data": [{"date": d, "max_tmpf": h, "min_tmpf": l} for d, h, l in asos_2026]}
        return {"data": [{"date": f"{year}-10-{day:02d}", "max_tmpf": 65.0,
                          "min_tmpf": float(coldest_low if day == 20 else 45)} for day in range(1, 32)]}
    return {f"year={y}&": body(y) for y in range(1990, 2027)}


def forecast(periods):
    return {"properties": {"periods": [{"startTime": s, "isDaytime": day, "temperature": t} for s, day, t in periods]}}


def routes(cli_days, coldest_low, periods=(), asos_2026=()):
    return {"cli.py": cli(*cli_days), **daily_for(coldest_low, asos_2026),
            "/points/": {"properties": {"forecast": "https://api.weather.gov/gridpoints/X/1,1/forecast"}},
            "/gridpoints/": forecast(periods)}


def test_swing_already_reached_is_certain(at):
    ctx = FakeCtx(routes([("2026-10-07", 85, 49), ("2026-10-08", 60, 30)], 45), at(2026, 10, 9, 9))
    assert fair_prob({}, PARAMS, ctx).p_yes == 1.0


def test_climatology_decides_the_days_beyond_the_forecast(at):
    seen = [("2026-10-07", 85, 49)]
    cold = fair_prob({}, PARAMS, FakeCtx(routes(seen, 25), at(2026, 10, 9, 9)))
    mild = fair_prob({}, PARAMS, FakeCtx(routes(seen, 40), at(2026, 10, 9, 9)))
    assert cold.data_ok and cold.p_yes == 1.0 and cold.inputs["climatology_years"] == 36
    assert mild.p_yes == 0.0
    assert "YES needs a low of 31°F or colder" in cold.notes


def test_forecast_overrides_climatology_and_night_lows_land_on_the_next_day(at):
    periods = [("2026-10-19T18:00:00-05:00", False, 60)]  # the night before the day climatology calls cold
    result = fair_prob({}, PARAMS, FakeCtx(routes([("2026-10-07", 85, 49)], 25, periods), at(2026, 10, 19, 12)))
    assert result.inputs["forecast_lows"] == {"2026-10-20": 60}
    assert result.p_yes == 0.0


def test_bogus_readings_are_ignored_and_asos_fills_recent_gaps(at):
    seen = [("2026-10-07", 85, 49), ("2026-10-08", 80, -1)]
    asos = [("2026-10-08", 80.0, -1.0), ("2026-10-09", 84.2, 30.9)]
    result = fair_prob({}, PARAMS, FakeCtx(routes(seen, 45, asos_2026=asos), at(2026, 10, 9, 20)))
    assert result.inputs["low_obs"] == 31 and result.inputs["provisional_days"] == ["2026-10-09"]
    assert result.p_yes == 1.0


def test_window_over_is_decided_from_observations(at):
    days = [(f"2026-10-{d:02d}", 80, 40) for d in range(7, 29)]
    result = fair_prob({}, PARAMS, FakeCtx(routes(days, 25), at(2026, 10, 30, 9)))
    assert result.data_ok and result.p_yes == 0.0


def test_missing_inputs_are_not_ok(at):
    full = routes([("2026-10-07", 85, 49)], 45)
    for missing in ("cli.py", "/gridpoints/"):
        ctx = FakeCtx({k: v for k, v in full.items() if k != missing}, at(2026, 10, 9, 9))
        assert not fair_prob({}, PARAMS, ctx).data_ok
    no_history = {k: v for k, v in full.items() if not re.match(r"year=(199|200|201)", k)}
    assert not fair_prob({}, PARAMS, FakeCtx(no_history, at(2026, 10, 9, 9))).data_ok
    stale = {**full, "/gridpoints/": (forecast([]), 13 * 3600)}
    assert not fair_prob({}, PARAMS, FakeCtx(stale, at(2026, 10, 9, 9))).data_ok


def test_recent_years_can_be_weighted_more_and_early_predictions_are_low_confidence(at):
    full = routes([("2026-10-07", 85, 49)], 45)
    for year in range(1990, 2008):  # only the older half of the record had a cold enough day
        for row in full[f"year={year}&"]["data"]:
            if row["date"].endswith("-10-20"):
                row["min_tmpf"] = 25.0
    equal = fair_prob({}, PARAMS, FakeCtx(full, at(2026, 10, 9, 9)))
    recent = fair_prob({}, {**PARAMS, "recent_half_life_years": 5}, FakeCtx(full, at(2026, 10, 9, 9)))
    assert equal.p_yes == 0.5 and recent.p_yes < 0.1
    assert equal.confidence == "low"


def test_confidence_rises_as_the_forecast_covers_the_remaining_days(at):
    periods = [(f"2026-10-{d}T06:00:00-05:00", True, 70) for d in (26, 27, 28)]
    periods += [(f"2026-10-{d}T18:00:00-05:00", False, 50) for d in (25, 26, 27)]
    seen = [(f"2026-10-{d:02d}", 80, 40) for d in range(7, 26)]
    result = fair_prob({}, PARAMS, FakeCtx(routes(seen, 45, periods), at(2026, 10, 26, 5)))
    assert result.confidence == "high" and result.p_yes == 0.0
