"""The public index of valuation models (deep_dcf/store.py).

A model carries the day and the price it was built at. One built because the
brokerage account holds a name, or wants to sell it, is built on the account's
schedule, so its date alone would say what is held. The job therefore keeps a
second index for the public site and writes to it only for models the account
had no part in.
"""
import json
from datetime import date

import pytest

from deep_dcf import store
from tests.deep_dcf_helpers import make_settings

VERDICT = {"verdict": "PASS", "errors": [], "warnings": [],
           "values": {"price": 30.0, "tv_fraction": 0.7, "upside_pct": {"base": 20.0},
                      "scenarios": {"base": {"fair_value": 36.0, "wacc": 0.09, "terminal_growth": 0.025}}}}


@pytest.fixture
def s(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _publish(s, tmp_path, symbol, run_id, day, **meta):
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    for name in ("model.xlsx", "model.recalc.xlsx"):
        (run_dir / name).write_bytes(b"PK-" + run_id.encode())
    (run_dir / "summary.json").write_text(json.dumps({"thesis": "t", "key_assumptions": [], "caveats": []}))
    return store.publish(s, symbol=symbol, run_id=run_id, run_dir=run_dir, verdict=VERDICT,
                         inputs={"symbol": symbol}, meta=meta, today=day)


def _indexes(s):
    read = lambda p: json.loads(p.read_text())["models"] if p.exists() else {}   # noqa: E731
    return read(s.latest_path), read(s.public_path)


def test_a_model_built_for_the_account_stays_out_of_the_public_index(s, tmp_path):
    _publish(s, tmp_path, "OWNA", "r1", date(2026, 11, 15),
             model=s.model_held, triggers=["T1: never modelled"], real_money=True)
    latest, public = _indexes(s)
    assert set(latest) == {"OWNA"} and public == {}
    _publish(s, tmp_path, "AAA", "r2", date(2026, 11, 17),
             model=s.model_other, triggers=["T1: never modelled"], real_money=False)
    latest, public = _indexes(s)
    assert set(latest) == {"OWNA", "AAA"} and set(public) == {"AAA"}
    assert public["AAA"] == latest["AAA"]


def test_the_public_keeps_its_model_when_the_account_builds_a_newer_one(s, tmp_path):
    """AAA had a public model, then the account bought it and rebuilt it. If the
    public entry were replaced or removed, the change would say AAA was bought."""
    _publish(s, tmp_path, "AAA", "r1", date(2026, 10, 15),
             model=s.model_other, triggers=["T1: never modelled"], real_money=False)
    _publish(s, tmp_path, "AAA", "r2", date(2026, 11, 26),
             model=s.model_held, triggers=["T5: model is 120d old"], real_money=True)
    latest, public = _indexes(s)
    assert latest["AAA"]["run_id"] == "r2" and latest["AAA"]["as_of"] == "2026-11-26"
    assert public["AAA"]["run_id"] == "r1" and public["AAA"]["as_of"] == "2026-10-15"
    assert (s.data_dir / public["AAA"]["file"]).read_bytes() == b"PK-r1"       # its workbook is still there
    # sold again, and due on the public schedule: the public model moves on
    _publish(s, tmp_path, "AAA", "r3", date(2027, 2, 1),
             model=s.model_other, triggers=["T1: 10-K filed 2027-01-28"], real_money=False)
    latest, public = _indexes(s)
    assert latest["AAA"]["run_id"] == public["AAA"]["run_id"] == "r3"


HELD, OTHER = "model-for-held-names", "model-for-everything-else"


@pytest.mark.parametrize("meta, public", [
    ({"real_money": False, "model": OTHER, "triggers": ["T1: 10-Q filed 2026-10-29"]}, True),
    ({"real_money": False, "model": OTHER, "triggers": ["T3: alpha take-profit pending"]}, True),   # the paper book is public
    ({"real_money": True, "model": HELD, "triggers": ["T1: never modelled"]}, False),
    ({"real_money": True, "model": OTHER, "triggers": ["T1: never modelled"]}, False),              # the flag decides, not the model
    ({"real_money": False, "model": OTHER, "triggers": ["T6: manual"]}, False),                     # built by hand, on the owner's day
    ({"real_money": False, "model": OTHER, "triggers": ["T3: Robinhood action CUT LOSS"]}, False),
    # written before the flag existed: the model name stands in for it
    ({"model": OTHER, "triggers": ["T1: never modelled"]}, True),
    ({"model": HELD, "triggers": ["T1: never modelled"]}, False),
    ({"triggers": ["T1: never modelled"]}, False),
    ({}, False),
])
def test_only_proof_makes_a_model_public(tmp_path, meta, public):
    s = make_settings(tmp_path, model_held=HELD, model_other=OTHER)
    assert store.on_public_schedule(s, meta) is public


def test_one_model_for_everyone_proves_nothing_about_an_unflagged_request(tmp_path):
    s = make_settings(tmp_path, model_held=OTHER, model_other=OTHER)
    assert store.on_public_schedule(s, {"model": OTHER, "triggers": ["T1: never modelled"]}) is False
    assert store.on_public_schedule(s, {"model": OTHER, "real_money": False, "triggers": []}) is True


def test_an_unreadable_public_index_stops_a_run_before_it_spends(s):
    s.public_path.write_text("[]")
    assert any("public.json" in p for p in store.integrity_problems(s))


def test_the_index_can_be_built_for_models_that_predate_it_and_never_loses_one(s, tmp_path):
    _publish(s, tmp_path, "AAA", "r1", date(2026, 10, 15), model=s.model_other, triggers=["T1: never modelled"])
    _publish(s, tmp_path, "BBB", "r2", date(2026, 10, 16), model=s.model_other, triggers=["T6: manual"])
    _publish(s, tmp_path, "OWNA", "r3", date(2026, 10, 17), model=s.model_held, triggers=["T1: never modelled"])
    s.public_path.unlink()                                   # as on a server that never had the index
    assert store.sync_public(s) == {"latest": 3, "public": 1, "added": 1}
    assert set(_indexes(s)[1]) == {"AAA"}
    assert store.sync_public(s) == {"latest": 3, "public": 1, "added": 0}     # safe to run again
    # AAA is rebuilt for the account: a later sync leaves the public model alone
    _publish(s, tmp_path, "AAA", "r4", date(2026, 11, 26), model=s.model_held, triggers=["T5: model is 40d old"],
             real_money=True)
    assert store.sync_public(s)["added"] == 0
    assert _indexes(s)[1]["AAA"]["run_id"] == "r1"
