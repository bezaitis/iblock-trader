"""YES if (highest daily high) - (lowest daily low) over a date window is >= threshold.

Observed days come from the NWS daily climate report (CLI) as republished by
IEM, which is the product the market resolves on. Days at or before today that
have no CLI yet use IEM's ASOS daily summary as a provisional value. Days ahead
use the NWS forecast plus error where it reaches, and past years' values for
the same calendar days beyond that.

Forecast error on a day has standard deviation base + per_day * lead. Part of it
is shared by every forecast day in a simulation (a forecast that runs warm tends
to run warm all week) and the rest is that day's own. Past years count equally
by default: a backtest on 2006-2025 (scripts/backtest_temperature_swing.py) found
that favouring recent years made the climatology slightly worse, not better.

params: station (KCMI), iem_station (CMI), iem_network (IL_ASOS), lat, lon,
        start, end, threshold, clim_years [first, last]
        optional: recent_half_life_years (default null = all years equal; 10 halves a year's weight per decade),
                  forecast_sd_high [base, per_day] (default [2.5, 0.5]),
                  forecast_sd_low [base, per_day] (default [3.0, 0.5]),
                  forecast_error_correlation (default 0.5, share of error variance that is shared),
                  high_bounds, low_bounds, draws_per_year
"""

import math
import random
from datetime import date, datetime, timedelta

from . import FetchError, ModelResult, as_date

CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py?station={station}&year={year}"
DAILY_URL = "https://mesonet.agron.iastate.edu/api/1/daily.json?station={station}&network={network}&year={year}&month={month}"
POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
MIN_CLIM_YEARS = 15


def _number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _daily(ctx, params, year: int, month: int, max_age) -> dict[date, tuple]:
    url = DAILY_URL.format(station=params["iem_station"], network=params["iem_network"], year=year, month=month)
    data, _ = ctx.get_json(url, max_age)
    return {date.fromisoformat(r["date"]): (r["max_tmpf"], r["min_tmpf"]) for r in data["data"]}


def fair_prob(market, params, ctx) -> ModelResult:
    start, end, threshold = as_date(params["start"]), as_date(params["end"]), float(params["threshold"])
    hi_lo, hi_hi = params.get("high_bounds", [20, 105])
    lo_lo, lo_hi = params.get("low_bounds", [0, 90])
    today = ctx.now.date()
    window = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    months = sorted({(d.year, d.month) for d in window})

    def plausible(high, low) -> bool:
        return _number(high) and _number(low) and hi_lo <= high <= hi_hi and lo_lo <= low <= lo_hi and high >= low

    # Observed so far: official CLI first, provisional ASOS for recent gaps.
    obs: dict[date, tuple] = {}
    try:
        for year in sorted({y for y, _ in months}):
            data, age = ctx.get_json(CLI_URL.format(station=params["station"], year=year), max_age=1800)
            if ctx.is_stale(age) and today <= end + timedelta(days=2):
                return ModelResult(notes="NWS climate report data is stale")
            for r in data["results"]:
                d = date.fromisoformat(r["valid"])
                if start <= d <= end and plausible(r["high"], r["low"]):
                    obs[d] = (r["high"], r["low"], "cli")
    except FetchError as exc:
        return ModelResult(notes=f"NWS climate report fetch failed: {exc}")
    try:
        for year, month in months:
            if date(year, month, 1) > today:
                continue
            for d, (high, low) in _daily(ctx, params, year, month, max_age=1800).items():
                if d in window and d <= today and d not in obs and plausible(high, low):
                    obs[d] = (round(high), round(low), "asos provisional")
    except FetchError:
        pass  # CLI is the record; this only fills the last day or so

    # Forecast for today onward. A night period's low lands on the next morning's date.
    fc_high: dict[date, float] = {}
    fc_low: dict[date, float] = {}
    ahead = [d for d in window if d >= today]
    if ahead:
        try:
            point, _ = ctx.get_json(POINTS_URL.format(lat=params["lat"], lon=params["lon"]), max_age=7 * 86400)
            forecast, age = ctx.get_json(point["properties"]["forecast"], max_age=3600)
        except FetchError as exc:
            return ModelResult(notes=f"NWS forecast fetch failed: {exc}")
        if age > 12 * 3600:
            return ModelResult(notes="NWS forecast is more than 12 hours old")
        for period in forecast["properties"]["periods"]:
            begins = datetime.fromisoformat(period["startTime"])
            if period["isDaytime"]:
                fc_high[begins.date()] = period["temperature"]
            else:
                fc_low[(begins + timedelta(hours=12)).date()] = period["temperature"]

    # Days still to simulate; those the forecast doesn't fully cover borrow from past years.
    todo = [d for d in window if d >= today or d not in obs]
    need_clim = [d for d in todo if d != today and (d not in fc_high or d not in fc_low)]

    first, last = params["clim_years"]
    clim: dict[int, dict[tuple, tuple]] = {}
    if need_clim:
        for year in range(first, last + 1):
            try:
                days = {}
                for month in sorted({m for _, m in months}):
                    for d, (high, low) in _daily(ctx, params, year, month, max_age=None).items():
                        if plausible(high, low):
                            days[(d.month, d.day)] = (round(high), round(low))
            except FetchError:
                continue
            if all((d.month, d.day) in days for d in need_clim):
                clim[year] = days
        if len(clim) < MIN_CLIM_YEARS:
            return ModelResult(notes=f"only {len(clim)} usable climatology years")

    h_obs = max((v[0] for v in obs.values()), default=None)
    l_obs = min((v[1] for v in obs.values()), default=None)
    rng = random.Random(0)  # same inputs give the same answer, so alerts don't flap on sampling noise
    draws = params.get("draws_per_year", 200)
    high_base, high_step = params.get("forecast_sd_high", [2.5, 0.5])
    low_base, low_step = params.get("forecast_sd_low", [3.0, 0.5])
    rho = float(params.get("forecast_error_correlation", 0.5))
    shared_part, own_part = math.sqrt(rho), math.sqrt(1 - rho)
    half_life = params.get("recent_half_life_years")
    newest = max(clim, default=0)
    hits = total = 0.0
    for year, days in clim.items() or [(0, {})] * 30:
        weight = 0.5 ** ((newest - year) / half_life) if half_life else 1.0
        for _ in range(draws if todo else 1):
            high = h_obs if h_obs is not None else float("-inf")
            low = l_obs if l_obs is not None else float("inf")
            shared_high, shared_low = rng.gauss(0, 1), rng.gauss(0, 1)
            for d in todo:
                lead = max(0, (d - today).days)
                if d in fc_high:
                    error = shared_part * shared_high + own_part * rng.gauss(0, 1)
                    high = max(high, fc_high[d] + (high_base + high_step * lead) * error)
                elif d != today:
                    high = max(high, days[(d.month, d.day)][0])
                if d in fc_low:
                    error = shared_part * shared_low + own_part * rng.gauss(0, 1)
                    low = min(low, fc_low[d] + (low_base + low_step * lead) * error)
                elif d != today:
                    low = min(low, days[(d.month, d.day)][1])
            hits += weight * (round(high) - round(low) >= threshold)
            total += weight
    p_yes = hits / total

    provisional = sorted(str(d) for d, v in obs.items() if v[2] != "cli")
    fc_min = min(((t, d) for d, t in fc_low.items() if d in ahead), default=None)
    events = [f"The forecast now shows a low of {t}°F on {d:%b} {d.day}" for d, t in fc_low.items() if d in ahead and t <= 32]
    # Backtested from day 2 of the window, the climatology barely beats the base rate, so lean on it lightly.
    covered = len(todo) - len(need_clim)
    confidence = "high" if not need_clim else "med" if covered >= len(need_clim) else "low"
    if h_obs is None:
        notes = "The window has no observations yet."
    else:
        notes = (f"High so far {h_obs}°F, low so far {l_obs}°F.\n"
                 f"YES needs a low of {h_obs - threshold:g}°F or colder, or a high of {l_obs + threshold:g}°F.")
    if fc_min:
        notes += f"\nColdest low in the forecast: {fc_min[0]}°F on {fc_min[1]:%b} {fc_min[1].day}."
    return ModelResult(
        p_yes=p_yes, confidence=confidence, data_ok=True, notes=notes, events=events,
        inputs={"high_obs": h_obs, "low_obs": l_obs, "days_observed": len(obs), "provisional_days": provisional,
                "days_remaining": len(ahead), "forecast_highs": {str(d): t for d, t in fc_high.items()},
                "forecast_lows": {str(d): t for d, t in fc_low.items()}, "climatology_years": len(clim)},
    )
