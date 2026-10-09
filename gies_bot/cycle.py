"""One monitoring pass: price every market, compare to its model, send alerts."""

import logging
from datetime import datetime

from pathlib import Path

from . import alerts, models
from .executor import Executor
from .fmt import cents, pct
from .models import Ctx, ModelResult
from .platform import AuthError

log = logging.getLogger(__name__)


def run_model(market: dict, entry: dict, ctx: Ctx) -> ModelResult:
    try:
        return models.load(entry["model"])(market, entry.get("params") or {}, ctx)
    except Exception as exc:  # a broken model must not stop the other markets
        log.exception("model %s failed for market %s", entry.get("model"), market.get("id"))
        return ModelResult(notes=f"model error: {exc!r}")


def best_edge(market: dict, p_yes: float) -> tuple[str, float, float, float]:
    """(side, fair, price, edge) for the better side, with the trading fee taken off."""
    fee = 1 + market.get("tradingFeeRate", 0)
    yes = ("YES", p_yes, market["yesPrice"], p_yes - market["yesPrice"] * fee)
    no = ("NO", 1 - p_yes, market["noPrice"], (1 - p_yes) - market["noPrice"] * fee)
    return max(yes, no, key=lambda side: side[3])


def evaluate(market: dict, result: ModelResult | None, positions: list[dict], cfg: dict) -> dict:
    """Everything the alerts, summary and dashboard need to know about one market."""
    entry = cfg["markets"].get(market["id"]) or {}
    row = {
        "id": market["id"], "name": entry.get("name") or market["title"][:40], "title": market["title"],
        "yes": market["yesPrice"], "no": market["noPrice"], "has_model": result is not None,
        "data_ok": bool(result and result.data_ok), "p_yes": None, "side": None, "edge": None,
        "actionable": False, "confidence": result.confidence if result else None,
        "notes": result.notes if result else "",
        "holdings": [{"outcome": p["outcome"].upper(), "tokens": p["tokenAmount"], "cost": p["avgCostBasis"],
                      "value": p.get("currentValue"), "pnl": p.get("unrealizedPnl")}
                     for p in positions if p["marketId"] == market["id"] and p["tokenAmount"] > 0],
    }
    if row["data_ok"]:
        side, fair, price, edge = best_edge(market, result.p_yes)
        needed = cfg["min_edge_low_confidence"] if result.confidence == "low" else cfg["min_edge"]
        row.update(p_yes=result.p_yes, side=side, fair=fair, price=price, edge=edge, actionable=edge >= needed)
    return row


def holding_text(row: dict) -> str:
    return ", ".join(f"{h['tokens']:,.0f} {h['outcome']} bought at {cents(h['cost'])}" for h in row["holdings"])


class Cycle:
    def __init__(self, cfg: dict, store, platform, ctx: Ctx, mode: str = "monitor", stop_file: Path = Path("STOP")):
        self.cfg, self.store, self.platform, self.ctx = cfg, store, platform, ctx
        self.mode, self.stop_file = mode, stop_file
        self.now: datetime = ctx.now
        self.positions: list[dict] = []
        self.balance = None

    def alert(self, level: str, title: str, body: str, tag: str | None = None) -> None:
        self.store.log_alert(self.now.isoformat(), level, title, body)
        alerts.send(level, title, body, tag=tag)

    def once(self, key: str) -> bool:
        """True the first time a key is seen."""
        if self.store.get(f"once:{key}"):
            return False
        self.store.set(f"once:{key}", self.now.isoformat())
        return True

    def changed(self, key: str, value: float | None) -> bool:
        """True if this alert is new, moved enough, or is due a repeat. None clears it."""
        if value is None:
            self.store.set(f"alert:{key}", None)
            return False
        last = self.store.get(f"alert:{key}")
        if last:
            hours = (self.now - datetime.fromisoformat(last["at"])).total_seconds() / 3600
            if abs(value - last["value"]) < self.cfg["alert_edge_change"] and hours < self.cfg["alert_repeat_hours"]:
                return False
        self.store.set(f"alert:{key}", {"value": value, "at": self.now.isoformat()})
        return True

    def run(self) -> list[dict]:
        try:
            markets = self.platform.markets()
            self.positions = self.platform.positions()
            self.balance = self.platform.me().get("giesBalance")
        except AuthError:
            self.login_expired()
            return []
        self.changed("auth", None)

        rows = []
        configured = self.cfg["markets"]
        for market in sorted(markets, key=lambda m: m["id"]):
            mid = market["id"]
            if market["status"] != "open":
                if mid in configured and self.once(f"closed:{mid}"):
                    name = configured[mid].get("name") or market["title"][:40]
                    self.alert("quiet", f"{name}: market {market['status']}",
                               f"Outcome: {market.get('outcome') or 'not posted yet'}.\n"
                               f"You can remove market {mid} from config.yaml.", "checkered_flag")
                continue
            if mid not in configured:
                if self.once(f"unmapped:{mid}"):
                    self.alert("loud", f"New market: {market['title'][:60]}",
                               f"YES is at {cents(market['yesPrice'])}. No model yet.\n"
                               f"Ask Claude Code: \"add a model for market {mid}\".\n\n"
                               f"{market.get('resolutionCriteria', '')}", "new")
                rows.append(evaluate(market, None, self.positions, self.cfg))
                continue

            result = run_model(market, configured[mid], self.ctx)
            self.store.snapshot(self.now.isoformat(), market, result)
            row = evaluate(market, result, self.positions, self.cfg)
            rows.append(row)
            for event in result.events:
                if self.once(f"event:{mid}:{event}"):
                    self.alert("quiet", f"{row['name']}: news", event, "newspaper")
            self.check_data(row)
            if row["data_ok"]:
                self.check_edge(row)
                self.check_holdings(row)

        orders = []
        if self.mode != "monitor":
            try:
                orders = Executor(self, self.mode, self.stop_file).run(rows)
            except AuthError:
                self.login_expired()

        value = None if self.balance is None else self.balance + sum(p.get("currentValue", 0) for p in self.positions)
        self.store.set("state", {"at": self.now.isoformat(), "balance": self.balance, "account_value": value,
                                 "rows": rows, "mode": self.mode, "orders": orders})
        self.daily_summary(rows)
        return rows

    def login_expired(self) -> None:
        if self.changed("auth", 1.0):
            self.alert("loud", "Login expired", "The market site rejected the saved token.\n"
                       "Log in again, copy the new token into .env as GIES_TOKEN, and restart the bot.", "warning")

    def check_data(self, row: dict) -> None:
        key = f"bad_since:{row['id']}"
        if row["data_ok"]:
            self.store.set(key, None)
            return
        since = self.store.get(key)
        if not since:
            self.store.set(key, self.now.isoformat())
            return
        hours = (self.now - datetime.fromisoformat(since)).total_seconds() / 3600
        if hours >= self.cfg["stale_alert_hours"] and self.once(f"bad:{row['id']}:{since}"):
            self.alert("loud", f"{row['name']}: no fair value for {hours:.0f} hours",
                       f"The model can't produce a number, so there are no edge alerts for this market.\n\n"
                       f"Reason: {row['notes']}", "warning")

    def check_edge(self, row: dict) -> None:
        key = f"edge:{row['id']}"
        if not row["actionable"]:
            self.changed(key, None)
            return
        side, edge = row["side"], row["edge"]
        if not self.changed(key, edge if side == "YES" else -edge):
            return
        body = (f"{side} costs {cents(row['price'])}. Model says {pct(row['fair'])}.\n"
                f"Edge after fee: +{edge * 100:.0f} points. Confidence: {row['confidence']}.\n\n{row['notes']}")
        if row["holdings"]:
            body += f"\n\nYou hold {holding_text(row)}."
        self.alert("loud", f"{row['name']}: {side} looks cheap (+{edge * 100:.0f})", body, "chart_with_upwards_trend")

    def check_holdings(self, row: dict) -> None:
        for held in row["holdings"]:
            yes = held["outcome"] == "YES"
            price = row["yes"] if yes else row["no"]
            fair = row["p_yes"] if yes else 1 - row["p_yes"]
            key = f"sell:{row['id']}:{held['outcome']}"
            if price - fair < self.cfg["take_profit_buffer"]:
                self.changed(key, None)
            elif self.changed(key, price - fair):
                self.alert("loud", f"{row['name']}: consider selling {held['outcome']}",
                           f"Your {held['outcome']} trades at {cents(price)}, but the model says it is worth {pct(fair)}.\n"
                           f"You hold {held['tokens']:,.0f} bought at {cents(held['cost'])}.\n\n{row['notes']}", "moneybag")

    def daily_summary(self, rows: list[dict]) -> None:
        today = self.now.date()
        if self.now.hour < self.cfg["daily_summary_hour"] or self.store.get("summary_date") == today.isoformat():
            return
        self.store.set("summary_date", today.isoformat())
        self.alert("quiet", f"Daily summary, {today:%a %b} {today.day}",
                   summary_text(rows, self.positions, self.balance), "clipboard")


def summary_text(rows: list[dict], positions: list[dict], balance) -> str:
    blocks = []
    for row in rows:
        line = f"YES {cents(row['yes'])}"
        if not row["has_model"]:
            line += " · no model yet"
        elif not row["data_ok"]:
            line += f" · no fair value ({row['notes'][:70]})"
        else:
            verdict = f"{row['side']} +{row['edge'] * 100:.0f}" if row["actionable"] else "no edge"
            line += f" · model {pct(row['p_yes'])} · {verdict}"
        block = f"{row['name']}\n  {line}"
        for h in row["holdings"]:
            pnl = f" ({h['pnl']:+,.0f})" if h["pnl"] is not None else ""
            block += f"\n  Hold {h['tokens']:,.0f} {h['outcome']}{pnl}"
        blocks.append(block)
    if balance is not None:
        value = balance + sum(p.get("currentValue", 0) for p in positions)
        blocks.append(f"Cash {balance:,.0f} · Account {value:,.0f}")
    return "\n\n".join(blocks)
