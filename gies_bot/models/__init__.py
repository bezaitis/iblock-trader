"""Market models. One file per model type; the file name is the model name used in config.yaml.

Each model file defines:

    def fair_prob(market: dict, params: dict, ctx: Ctx) -> ModelResult

and fetches its own data through ctx.get_json / ctx.get_text.
"""

import importlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

log = logging.getLogger(__name__)


@dataclass
class ModelResult:
    p_yes: float | None = None
    confidence: str = "low"            # low | med | high
    data_ok: bool = False              # False means: never act on p_yes
    notes: str = ""                    # one human-readable line for alerts
    inputs: dict = field(default_factory=dict)
    events: list[str] = field(default_factory=list)  # one-off news, each alerted once


class FetchError(Exception):
    pass


class Ctx:
    """What a model gets to work with: the clock, config, state, and cached HTTP."""

    def __init__(self, cfg: dict, store, now: datetime, http: httpx.Client | None = None):
        self.cfg, self.store, self.now = cfg, store, now
        # ESPN returns 403 to agent strings containing "bot" or an email address.
        self.agent = "gies-market-monitor/0.1"
        self.http = http or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": self.agent})

    def get_text(self, url: str, max_age: float | None) -> tuple[str, float]:
        """Return (body, age in seconds). max_age=None caches forever.

        If a refresh fails, the old copy is returned with its true age so the
        model can decide whether it is too stale to use.
        """
        cached = self.store.cache_get(url)
        if cached and (max_age is None or time.time() - cached[1] <= max_age):
            return cached[0], time.time() - cached[1]
        try:
            headers = {}
            if "api.weather.gov" in url:  # NWS asks for a contact; nobody else gets the address
                headers["User-Agent"] = f"{self.agent} ({os.environ.get('CONTACT_EMAIL') or 'no contact set'})"
            resp = self.http.get(url, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            if cached:
                log.warning("fetch failed, using cached copy: %s (%s)", url, exc)
                return cached[0], time.time() - cached[1]
            raise FetchError(f"{url}: {exc}") from exc
        self.store.cache_put(url, resp.text)
        return resp.text, 0.0

    def get_json(self, url: str, max_age: float | None) -> tuple[dict, float]:
        body, age = self.get_text(url, max_age)
        return json.loads(body), age

    def is_stale(self, age: float) -> bool:
        return age > self.cfg.get("stale_minutes", 120) * 60


def load(name: str):
    return importlib.import_module(f"gies_bot.models.{name}").fair_prob


def as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
