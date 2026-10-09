"""python -m gies_bot <command>

  run         monitor forever, one cycle every poll_minutes
  once        run a single cycle and print the summary
  models      run the configured models without touching the market (no token needed)
  markets     list the platform's markets and whether each has a model
  dashboard   serve the dashboard on its own (run also serves it)
  test-alert  send a test notification to your ntfy topic
  stop        kill switch: no orders until `resume` (monitoring and alerts continue)
  resume      clear the kill switch and any loss-limit halt
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from . import alerts, dashboard
from .config import ROOT, apply_controls, load_config
from .cycle import Cycle, run_model, summary_text
from .executor import resolve_mode
from .models import Ctx
from .platform import AuthError, Platform
from .store import Store

log = logging.getLogger("gies_bot")
STOP_FILE = ROOT / "STOP"


def setup_logging() -> None:
    handlers = [logging.StreamHandler(), RotatingFileHandler(ROOT / "gies.log", maxBytes=2_000_000, backupCount=3)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines print full URLs


def make_cycle(cfg: dict, store: Store) -> Cycle:
    token = os.environ.get("GIES_TOKEN")
    if not token:
        sys.exit("GIES_TOKEN is not set. Put it in .env (see README), or use `models` which needs no token.")
    mode, warning = resolve_mode(cfg)
    if warning:
        log.warning(warning)
    ctx = Ctx(cfg, store, datetime.now(ZoneInfo(cfg["timezone"])))
    return Cycle(cfg, store, Platform(token, allow_trading=mode == "live"), ctx, mode, STOP_FILE)


def cmd_once(cfg, store, args) -> None:
    cycle = make_cycle(cfg, store)
    rows = cycle.run()
    if rows:
        print(summary_text(rows, cycle.positions, cycle.balance))


def cmd_run(cfg, store, args) -> None:
    dashboard.start_in_background(ROOT / "gies.db", cfg.get("dashboard_port", 8765), STOP_FILE)
    period = cfg["poll_minutes"] * 60
    while True:
        cfg = apply_controls(load_config(), store)  # pick up config.yaml edits and dashboard switches
        try:
            make_cycle(cfg, store).run()
        except Exception as exc:
            log.exception("cycle failed")
            if (store.get("crash_alert_at") or 0) < time.time() - 6 * 3600:
                store.set("crash_alert_at", time.time())
                alerts.send("loud", "GIES bot cycle failed", repr(exc))
        time.sleep(period - time.time() % period)


def cmd_models(cfg, store, args) -> None:
    ctx = Ctx(cfg, store, datetime.now(ZoneInfo(cfg["timezone"])))
    for mid, entry in cfg["markets"].items():
        if args.market and mid != args.market:
            continue
        result = run_model({"id": mid}, entry, ctx)
        fair = f"{result.p_yes:.3f}" if result.p_yes is not None else "n/a"
        print(f"M{mid} {entry['model']}: p_yes {fair}, confidence {result.confidence}, data_ok {result.data_ok}")
        print(f"   {result.notes}")
        for event in result.events:
            print(f"   event: {event}")
        if args.verbose:
            print(json.dumps(result.inputs, indent=2, default=str))


def cmd_markets(cfg, store, args) -> None:
    try:
        markets = make_cycle(cfg, store).platform.markets()
    except AuthError:
        sys.exit("The platform rejected GIES_TOKEN. Log in again and update .env.")
    for market in sorted(markets, key=lambda m: m["id"]):
        entry = cfg["markets"].get(market["id"])
        print(f"M{market['id']} [{market['status']}] YES {market['yesPrice']:.2f}  "
              f"model: {entry['model'] if entry else 'NONE'}\n   {market['title']}")
        if args.verbose or (not entry and market["status"] == "open"):
            print(f"   description: {market.get('description')}\n   resolution: {market.get('resolutionCriteria')}")


def cmd_dashboard(cfg, store, args) -> None:
    port = cfg.get("dashboard_port", 8765)
    print(f"Dashboard at http://127.0.0.1:{port} (Ctrl+C to stop)")
    dashboard.serve(ROOT / "gies.db", port, STOP_FILE).serve_forever()


def cmd_stop(cfg, store, args) -> None:
    STOP_FILE.touch()
    print(f"Created {STOP_FILE}. No orders will be placed until you run `resume`.")


def cmd_resume(cfg, store, args) -> None:
    STOP_FILE.unlink(missing_ok=True)
    store.set("trading_halted", None)
    store.set("account_values", [])  # the loss limit measures from now
    print(f"Kill switch and halts cleared. Mode is {resolve_mode(cfg)[0]}.")


def cmd_test_alert(cfg, store, args) -> None:
    if not os.environ.get("NTFY_TOPIC"):
        sys.exit("NTFY_TOPIC is not set in .env.")
    alerts.send("loud", "Test alert", "If you can read this on your phone, alerts work.", tag="white_check_mark")


def main() -> None:
    parser = argparse.ArgumentParser(prog="gies_bot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "once", "models", "markets", "dashboard", "test-alert", "stop", "resume"])
    parser.add_argument("--market", type=int, help="only this market id (models)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging()
    store = Store(ROOT / "gies.db")
    cfg = apply_controls(load_config(), store)
    {"run": cmd_run, "once": cmd_once, "models": cmd_models, "markets": cmd_markets,
     "dashboard": cmd_dashboard, "test-alert": cmd_test_alert, "stop": cmd_stop, "resume": cmd_resume}[args.command](cfg, store, args)


if __name__ == "__main__":
    main()
