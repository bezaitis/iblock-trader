import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    load_env()
    cfg = yaml.safe_load(path.read_text())
    cfg["markets"] = {int(k): v for k, v in (cfg.get("markets") or {}).items()}
    return cfg


def apply_controls(cfg: dict, store) -> dict:
    """Lay the dashboard's switches over config.yaml, which stays the default."""
    controls = store.get("controls") or {}
    if controls.get("mode"):
        cfg["mode"] = controls["mode"]
    for mid, flag in (controls.get("trade") or {}).items():
        if int(mid) in cfg["markets"]:
            cfg["markets"][int(mid)]["trade"] = bool(flag)
    return cfg
