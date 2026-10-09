"""Dashboard on localhost: shows what the monitor saved in SQLite and holds the trading switches."""

import json
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import apply_controls, load_config
from .executor import LIVE_ENV, LIVE_PHRASE, resolve_mode
from .store import Store

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("dashboard.html")
HISTORY_DAYS = 30
RECENT_ALERTS = 40
MODES = ("monitor", "dry_run", "live")


def controls_view(store: Store, stop_file: Path) -> dict:
    """The switches as they stand right now, which may be newer than the last cycle."""
    cfg = apply_controls(load_config(), store)
    effective, _ = resolve_mode(cfg)
    return {
        "mode": cfg.get("mode", "monitor"), "effective_mode": effective,
        "live_blocked": cfg.get("mode") == "live" and effective != "live",
        "live_hint": f"{LIVE_ENV}={LIVE_PHRASE}",
        "stopped": stop_file.exists(), "halted": store.get("trading_halted"),
        "trade": {mid: bool(entry.get("trade")) for mid, entry in cfg["markets"].items()},
        "poll_minutes": cfg.get("poll_minutes", 15),
    }


def collect(db_path: Path, stop_file: Path) -> dict:
    store = Store(db_path)
    db = store.db
    try:
        since = (datetime.now().astimezone() - timedelta(days=HISTORY_DAYS)).isoformat()
        history: dict[int, list] = {}
        for ts, mid, yes, p_yes, ok in db.execute(
            "SELECT ts, market_id, yes_price, p_yes, data_ok FROM snapshots WHERE ts >= ? ORDER BY ts", (since,)
        ):
            history.setdefault(mid, []).append([ts, yes, p_yes if ok else None])
        recent = db.execute(
            "SELECT ts, level, title, body FROM alerts ORDER BY ts DESC LIMIT ?", (RECENT_ALERTS,)
        ).fetchall()
        orders = db.execute(
            "SELECT ts, mode, market_id, side, outcome, amount, amount_type, status, reason FROM orders "
            "WHERE status NOT IN ('quoted', 'quote_rejected') ORDER BY rowid DESC LIMIT 25"
        ).fetchall()
        return {
            "state": store.get("state"),
            "controls": controls_view(store, stop_file),
            "history": history,
            "alerts": [{"ts": ts, "level": level, "title": title, "body": body} for ts, level, title, body in recent],
            "orders": [dict(zip(("ts", "mode", "market", "side", "outcome", "amount", "unit", "status", "reason"), o))
                       for o in orders],
        }
    finally:
        db.close()


def apply_control(db_path: Path, stop_file: Path, request: dict) -> None:
    """Flip one switch. Raises ValueError on anything unexpected."""
    store = Store(db_path)
    try:
        controls = store.get("controls") or {}
        action = request.get("action")
        if action == "mode" and request.get("value") in MODES:
            controls["mode"] = request["value"]
        elif action == "trade" and isinstance(request.get("market"), int) and isinstance(request.get("value"), bool):
            controls.setdefault("trade", {})[str(request["market"])] = request["value"]
        elif action == "stop":
            stop_file.touch()
        elif action == "resume":
            stop_file.unlink(missing_ok=True)
            store.set("trading_halted", None)
            store.set("account_values", [])  # the loss limit measures from now
        else:
            raise ValueError(f"unknown control: {request!r}")
        store.set("controls", controls)
        log.info("dashboard control: %s", request)
    finally:
        store.db.close()


def serve(db_path: Path, port: int, stop_file: Path) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(24)  # only a page this server rendered can flip switches

    class Handler(BaseHTTPRequestHandler):
        def local(self) -> bool:
            # Refuse other sites reaching this port through the browser (DNS rebinding).
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def reply(self, status: int, body: bytes, kind: str = "text/plain; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.local() or self.path.split("?")[0] != "/":
                return self.reply(404, b"not found")
            data = json.dumps(collect(db_path, stop_file)).replace("</", "<\\/")  # page text can't close the script tag
            page = PAGE.read_text().replace("/*DATA*/null", data).replace("/*TOKEN*/", token)
            self.reply(200, page.encode(), "text/html; charset=utf-8")

        def do_POST(self):
            if not self.local() or self.path != "/control":
                return self.reply(404, b"not found")
            if not secrets.compare_digest(self.headers.get("X-Control-Token") or "", token):
                return self.reply(403, b"reload the dashboard and try again")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                apply_control(db_path, stop_file, json.loads(self.rfile.read(min(length, 10_000))))
            except (ValueError, sqlite3.Error) as exc:
                return self.reply(400, str(exc).encode())
            self.reply(200, b"ok")

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)  # localhost only; use an SSH tunnel remotely


def start_in_background(db_path: Path, port: int, stop_file: Path) -> None:
    try:
        server = serve(db_path, port, stop_file)
    except OSError as exc:
        log.warning("dashboard not started on port %s: %s", port, exc)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("dashboard at http://127.0.0.1:%s", port)
