"""YES if sum(sign * team points) >= threshold, across one or more football games.

Before kickoff a team's points are normal around its implied total from the
betting line on ESPN's scoreboard. A final score is exact. While any game is in
progress the model reports data_ok=False: there is no in-game model.

params:
  threshold: number
  terms: list of {league: college-football|nfl, team: ESPN display name,
                  date: game date (US Eastern), sign: 1|-1, fallback_implied: optional}
"""

import math

from . import FetchError, ModelResult, as_date, norm_cdf

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard?dates={ymd}"
SCORE_SD = {"college-football": 10.5, "nfl": 10.0}
FALLBACK_LINE_SD = 3.0  # extra uncertainty when the line is a config guess


def _implied_total(comp: dict, side: str) -> float | None:
    odds = (comp.get("odds") or [None])[0]
    if not odds or odds.get("overUnder") is None or odds.get("spread") is None:
        return None
    total, spread = float(odds["overUnder"]), abs(float(odds["spread"]))
    favorite = (odds.get(f"{side}TeamOdds") or {}).get("favorite")
    return (total + spread) / 2 if favorite else (total - spread) / 2


def _game(term: dict, ctx) -> dict:
    """Return {state: pre|in|final|unknown, value, sd, source, label}."""
    league, team = term["league"], term["team"]
    url = SCOREBOARD.format(league=league, ymd=as_date(term["date"]).strftime("%Y%m%d"))
    if league == "college-football":
        url += "&groups=80&limit=300"
    data, age = ctx.get_json(url, max_age=600)
    for event in data.get("events", []):
        comp = event["competitions"][0]
        me = next((c for c in comp["competitors"] if c["team"]["displayName"] == team), None)
        if me:
            break
    else:
        return {"state": "unknown", "label": f"{team}: game not found on ESPN"}

    label = f"{team} in {event.get('shortName', event['id'])}"
    status = event["status"]["type"]
    line_key = f"line:{event['id']}:{team}"
    if ctx.is_stale(age):
        return {"state": "unknown", "label": f"{label}: ESPN data is stale"}
    if status["state"] == "in":
        return {"state": "in", "label": label}
    if status["state"] == "post":
        if status.get("name") != "STATUS_FINAL":
            return {"state": "unknown", "label": f"{label}: {status.get('name')}"}
        return {"state": "final", "value": float(me["score"]), "sd": 0.0, "source": "final", "label": label}

    sd = SCORE_SD[league]
    implied = _implied_total(comp, me["homeAway"])
    if implied is not None:
        ctx.store.set(line_key, implied)
        return {"state": "pre", "value": implied, "sd": sd, "source": "betting line", "label": label}
    saved = ctx.store.get(line_key)
    if saved is not None:
        return {"state": "pre", "value": saved, "sd": sd, "source": "last betting line", "label": label}
    if term.get("fallback_implied") is not None:
        return {"state": "pre", "value": float(term["fallback_implied"]),
                "sd": math.hypot(sd, FALLBACK_LINE_SD), "source": "fallback guess, no line yet", "label": label}
    return {"state": "unknown", "label": f"{label}: no betting line posted and no fallback_implied in config"}


def fair_prob(market, params, ctx) -> ModelResult:
    threshold = float(params["threshold"])
    try:
        games = [(_game(term, ctx), term.get("sign", 1)) for term in params["terms"]]
    except FetchError as exc:
        return ModelResult(notes=f"ESPN fetch failed: {exc}")

    inputs = {"threshold": threshold, "games": [g for g, _ in games]}
    events = [f"Final score: {g['label']} scored {g['value']:.0f}" for g, _ in games if g["state"] == "final"]
    blocked = [g["label"] for g, _ in games if g["state"] in ("in", "unknown")]
    if blocked:
        live = any(g["state"] == "in" for g, _ in games)
        why = "Game in progress, so no fair value until it ends" if live else "Missing data"
        return ModelResult(notes=f"{why}: {'; '.join(blocked)}", inputs=inputs, events=events)

    mean = sum(sign * g["value"] for g, sign in games)
    sd = math.sqrt(sum(g["sd"] ** 2 for g, _ in games))
    if sd == 0:
        p_yes = 1.0 if mean >= threshold else 0.0
    else:
        p_yes = norm_cdf((mean - (threshold - 0.5)) / sd)  # scores are integers

    sources = {g["source"] for g, _ in games}
    confidence = ("low" if any(s.startswith("fallback") for s in sources)
                  else "med" if "last betting line" in sources else "high")
    lines = [f"{g['label']}: scored {g['value']:.0f}" if g["state"] == "final"
             else f"{g['label']}: {g['value']:.1f} points expected ({g['source']})" for g, _ in games]
    plus = sum(sign > 0 for _, sign in games)
    if plus == len(games):
        rule = f"YES needs {threshold:g} or more combined."
    elif len(games) == 2 and plus == 1:
        rule = f"YES needs the first to beat the second by {threshold:g} or more."
    else:
        rule = f"YES needs a signed total of {threshold:g} or more."
    return ModelResult(p_yes=p_yes, confidence=confidence, data_ok=True, inputs=inputs, events=events,
                       notes="\n".join(lines + [rule]))
