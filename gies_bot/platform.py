"""Client for the prediction market. It can only place trades when built with allow_trading=True."""

import logging
import time

import httpx

BASE_URL = "https://i-block-prediction-market.replit.app"
log = logging.getLogger(__name__)


class AuthError(Exception):
    pass


class Platform:
    def __init__(self, token: str, http: httpx.Client | None = None, min_interval: float = 1.0,
                 allow_trading: bool = False):
        self.http = http or httpx.Client(base_url=BASE_URL, timeout=30)
        self.http.headers["Authorization"] = f"Bearer {token}"
        self.min_interval, self.allow_trading = min_interval, allow_trading
        self._last = 0.0

    def _wait(self) -> None:
        time.sleep(max(0.0, self._last + self.min_interval - time.monotonic()))
        self._last = time.monotonic()

    def _request(self, method: str, path: str, body: dict | None = None):
        """For calls that are safe to repeat: retries on 429 and 5xx."""
        for attempt in range(4):
            self._wait()
            resp = self.http.request(method, path, json=body)
            if resp.status_code in (401, 403):
                raise AuthError(path)
            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("%s %s -> %s, backing off", method, path, resp.status_code)
                time.sleep(self.min_interval * 2 ** (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    def _get(self, path: str):
        return self._request("GET", path)

    def quote(self, order: dict) -> dict:
        """Preview an order. Nothing is executed."""
        return self._request("POST", "/api/trades/quote", order)

    def trade(self, order: dict) -> dict:
        """Execute an order. Sent exactly once: a repeat could trade twice."""
        if not self.allow_trading:
            raise RuntimeError("this client was not built with allow_trading=True")
        self._wait()
        resp = self.http.post("/api/trades", json=order)
        if resp.status_code in (401, 403):
            raise AuthError("/api/trades")
        resp.raise_for_status()
        return resp.json()

    def me(self) -> dict:
        return self._get("/api/auth/me")

    def markets(self) -> list[dict]:
        return self._get("/api/markets")

    def positions(self) -> list[dict]:
        return self._get("/api/wallet/positions")
