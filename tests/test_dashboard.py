import json
import threading

import httpx
import pytest

from gies_bot import dashboard
from gies_bot.config import apply_controls
from gies_bot.store import Store

CFG = {"mode": "monitor", "poll_minutes": 15, "markets": {7: {"model": "manual", "trade": False}, 8: {"model": "manual"}}}


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "load_config", lambda: json.loads(json.dumps(CFG), object_hook=lambda d: {
        (int(k) if k.isdigit() else k): v for k, v in d.items()}))
    db, stop = tmp_path / "gies.db", tmp_path / "STOP"
    server = dashboard.serve(db, 0, stop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    page = httpx.get(base).text
    token = page.split('const TOKEN = "')[1].split('"')[0]
    post = lambda body, **headers: httpx.post(f"{base}/control", json=body, headers={"X-Control-Token": token, **headers})
    yield {"base": base, "db": db, "stop": stop, "post": post, "page": page}
    server.shutdown()


def controls(site):
    return dashboard.controls_view(Store(site["db"]), site["stop"])


def test_switches_override_config_and_unknown_markets_are_ignored(tmp_path):
    store = Store(tmp_path / "x.db")
    store.set("controls", {"mode": "dry_run", "trade": {"7": True, "99": True}})
    cfg = apply_controls(json.loads(json.dumps(CFG), object_hook=lambda d: {(int(k) if k.isdigit() else k): v for k, v in d.items()}), store)
    assert cfg["mode"] == "dry_run" and cfg["markets"][7]["trade"] is True and 99 not in cfg["markets"]


def test_page_renders_before_any_cycle_has_run(site):
    assert "GIES market monitor" in site["page"] and "/*DATA*/" not in site["page"]


def test_mode_trade_and_pause_buttons(site, monkeypatch):
    monkeypatch.delenv("GIES_LIVE_TRADING", raising=False)
    assert site["post"]({"action": "mode", "value": "dry_run"}).status_code == 200
    assert site["post"]({"action": "trade", "market": 7, "value": True}).status_code == 200
    view = controls(site)
    assert view["mode"] == view["effective_mode"] == "dry_run" and view["trade"] == {7: True, 8: False}

    site["post"]({"action": "mode", "value": "live"})
    view = controls(site)
    assert view["mode"] == "live" and view["effective_mode"] == "dry_run" and view["live_blocked"]

    site["post"]({"action": "stop"})
    assert site["stop"].exists() and controls(site)["stopped"]
    Store(site["db"]).set("trading_halted", "loss limit")
    site["post"]({"action": "resume"})
    view = controls(site)
    assert not site["stop"].exists() and not view["stopped"] and view["halted"] is None


def test_rejects_bad_requests_and_other_origins(site):
    assert site["post"]({"action": "mode", "value": "yolo"}).status_code == 400
    assert site["post"]({"action": "trade", "market": "7", "value": 1}).status_code == 400
    assert httpx.post(f"{site['base']}/control", json={"action": "stop"}).status_code == 403
    assert site["post"]({"action": "stop"}, **{"X-Control-Token": "guess"}).status_code == 403
    assert site["post"]({"action": "stop"}, Host="evil.example").status_code == 404
    assert not site["stop"].exists() and controls(site)["mode"] == "monitor"
