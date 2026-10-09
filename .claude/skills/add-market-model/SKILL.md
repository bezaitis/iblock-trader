---
name: add-market-model
description: Add, change, or remove the fair-value model for a market on the GIES prediction market bot in this repo. Use when the user says a new market came online, pastes a market id or title, gets a "needs a model" alert, wants to retire a resolved market, or wants to tune a model's parameters.
---

# Add or remove a market model

The user wants to do this with little or no code. Do the work yourself and report
the fair value at the end. A market is tracked when it has a block under `markets:`
in `config.yaml`; a model is one file in `gies_bot/models/` whose file name is the
model name.

## Add a market

1. **Read the market.** Run `uv run python -m gies_bot markets -v` and find the market
   (needs `GIES_TOKEN` in `.env`; if it is missing, ask the user to paste the title,
   description and resolution criteria). Never print or ask for the token.
2. **Read the resolution criteria closely, not the title.** State in one or two lines
   exactly what makes it resolve YES: the quantity, the threshold and whether it is
   inclusive, the date window and timezone, the named data source, and tie rules.
   Tell the user about any mismatch between title and criteria (market 7's title and
   criteria disagreed on dates) and follow the criteria.
3. **Pick the cheapest model that fits**, in this order:
   - An existing model with new params. Read the docstring at the top of each file in
     `gies_bot/models/` for what it covers and its params:
     `football_points` (sums and differences of team points across games),
     `temperature_swing` (max high minus min low at a station over a window),
     `massmail_count` (count of dated entries in a web archive over a window).
   - `manual`, with a `p_yes` the user gives or you estimate together. Use this right
     away when a real model will take a while, so alerts start now.
   - A new model file, only when the above do not fit.
4. **Add the block to `config.yaml`** under `markets:`, keyed by market id, with
   `model:` and `params:`. Dates are unquoted `YYYY-MM-DD`. Add a short comment for
   anything surprising in the criteria.
5. **Check it.** Run `uv run python -m gies_bot models --market <id> -v` and read the
   inputs. Compare the fair value with a quick hand estimate and with the market
   price; if they disagree badly, find out why before trusting either.
6. **Report** the fair value, confidence, the inputs it used, and the current price.
   The running bot rereads `config.yaml` every cycle, so no restart is needed.

## Write a new model file

Copy the shape of `gies_bot/models/massmail_count.py`, the simplest full example.

- One function: `fair_prob(market: dict, params: dict, ctx) -> ModelResult`.
- Start the file with a docstring: what resolves YES, the method, and the params.
- Fetch through `ctx.get_json(url, max_age)` or `ctx.get_text(url, max_age)`. Both
  return `(data, age_seconds)`, cache in SQLite, and hand back the old copy with its
  true age if a refresh fails. Use `ctx.now` (timezone-aware, Central) for all date
  logic and `ctx.store.get/set` for anything to remember between cycles.
- Return `ModelResult(...)` with `data_ok=True` only when every input was fetched,
  passed sanity checks, and is fresh (`ctx.is_stale(age)`). On any doubt return
  `ModelResult(notes="why")`, which is not ok and never produces an edge alert.
- If the outcome depends on a live event with no in-progress model (a game, a vote
  count), return not ok while it is in progress.
- `confidence` is `high` only when the inputs are market-grade (betting lines, an
  official forecast that covers the whole window). Thin calibration is `low`.
- `notes` is one line a person reads in a phone alert. `events` are one-off facts
  worth a quiet notification; each distinct string is sent once.
- Handle the ends: before the window opens, and after the outcome is decided (return
  exactly 0.0 or 1.0).
- Before writing the parser, fetch the real source once and look at it. ESPN returns
  403 to User-Agents containing "bot"; the shared client already avoids that.
- No new dependencies unless there is no reasonable way without one.

Then add `tests/test_<model>.py` using `FakeCtx` from `tests/conftest.py` with canned
responses. Cover a hand-calculated case, the decided cases, and bad or stale data.
Run `uv run pytest -q`; everything must pass before reporting done.

## Remove or retire a market

Delete its block from `config.yaml`. Delete the model file and its test only if no
other block uses that model and the user does not expect that market type again.
The bot sends a one-time reminder when a configured market stops being open.

## Tune a model

Record the fair value first (step 5). If the change fits existing params, edit
`config.yaml`. If it needs new behaviour, add an optional param to the model file
with a default, document it in the file's docstring, and add a test.

Before trusting a change, check it against history when the data allows: for each
past year, run the model as it would have stood early in the window and compare
what it said with what happened (`scripts/backtest_temperature_swing.py` is the
pattern). Keep a change as the default only if the backtest supports it; say so
plainly when it does not. Report the fair value before and after, what the
backtest showed, and how many cases it rests on.

The running bot rereads `config.yaml` every cycle but loads model code once, so
after editing a model file tell the user to restart `run`.

## Boundaries

This skill covers models and config only. Do not add or change trading code, and do
not place trades, as part of adding a model. Add new markets with `trade: false`;
turning it on is the user's decision once they trust the fair value.
