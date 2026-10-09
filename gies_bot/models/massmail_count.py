"""YES if the UIUC Massmail archive lists at least `threshold` entries dated inside the window.

Every list entry counts, including same-day duplicates. Remaining sends are
negative binomial with mean rate_per_day * days_left and variance = dispersion * mean.

params: url, start, end, threshold, rate_per_day, dispersion
"""

import html
import math
import re
from datetime import date, datetime, time, timedelta

from . import FetchError, ModelResult, as_date

ENTRY = re.compile(
    r'<li>.*?<a href="(?P<href>[^"]*)"[^>]*>(?P<title>.*?)</a>.*?'
    r'class="col1">\s*(?P<date>\d{2}-\d{2}-\d{4})(?:\s|&nbsp;)*(?P<time>\d{1,2}:\d{2}\s*[ap]m)?',
    re.S | re.I,
)


def parse_archive(page: str) -> list[dict]:
    entries = []
    for m in ENTRY.finditer(page):
        month, day, year = (int(x) for x in m["date"].split("-"))
        entries.append({"date": date(year, month, day), "time": m["time"] or "",
                        "title": html.unescape(re.sub(r"<[^>]+>", "", m["title"])).strip(), "href": m["href"]})
    return entries


def prob_at_least(need: int, mean: float, dispersion: float) -> float:
    if need <= 0:
        return 1.0
    if mean <= 0:
        return 0.0
    if dispersion <= 1:  # Poisson
        pmf = [math.exp(-mean + n * math.log(mean) - math.lgamma(n + 1)) for n in range(need)]
    else:
        r, p = mean / (dispersion - 1), 1 / dispersion
        pmf = [math.exp(math.lgamma(n + r) - math.lgamma(r) - math.lgamma(n + 1)
                        + r * math.log(p) + n * math.log(1 - p)) for n in range(need)]
    return max(0.0, 1 - sum(pmf))


def fair_prob(market, params, ctx) -> ModelResult:
    start, end, threshold = as_date(params["start"]), as_date(params["end"]), int(params["threshold"])
    try:
        page, age = ctx.get_text(params["url"], max_age=600)
    except FetchError as exc:
        return ModelResult(notes=f"archive fetch failed: {exc}")
    entries = parse_archive(page)
    if not entries:
        return ModelResult(notes="archive page parsed to zero entries; layout may have changed")
    if ctx.is_stale(age):
        return ModelResult(notes="archive data is stale")

    in_window = sorted((e for e in entries if start <= e["date"] <= end), key=lambda e: e["date"])
    k = len(in_window)
    window_open = datetime.combine(start, time.min, ctx.now.tzinfo)
    window_close = datetime.combine(end + timedelta(days=1), time.min, ctx.now.tzinfo)
    days_left = max(0.0, (window_close - max(ctx.now, window_open)) / timedelta(days=1))
    mean = float(params["rate_per_day"]) * days_left
    p_yes = prob_at_least(threshold - k, mean, float(params.get("dispersion", 1)))

    history = {}
    for year in range(start.year - 5, start.year):
        lo, hi = start.replace(year=year), end.replace(year=year)
        history[year] = sum(lo <= e["date"] <= hi for e in entries)
    events = [f"Massmail {i} of the {threshold} needed was sent {e['date']:%b} {e['date'].day}: {e['title']}"
              for i, e in enumerate(in_window, 1)]
    return ModelResult(
        p_yes=p_yes, confidence="low", data_ok=True, events=events,
        notes=f"{k} of the {threshold} needed have been sent, with {days_left:.0f} days left.\n"
              f"The model expects about {mean:.1f} more.",
        inputs={"count": k, "days_left": round(days_left, 2), "mean_remaining": round(mean, 2),
                "same_window_prior_years": history, "entries": [f"{e['date']} {e['title']}" for e in in_window]},
    )
