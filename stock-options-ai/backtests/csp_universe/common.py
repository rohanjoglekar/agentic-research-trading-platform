"""Shared plumbing for the put-selling universe backtest: throttled Alpaca
access and an on-disk cache. Research code — it only READS market data."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SOAI = HERE.parents[1]
sys.path.insert(0, str(SOAI))

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY  # noqa: E402

CACHE = HERE.parent / "cache" / "csp_universe"
DATA_HOST = "https://data.alpaca.markets"
TRADING_HOST = "https://paper-api.alpaca.markets"
START = "2024-01-02"          # option bars exist from Feb 2024
END = "2026-11-20"            # the November 2026 monthly expiration
# The free plan refuses SIP data from the last 15 minutes, and a bar that is
# still forming counts as recent. Stock queries therefore end at a timestamp
# safely in the past (after the 20 November close).
STOCK_END = "2026-11-20T21:20:00Z"


class Limiter:
    """At most `rate` calls a second, shared across threads."""

    def __init__(self, rate: float):
        self.gap = 1.0 / rate
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.gap
        if at > now:
            time.sleep(at - now)


# The free plan allows 200 requests a minute per host. The live paper trader
# shares this key, so stay well under it.
LIMITS = {DATA_HOST: Limiter(2.9), TRADING_HOST: Limiter(2.9)}
_session = threading.local()


def _sess() -> requests.Session:
    s = getattr(_session, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"APCA-API-KEY-ID": ALPACA_API_KEY,
                          "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
                          "Accept": "application/json"})
        _session.s = s
    return s


def get(host: str, path: str, params: dict | None = None, tries: int = 6):
    for attempt in range(tries):
        LIMITS[host].wait()
        try:
            r = _sess().get(host + path, params=params or {}, timeout=30)
        except requests.RequestException:
            time.sleep(2 + 3 * attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(5 + 5 * attempt)
            continue
        raise RuntimeError(f"Alpaca {r.status_code} on {path}: {r.text[:200]}")
    raise RuntimeError(f"Alpaca gave up on {path} after {tries} tries")


def paged(host: str, path: str, params: dict, key: str):
    """Yield every page's `key` payload, following next_page_token."""
    params = dict(params)
    while True:
        d = get(host, path, params)
        yield d.get(key)
        tok = d.get("next_page_token")
        if not tok:
            return
        params["page_token"] = tok


def load(name: str, default=None):
    p = CACHE / name
    if not p.exists():
        return default
    return json.loads(p.read_text())


def save(name: str, obj) -> None:
    p = CACHE / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)
