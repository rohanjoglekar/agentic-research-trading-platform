"""The owner's brokerage snapshot, as far as the research code is concerned.

Research does not need an account: the screen grades the same universe and one
company's page is built the same way without one. The private dashboard adds
the owner's position to a research answer in two places (a "held" mark on the
screen, a position on a company's page), and those two places ask here.

A checkout that does not carry the account adapter has no account, and the
research code runs exactly as it does for the public gateway. The public
repository is such a checkout.
"""
from __future__ import annotations

import importlib

_ADAPTER = "adapters.robinhood"


def snapshot() -> dict:
    """The latest account snapshot, or {} when there is no account."""
    try:
        adapter = importlib.import_module(_ADAPTER)
    except ModuleNotFoundError as e:
        if e.name != _ADAPTER:
            raise                        # a broken adapter must stay loud
        return {}
    return adapter._snap() or {}
