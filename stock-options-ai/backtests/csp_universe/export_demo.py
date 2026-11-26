"""Step 6: the handful of replay results the public demo shows, as JSON.

    python export_demo.py > ../../../forge-view/components/demo/optionsReplay.json
"""
from __future__ import annotations

import json
import sys

from common import load


def row(res: dict, key: str, name: str, live: bool = False) -> dict:
    s = res["rules"].get(key) or res["arms"]["ride"].get(key) or res["sens"][key]
    return {"name": name, "cagr": round(s["cagr"] * 100, 1), "mdd": round(s["mdd"] * 100, 1),
            "trades": s["trades"], "win": round(s["win"] * 100), "live": live}


def main() -> None:
    res = load("results.json")
    arm = lambda k, name, live=False: {**row({**res, "rules": res["arms"]["ride"]}, k, name, live)}   # noqa: E731
    out = {
        "from": res["window"][0], "to": res["window"][1], "account": 100_000,
        "spy_cagr": round(res["spy_cagr"] * 100, 1),
        "candidates": res["candidates"], "pass_2pct": res["pass_2pct"],
        "groups": [
            {"title": "How a losing trade is handled", "rows": [
                row(res, "base|ride: hold losers to expiry", "Held to expiry, shares taken if it comes to that", True),
                row(res, "base|cut: buy back at 2.5x, 4x at the latest", "Bought back at 2.5 times the payment", True),
                row(res, "base|4x exit only", "Bought back only at 4 times the payment"),
            ]},
            {"title": "When a winning trade is closed", "rows": [
                row(res, "base|take profit at 50%", "Once half the payment is earned"),
                row(res, "base|take profit at 65%", "Once 65% is earned"),
                row(res, "base|ride: hold losers to expiry", "Once 80% is earned", True),
                row(res, "base|never take profit early", "Never: held to expiry"),
            ]},
            {"title": "The least a trade must pay", "rows": [
                row(res, "base|monthly floor 1.0%", "1% a month"),
                row(res, "base|ride: hold losers to expiry", "2% a month", True),
                row(res, "base|monthly floor 3.0%", "3% a month"),
            ]},
            {"title": "Which stocks it may trade", "rows": [
                arm("base", "Today's list of about 90, up to $100 a share", True),
                arm("base_nocap", "Today's list, any share price"),
                arm("wide", "The 300 most-traded US stocks, any share price"),
            ]},
        ],
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
