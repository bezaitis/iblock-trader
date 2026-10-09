# iblock-trader

Monitors the GIES classroom prediction market. Every 15 minutes it computes a fair
probability for each configured market from outside data, compares it with the
market price, and sends phone alerts through [ntfy](https://ntfy.sh). It can also
trade, but ships with trading off (`mode: monitor`).

## Setup

```bash
uv sync
cp .env.example .env && chmod 600 .env
```

Fill in `.env`:

- `NTFY_TOPIC`: any long, hard-to-guess name. Install the ntfy app on your phone
  and subscribe to the same name. Anyone who knows the name can read the alerts.
- `GIES_TOKEN`: log in to the market site in Chrome, open DevTools (Cmd+Opt+I),
  go to Network, reload, click any request whose path starts with `/api/`, and under
  Request Headers copy the `Authorization` value without the word `Bearer`.
  Repeat this when the bot alerts that the token expired.
- `CONTACT_EMAIL`: your email. It is sent only to the National Weather Service,
  which asks API users to identify themselves.


## Commands

```bash
uv run python -m gies_bot models -v     # fair values only, no token needed
uv run python -m gies_bot test-alert    # check the phone gets a notification
uv run python -m gies_bot markets       # platform markets and which have a model
uv run python -m gies_bot once          # one full cycle, prints a summary
uv run python -m gies_bot run           # monitor until stopped; also serves the dashboard
uv run python -m gies_bot dashboard     # dashboard only
uv run pytest -q
```

## Alerts

Loud: an edge past `min_edge` after the trading fee, a held position trading above
fair value, a new market with no model, a model with no usable data for two hours,
an expired token. Quiet: the 8am summary and model news such as a new Massmail or
a final score. An edge alert repeats only if the edge moves 5 points or 6 hours pass.

## Trading

`mode` in `config.yaml` is `monitor` (alerts only), `dry_run` (also plans orders and
gets real quotes, never trades) or `live`. Live additionally needs
`GIES_LIVE_TRADING=I_UNDERSTAND` in `.env`; without it the bot runs as dry run.
Only markets with `trade: true` are touched.

Each cycle, per market, the bot places at most one order, starting from the
positions the platform reports:

- **Sell** a held side trading `take_profit_buffer` above fair value. The order is
  rejected if it would receive less than fair value.
- **Buy** the side whose edge would trigger an alert, as long as the price paid and
  the price afterwards both stay `buy_buffer` under fair value.
- Size is `max_order_gies`, reduced to fit cash, the per-market cap and the overall
  cap, and halved (up to three times) while the platform's quote is unacceptable.
- Every order is quoted immediately before it is sent. A quote that is blocked or
  exceeds `max_price_impact` is not traded. Platform warnings are advisory: they
  are noted in the alert and the order log but do not stop an order.
- An order is sent once and never retried. If there is no answer, the bot alerts,
  stops ordering for that cycle, and re-reads positions next cycle.

To pause trading while alerts continue, and to undo that or a loss-limit halt:

```bash
uv run python -m gies_bot stop
```

```bash
uv run python -m gies_bot resume
```

Every quote and order is kept in the `orders` table of `gies.db`.

## Dashboard

http://127.0.0.1:8765 while `run` or `dashboard` is going. It is bound
to localhost and shows: current price, model and edge per market, positions, a
price-against-model chart per market, markets that still need a model, and recent
alerts.

It also has the trading switches: mode (alerts only, dry run, live), auto-trade per
market, and pause/resume. A switch overrides `config.yaml`, is remembered in
`gies.db`, and takes effect at the next check. Selecting live there still does
nothing unless `GIES_LIVE_TRADING=I_UNDERSTAND` is in `.env`.

On a remote machine reach it with `ssh -L 8765:127.0.0.1:8765 <host>`.

## Markets and models

`config.yaml` maps each market id to a short name, a model and its parameters. A model is one
file in `gies_bot/models/`. To add, tune or retire one, ask Claude Code in this
folder ("add a model for market 11"); it follows `.claude/skills/add-market-model`.
For an instant placeholder use the `manual` model with a `p_yes` you choose.

State lives in `gies.db` (SQLite): cached downloads, alert history, and a
`snapshots` table of price and fair value per cycle. Logs go to `gies.log`.
