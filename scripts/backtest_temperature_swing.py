"""Backtest temperature_swing on past years: what would it have said early in the window, and was it right?

    uv run python scripts/backtest_temperature_swing.py [market id, default 7]

For each past year it stands on the second day of the window, knowing only that
year's readings so far and earlier years as climatology. There is no archive of
real forecasts, so it runs twice: with no forecast at all (climatology only),
and with a perfect 7-day forecast (that year's actual weather, to which the
model still adds its forecast error). Real forecasts sit between the two.
"""

import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from gies_bot.config import ROOT, load_config  # noqa: E402
from gies_bot.models import Ctx, as_date  # noqa: E402
from gies_bot.models.temperature_swing import DAILY_URL, fair_prob  # noqa: E402
from gies_bot.store import Store  # noqa: E402

FIRST_TEST_YEAR, FORECAST_DAYS = 2006, 7
VARIANTS = {
    "old: equal years, independent error": {"recent_half_life_years": None, "forecast_sd_high": [2.0, 0.4],
                                            "forecast_sd_low": [2.5, 0.4], "forecast_error_correlation": 0.0},
    "equal years, shared error (default)": {"recent_half_life_years": None},
    "half-life 20, shared error": {"recent_half_life_years": 20},
    "half-life 10, shared error": {"recent_half_life_years": 10},
    "half-life 5, shared error": {"recent_half_life_years": 5},
}


class PastCtx(Ctx):
    """Serves one past year as if it were now: real daily data, a synthetic forecast."""

    def __init__(self, real: Ctx, params: dict, year: int, asof: date, forecast: bool):
        self.cfg, self.store, self.real, self.params = real.cfg, Store(":memory:"), real, params
        self.now = datetime.combine(asof, time(17), ZoneInfo("America/Chicago"))
        self.asof, self.forecast = asof, forecast
        self.actual = {}
        for month in {asof.month, (asof + timedelta(days=40)).month}:
            url = DAILY_URL.format(station=params["iem_station"], network=params["iem_network"], year=year, month=month)
            for r in real.get_json(url, None)[0]["data"]:
                self.actual[date.fromisoformat(r["date"])] = (r["max_tmpf"], r["min_tmpf"])

    def get_json(self, url, max_age):
        if "cli.py" in url:
            return {"results": [{"valid": str(d), "high": round(h), "low": round(l)} for d, (h, l) in self.actual.items()
                                if d <= self.asof and h is not None and l is not None]}, 0.0
        if "/points/" in url:
            return {"properties": {"forecast": "https://backtest/gridpoints/forecast"}}, 0.0
        if "/gridpoints/" in url:
            periods = []
            for lead in range(1, FORECAST_DAYS + 1 if self.forecast else 1):
                d = self.asof + timedelta(days=lead)
                high, low = self.actual.get(d, (None, None))
                if high is None or low is None:
                    continue
                periods.append({"startTime": f"{d}T06:00:00-05:00", "isDaytime": True, "temperature": round(high)})
                periods.append({"startTime": f"{d - timedelta(days=1)}T18:00:00-05:00", "isDaytime": False, "temperature": round(low)})
            return {"properties": {"periods": periods}}, 0.0
        return self.real.get_json(url, None)  # past years' daily data, cached forever


def outcome(actual: dict, start: date, end: date, threshold: float) -> bool | None:
    days = [actual.get(start + timedelta(days=i)) for i in range((end - start).days + 1)]
    if any(d is None or d[0] is None or d[1] is None or d[1] < 0 for d in days):
        return None
    return round(max(d[0] for d in days)) - round(min(d[1] for d in days)) >= threshold


def main() -> None:
    cfg = load_config()
    market_id = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    base = cfg["markets"][market_id]["params"]
    start, end = as_date(base["start"]), as_date(base["end"])
    real = Ctx(cfg, Store(ROOT / "gies.db"), datetime.now(ZoneInfo(cfg["timezone"])))
    thresholds = [base["threshold"] - 4, base["threshold"], base["threshold"] + 4]
    print(f"Window {start:%b %d} to {end:%b %d}, standing on day 2, years {FIRST_TEST_YEAR}-{start.year - 1}, thresholds {thresholds}")

    for forecast in (False, True):
        results = {name: [] for name in VARIANTS}
        for year in range(FIRST_TEST_YEAR, start.year):
            s, e = start.replace(year=year), end.replace(year=year)
            for threshold in thresholds:
                for name, tweak in VARIANTS.items():
                    params = {**base, **tweak, "start": s, "end": e, "threshold": threshold,
                              "clim_years": [base["clim_years"][0], year - 1], "draws_per_year": 100 if forecast else 1}
                    ctx = PastCtx(real, params, year, s + timedelta(days=1), forecast)
                    happened = outcome(ctx.actual, s, e, threshold)
                    result = fair_prob({}, params, ctx)
                    if happened is not None and result.data_ok:
                        results[name].append((year, threshold, result.p_yes, happened))

        print(f"\n{'Perfect 7-day forecast' if forecast else 'No forecast (climatology only)'}")
        print(f"{'variant':42} {'cases':>5} {'said':>5} {'happened':>8} {'Brier':>6} | threshold {base['threshold']} only: said, happened, Brier")
        for name, rows in results.items():
            stat = lambda rs: (sum(r[2] for r in rs) / len(rs), sum(r[3] for r in rs) / len(rs),
                               sum((r[2] - r[3]) ** 2 for r in rs) / len(rs))
            said, freq, brier = stat(rows)
            m_said, m_freq, m_brier = stat([r for r in rows if r[1] == base["threshold"]])
            print(f"{name:42} {len(rows):5d} {said:5.2f} {freq:8.2f} {brier:6.3f} | {m_said:.2f}, {m_freq:.2f}, {m_brier:.3f}")
        if "-v" in sys.argv:
            for year, threshold, said, happened in results["equal years, shared error (default)"]:
                if threshold == base["threshold"]:
                    print(f"   {year}: said {said:.2f}, {'YES' if happened else 'no'}")
    print("\nBrier: lower is better; always saying 50% scores 0.250.")


if __name__ == "__main__":
    main()
