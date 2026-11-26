"""What the public read-only gateway may see.

The public site (forge-view, on Vercel and on the VPS) reaches this backend
only through an nginx gateway that adds `X-Forge-View-Only: 1` to every
request. The owner's dashboard comes in through a different server block,
behind Basic Auth, and never carries that header.

A page that does not show the brokerage account (holdings, orders, watchlists,
risk, options positions, the account-against-the-index curve) is not a
boundary: an address no page links to still answers whoever asks for it. This
module is the boundary, enforced where the data is served:

- `allowed(path)`: the namespaces that carry account data are closed by
  default. A path under them is public only if it is listed here, so a route
  added later is private until someone decides otherwise.
- `scrub_*`: the shared payloads (research screen, one symbol) with every field
  that says what the owner holds removed.

Dropping fields is not enough when what is left still depends on the account.
One company's page is therefore built for the public without opening the
account snapshot at all (`rh_analytics.get_symbol(account=False)`,
`rh_ai_reads.public_read`): the price comes from the cached closes for every
name, and an AI read is shown only for a company the screen carries. The tests
compare whole public answers across different accounts and require them equal.

Paper-trading accounts (the options wheel, the equity book) are not personal
data and are untouched: they are what the public site exists to show.
"""
from __future__ import annotations

import re

HEADER = "x-forge-view-only"

# Namespaces holding the owner's account data: closed unless listed below.
_CLOSED = ("/api/robinhood", "/api/stock-options", "/api/ingest", "/api/status")
_SYM = r"[A-Za-z.]{1,8}"
_PUBLIC = re.compile(
    r"^/api/(?:"
    r"robinhood/research"                         # the ~600-company screen (scrubbed)
    r"|robinhood/research/history"                # grade changes over time
    r"|robinhood/factor-performance"              # does each signal predict returns
    r"|robinhood/deep-dcf"                        # accepted valuation models (scrubbed)
    rf"|robinhood/deep-dcf/{_SYM}/sheet"          # one model's computed values
    rf"|robinhood/deep-dcf/{_SYM}/model\.xlsx"    # the handler itself answers 404 to the public
    rf"|robinhood/chart/{_SYM}"                   # price history
    rf"|robinhood/symbol/{_SYM}"                  # one company's research (scrubbed)
    r"|stock-options/scan"                        # the morning put-selling scan
    r")$")

# Research rows: what the owner holds, and the gain or loss on it.
_ROW_PRIVATE = ("held", "pl_pct")
# One symbol: the position, the orders in it, the watchlists it sits on.
_SYMBOL_PRIVATE = ("held", "position", "orders", "watchlists")


def is_view_only(request) -> bool:
    """True for a request that came through the public gateway. A public caller
    cannot remove a header the gateway adds, so this fails toward redaction."""
    return request.headers.get(HEADER) == "1"


def allowed(path: str) -> bool:
    """May the public gateway be answered on this path?"""
    p = re.sub(r"/{2,}", "/", path or "").rstrip("/")
    low = p.lower()
    if not any(low == ns or low.startswith(ns + "/") for ns in _CLOSED):
        return True
    return bool(_PUBLIC.match(p))


def scrub_research(payload):
    """The research screen without `held`, `pl_pct` or the `held_extra` list
    (held companies that the screen does not otherwise carry)."""
    if not isinstance(payload, dict):
        return payload
    out = {k: v for k, v in payload.items() if k != "held_extra"}
    if isinstance(out.get("stocks"), list):
        out["stocks"] = [{k: v for k, v in r.items() if k not in _ROW_PRIVATE} if isinstance(r, dict) else r
                         for r in out["stocks"]]
    return out


def scrub_symbol(payload):
    """One company's research without the position, orders or watchlists. Of
    `ai_verdict` only the weekly AI read survives (`ai_*`: thesis, risk, rank).
    The verdict and reason come from a local-model deep dive that was shown the
    position, and the action fields are about the account."""
    if not isinstance(payload, dict) or payload.get("error"):
        return payload
    out = {k: v for k, v in payload.items() if k not in _SYMBOL_PRIVATE}
    av = out.get("ai_verdict")
    if isinstance(av, dict):
        kept = {k: v for k, v in av.items() if k.startswith("ai_")}
        out["ai_verdict"] = kept if any(v is not None for v in kept.values()) else None
    return out
