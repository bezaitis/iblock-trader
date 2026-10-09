"""Turns a cycle's fair values into at most one order per market.

Stateless: every cycle starts from the positions the platform reports, so a
lost or failed order is simply reconsidered next time and nothing is retried.

  monitor  this module is never called
  dry_run  plans and quotes orders, records and alerts them, never trades
  live     trades, and only if GIES_LIVE_TRADING=I_UNDERSTAND is also set

Rules, per market that has `trade: true` and a usable model:
  sell  a held side trading take_profit_buffer above fair, down to fair
  buy   the side with an alertable edge, up to fair - buy_buffer, within the caps
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx

from .fmt import cents, pct

log = logging.getLogger(__name__)
LIVE_ENV, LIVE_PHRASE = "GIES_LIVE_TRADING", "I_UNDERSTAND"
QUOTE_ATTEMPTS = 4          # each rejected quote halves the order
MAX_FILL_SLIPPAGE = 0.02    # fill price against quote, worth a mention in the alert


@dataclass
class Order:
    market_id: int
    side: str           # buy | sell
    outcome: str        # yes | no
    amount: float
    amount_type: str    # gies for buys, tokens for sells
    fair: float         # model value of this outcome
    limit: float        # buys must stay at or under this price, sells at or over it

    def body(self) -> dict:
        return {"marketId": self.market_id, "side": self.side, "outcome": self.outcome,
                "amount": round(self.amount, 4), "amountType": self.amount_type}

    def describe(self) -> str:
        unit = "GIES of" if self.amount_type == "gies" else "tokens of"
        return f"{self.side} {self.amount:,.1f} {unit} {self.outcome.upper()}"


@dataclass
class Book:
    """Cash and cost basis, kept current as orders fill within one cycle."""
    cash: float
    account_value: float
    basis: dict[int, float]

    @classmethod
    def from_platform(cls, balance: float, positions: list[dict]) -> "Book":
        basis: dict[int, float] = {}
        for p in positions:
            basis[p["marketId"]] = basis.get(p["marketId"], 0) + p["tokenAmount"] * p["avgCostBasis"]
        return cls(balance, balance + sum(p.get("currentValue", 0) for p in positions), basis)


def resolve_mode(cfg: dict) -> tuple[str, str | None]:
    """(mode, warning). live without the environment phrase falls back to dry_run."""
    mode = cfg.get("mode", "monitor")
    if mode not in ("monitor", "dry_run", "live"):
        return "monitor", f"unknown mode {mode!r} in config.yaml; running as monitor"
    if mode == "live" and os.environ.get(LIVE_ENV) != LIVE_PHRASE:
        return "dry_run", f"mode is live but {LIVE_ENV}={LIVE_PHRASE} is not set; running as dry_run"
    return mode, None


def plan(row: dict, cfg: dict, book: Book) -> tuple[Order | None, str]:
    """The one order this market warrants right now, or None and the reason."""
    t = cfg["trading"]
    if not row["data_ok"]:
        return None, "no fair value"

    for held in row["holdings"]:
        yes = held["outcome"] == "YES"
        price = row["yes"] if yes else row["no"]
        fair = row["p_yes"] if yes else 1 - row["p_yes"]
        if price - fair >= cfg["take_profit_buffer"]:
            tokens = min(held["tokens"], t["max_order_gies"] / price)
            return Order(row["id"], "sell", held["outcome"].lower(), tokens, "tokens", fair, limit=fair), "held side is rich"

    if not row["actionable"]:
        return None, "no edge"
    if any(h["outcome"] != row["side"] for h in row["holdings"]):
        return None, "holding the other side"
    limit = row["fair"] - t["buy_buffer"]
    if row["price"] >= limit:
        return None, "price is already at the buy limit"
    room = min(
        t["max_order_gies"], book.cash,
        t["max_per_market_pct"] * book.account_value - book.basis.get(row["id"], 0),
        t["max_total_exposure_pct"] * book.account_value - sum(book.basis.values()),
    )
    if room < t["min_order_gies"]:
        return None, "at a position cap or out of cash"
    return Order(row["id"], "buy", row["side"].lower(), room, "gies", row["fair"], limit), "edge"


def check_quote(order: Order, quote: dict, cfg: dict) -> str | None:
    """Why this quote must not be traded, or None if it is acceptable."""
    if quote.get("isBlocked"):
        return "the platform would block it"
    fill = quote.get("pricePerToken")
    after = quote.get("newYesPrice" if order.outcome == "yes" else "newNoPrice")
    if not (fill and after and quote.get("tokenAmount", 0) > 0 and 0 < fill < 1 and 0 < after < 1):
        return "the quote looks malformed"
    if quote.get("priceImpact", 0) / 100 > cfg["trading"]["max_price_impact"]:  # quoted as a percent
        return f"price impact {quote['priceImpact']:.1f}% is over the limit"
    if order.side == "buy" and max(fill, after) > order.limit:
        return f"it would pay {cents(fill)} and push the price to {cents(after)}, past the {cents(order.limit)} limit"
    if order.side == "sell" and min(fill, after) < order.limit:
        return f"it would receive {cents(fill)} and push the price to {cents(after)}, under fair value {cents(order.limit)}"
    return None


class TradingHalted(Exception):
    pass


class Executor:
    def __init__(self, cycle, mode: str, stop_file: Path):
        self.cycle, self.mode, self.stop_file = cycle, mode, stop_file
        self.cfg, self.store, self.platform, self.now = cycle.cfg, cycle.store, cycle.platform, cycle.now

    def blocked_reason(self, book: Book) -> str | None:
        """A reason not to trade at all this cycle. Applies to dry_run too, so it mirrors live."""
        if self.stop_file.exists():
            return f"kill switch: {self.stop_file.name} file exists"
        halted = self.store.get("trading_halted")
        if halted:
            return halted

        # Daily loss limit: compare with the best account value seen in the last 24 hours.
        cutoff = (self.now - timedelta(hours=24)).isoformat()
        values = [v for v in self.store.get("account_values", []) if v[0] >= cutoff]
        values.append([self.now.isoformat(), book.account_value])
        self.store.set("account_values", values)
        peak, limit = max(v[1] for v in values), self.cfg["trading"]["daily_loss_limit_pct"]
        if book.account_value < peak * (1 - limit):
            reason = (f"account value fell from {peak:,.0f} to {book.account_value:,.0f} within 24 hours, "
                      f"more than the {pct(limit)} loss limit")
            self.store.set("trading_halted", reason)
            self.cycle.alert("loud", "Trading halted: loss limit", f"The {reason}.\nThe bot keeps monitoring. "
                             "To trade again, run: uv run python -m gies_bot resume", "octagonal_sign")
            return reason
        return None

    def run(self, rows: list[dict]) -> list[dict]:
        """Returns one record per market considered, for the log and the dashboard."""
        book = Book.from_platform(self.cycle.balance, self.cycle.positions)
        blocked = self.blocked_reason(book)
        if blocked:
            log.info("trading skipped: %s", blocked)
            return [{"status": "halted", "reason": blocked}]

        records, placed = [], 0
        for row in rows:
            entry = self.cfg["markets"].get(row["id"]) or {}
            if not entry.get("trade"):
                continue
            order, reason = plan(row, self.cfg, book)
            if order is None:
                self.cycle.changed(f"order:{row['id']}", None)
                records.append({"market": row["id"], "status": "none", "reason": reason})
                continue
            if placed >= self.cfg["trading"]["max_orders_per_cycle"]:
                records.append({"market": row["id"], "status": "none", "reason": "order limit for this cycle reached"})
                continue
            try:
                record = self.place(row, order, book)
            except TradingHalted as exc:
                records.append({"market": row["id"], "status": "error", "reason": str(exc)})
                break
            placed += record["status"] in ("filled", "dry_run")
            records.append(record)
        return records

    def quote_down(self, order: Order) -> tuple[dict | None, str]:
        """Quote the order, halving it while the quote is unacceptable."""
        smallest = self.cfg["trading"]["min_order_gies"]
        if order.amount_type == "tokens":
            smallest /= max(order.fair, 0.01)
        reason = ""
        for _ in range(QUOTE_ATTEMPTS):
            quote = self.platform.quote(order.body())
            reason = check_quote(order, quote, self.cfg)
            self.record(order, "quoted" if reason is None else "quote_rejected", reason, quote)
            if reason is None:
                return quote, ""
            if order.amount / 2 < smallest or "malformed" in reason:
                break
            order.amount /= 2
        return None, reason

    def place(self, row: dict, order: Order, book: Book) -> dict:
        name, base = row["name"], {"market": row["id"], "order": order.describe()}
        try:
            quote, reason = self.quote_down(order)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("quote failed for market %s: %r", row["id"], exc)
            return {**base, "status": "error", "reason": f"quote failed: {exc!r}"}
        if quote is None:
            log.info("%s: no order, %s", name, reason)
            return {**base, "status": "none", "reason": reason}

        tokens, gies, fill = quote["tokenAmount"], abs(quote["giesCost"]), quote["pricePerToken"]
        verb = "Buy" if order.side == "buy" else "Sell"
        what = f"{tokens:,.0f} {order.outcome.upper()} at {cents(fill)} ({gies:,.0f} GIES)"
        why = f"Model says {pct(order.fair)}. Price after the order: {cents(quote['newYesPrice'])} YES."
        # Advisory only: thin pools warn on nearly any size. Our own impact limit and isBlocked decide.
        warnings = "; ".join(map(str, quote.get("safeguardWarnings") or []))
        if warnings:
            why += f"\nPlatform note: {warnings}."

        if self.mode != "live":
            self.record(order, "dry_run", warnings or None, quote)
            if self.cycle.changed(f"order:{row['id']}", order.fair - fill):  # positive for buys, negative for sells
                self.cycle.alert("quiet", f"{name}: would {verb.lower()} {order.outcome.upper()} (dry run)",
                                 f"{verb} {what}.\n{why}\nNothing was traded.", "test_tube")
            return {**base, "status": "dry_run", "reason": what}

        try:
            result = self.platform.trade(order.body())
        except httpx.HTTPStatusError as exc:  # the platform answered and refused: nothing was traded
            detail = exc.response.text[:200]
            self.record(order, "refused", detail, quote)
            self.cycle.alert("loud", f"{name}: order refused", f"Tried to {order.describe()}.\nPlatform said: {detail}", "warning")
            return {**base, "status": "error", "reason": detail}
        except httpx.HTTPError as exc:  # no answer: it may or may not have traded, so do not retry
            self.record(order, "unknown", repr(exc), quote)
            self.cycle.alert("loud", f"{name}: order status unknown",
                             f"Tried to {order.describe()} and got no answer ({exc!r}).\nIt may or may not have gone "
                             "through. No more orders this cycle; the next cycle starts from your actual positions.", "warning")
            raise TradingHalted("an order got no answer") from exc

        trade = result.get("trade", {})
        self.record(order, "filled", warnings or None, quote, result)
        real_fill = trade.get("pricePerToken", fill)
        real_gies = abs(trade.get("giesCost", gies))
        if order.side == "buy":
            book.cash -= real_gies
            book.basis[row["id"]] = book.basis.get(row["id"], 0) + real_gies
        else:
            book.cash += real_gies
        slip = f"\nFilled at {cents(real_fill)} against a quote of {cents(fill)}." if abs(real_fill - fill) > MAX_FILL_SLIPPAGE else ""
        self.cycle.alert("loud", f"{name}: {'bought' if order.side == 'buy' else 'sold'} {order.outcome.upper()}",
                         f"{verb} {trade.get('tokenAmount', tokens):,.0f} {order.outcome.upper()} at {cents(real_fill)} "
                         f"({real_gies:,.0f} GIES).\nModel says {pct(order.fair)}. Price is now {cents(result.get('newYesPrice', 0))} YES.\n"
                         f"Cash left: {result.get('newGiesBalance', book.cash):,.0f}.{slip}"
                         + (f"\nPlatform note: {warnings}." if warnings else ""), "handshake")
        return {**base, "status": "filled", "reason": what}

    def record(self, order: Order, status: str, reason: str | None, quote: dict | None, trade: dict | None = None) -> None:
        self.store.log_order(self.now.isoformat(), self.mode, order, status, reason,
                             json.dumps(quote) if quote else None, json.dumps(trade) if trade else None)
