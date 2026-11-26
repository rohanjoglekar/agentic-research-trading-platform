"""Deep DCF read path: what the dashboard serves, and the separate index the public is served from."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import appconfig as config  # noqa: E402
from adapters import deep_dcf  # noqa: E402

PRIVATE = ("file", "cost_usd", "run_id", "model", "triggers", "generated_at")


def _entry(sym, as_of, run="20261115-010101", **over):
    e = {"symbol": sym, "run_id": f"{run}-{sym}", "as_of": as_of, "price": 30.0,
         "generated_at": f"{as_of}T06:00:00+00:00",
         "scenarios": {"bear": {"fair_value": 24.0, "wacc": 0.1, "terminal_growth": 0.02},
                       "base": {"fair_value": 36.0, "wacc": 0.092, "terminal_growth": 0.025},
                       "bull": {"fair_value": 48.0, "wacc": 0.088, "terminal_growth": 0.03}},
         "upside_pct": {"bear": -20.0, "base": 20.0, "bull": 60.0}, "tv_fraction": 0.74,
         "thesis": "t", "key_assumptions": ["a", "b", "c"], "caveats": [], "warnings": [],
         "file": f"models/{sym}/{run}-{sym}/{sym}_DCF_{as_of}.xlsx",
         "model": "model-a", "triggers": ["T1: never modelled"], "cost_usd": 3.4}
    e.update(over)
    return e


def _write(root, name, entries):
    (root / name).write_text(json.dumps({"models": entries}))


@pytest.fixture
def store(tmp_path, monkeypatch):
    """AAA and BBB were built on the public schedule. OWNA was built for the
    account, so the job left it out of public.json."""
    monkeypatch.setattr(config, "DEEP_DCF_DIR", tmp_path)
    today = date.today()
    fresh, old = today.isoformat(), (today - timedelta(days=200)).isoformat()
    entries = {"AAA": _entry("AAA", fresh), "BBB": _entry("BBB", old),
               "OWNA": _entry("OWNA", fresh, model="model-b", triggers=["T3: account action"])}
    _write(tmp_path, "latest.json", entries)
    _write(tmp_path, "public.json", {k: entries[k] for k in ("AAA", "BBB")})
    (tmp_path / "status.json").write_text(json.dumps(
        {"cycle_start": "2026-11-15", "cycle_end": "2026-12-15", "spent_usd": 6.8, "ceiling_usd": 70.0,
         "runs": 3, "accepted": 3, "updated": "2026-11-15T06:00:00+00:00"}))
    for sym in ("AAA", "OWNA"):
        f = tmp_path / entries[sym]["file"]
        f.parent.mkdir(parents=True)
        f.write_bytes(b"PK-xlsx")
    return tmp_path


def test_models_are_listed_newest_first_with_age_and_staleness(store):
    out = deep_dcf.get_models()
    assert out["available"] and out["count"] == 3
    assert [m["symbol"] for m in out["models"]] == ["OWNA", "AAA", "BBB"]
    aaa, bbb = out["models"][1:]
    assert aaa["age_days"] == 0 and not aaa["stale"] and aaa["has_file"]
    assert bbb["age_days"] == 200 and bbb["stale"]
    assert out["status"]["spent_usd"] == 6.8 and out["status"]["ceiling_usd"] == 70.0


def test_the_public_is_served_its_own_index(store):
    out = deep_dcf.get_models(view_only=True)
    assert [m["symbol"] for m in out["models"]] == ["AAA", "BBB"] and out["count"] == 2
    aaa = out["models"][0]
    assert aaa["scenarios"]["base"]["fair_value"] == 36.0 and aaa["upside_pct"]["base"] == 20.0
    for private in PRIVATE:
        assert private not in aaa, private
    assert aaa["has_file"] is False
    # run counts cover models the public is not shown, so it gets no status at all
    assert out["status"] is None
    assert "file" not in deep_dcf.for_symbol("aaa", view_only=True)
    assert deep_dcf.for_symbol("AAA")["file"].endswith(".xlsx")


def test_a_model_built_for_the_account_is_not_public(store):
    assert deep_dcf.for_symbol("OWNA")["triggers"] == ["T3: account action"]
    assert deep_dcf.for_symbol("OWNA", view_only=True) is None
    assert deep_dcf.model_path("OWNA") is not None
    assert deep_dcf.model_path("OWNA", view_only=True) is None
    assert "OWNA" not in json.dumps(deep_dcf.get_models(view_only=True))


def test_the_public_keeps_its_model_when_the_account_builds_a_newer_one(store):
    """AAA is bought and rebuilt for the account. The public index still holds
    the earlier model, so nothing about AAA changes on the public site."""
    today = date.today().isoformat()
    earlier = (date.today() - timedelta(days=30)).isoformat()
    latest = json.loads((store / "latest.json").read_text())["models"]
    public = json.loads((store / "public.json").read_text())["models"]
    public["AAA"] = _entry("AAA", earlier, run="20261015-010101")
    latest["AAA"] = _entry("AAA", today, run="20261126-010101", model="model-b")
    _write(store, "latest.json", latest)
    _write(store, "public.json", public)
    f = store / public["AAA"]["file"]
    f.parent.mkdir(parents=True)
    f.write_bytes(b"PK-public")
    assert deep_dcf.for_symbol("AAA")["as_of"] == today
    assert deep_dcf.for_symbol("AAA", view_only=True)["as_of"] == earlier
    assert deep_dcf.model_path("AAA", view_only=True).read_bytes() == b"PK-public"


def test_without_a_public_index_the_public_sees_no_models(store):
    (store / "public.json").unlink()
    assert deep_dcf.get_models(view_only=True) == {"available": False, "count": 0, "models": [], "status": None}
    assert deep_dcf.for_symbol("AAA", view_only=True) is None
    assert deep_dcf.get_models()["count"] == 3                        # the owner is unaffected


def test_no_models_yet_is_an_empty_available_false_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEEP_DCF_DIR", tmp_path)
    assert deep_dcf.get_models() == {"available": False, "count": 0, "models": [], "status": None}
    assert deep_dcf.for_symbol("AAA") is None and deep_dcf.model_path("AAA") is None
    (tmp_path / "latest.json").write_text("not json")
    assert deep_dcf.get_models()["available"] is False


def test_download_path_is_confined_to_the_models_directory(store):
    assert deep_dcf.model_path("AAA").read_bytes() == b"PK-xlsx"
    assert deep_dcf.model_path("aaa") is not None
    assert deep_dcf.model_path("BBB") is None                         # indexed but file missing
    assert deep_dcf.model_path("NOPE") is None
    assert deep_dcf.model_path("../etc") is None
    secret = store.parent / "secret.xlsx"
    secret.write_bytes(b"secret")
    latest = json.loads((store / "latest.json").read_text())
    for bad in ("../secret.xlsx", str(secret), "models/../../secret.xlsx", "models/AAA/notes.txt", None, 7):
        latest["models"]["AAA"]["file"] = bad
        (store / "latest.json").write_text(json.dumps(latest))
        assert deep_dcf.model_path("AAA") is None, bad
    link = store / "models" / "AAA" / "link.xlsx"
    link.symlink_to(secret)
    latest["models"]["AAA"]["file"] = "models/AAA/link.xlsx"
    (store / "latest.json").write_text(json.dumps(latest))
    assert deep_dcf.model_path("AAA") is None                         # symlink resolves outside


def test_endpoints_serve_the_owner_and_withhold_the_workbook_from_the_public(store, api_server):
    from fastapi.testclient import TestClient

    public = {"X-Forge-View-Only": "1"}
    c = TestClient(api_server.app)
    r = c.get("/api/robinhood/deep-dcf")
    assert r.status_code == 200 and r.json()["models"][0]["cost_usd"] == 3.4 and r.json()["count"] == 3
    pub = c.get("/api/robinhood/deep-dcf", headers=public).json()
    assert "cost_usd" not in pub["models"][0] and pub["count"] == 2 and pub["status"] is None
    ok = c.get("/api/robinhood/deep-dcf/aaa/model.xlsx")
    assert ok.status_code == 200 and ok.content == b"PK-xlsx"
    assert "AAA_DCF_" in ok.headers["content-disposition"]
    assert c.get("/api/robinhood/deep-dcf/AAA/model.xlsx", headers=public).status_code == 404
    assert c.get("/api/robinhood/deep-dcf/BBB/model.xlsx").status_code == 404
    assert c.get("/api/robinhood/deep-dcf/ZZZZ/model.xlsx").status_code == 404
    # a model built for the account answers the public exactly like no model at all
    assert c.get("/api/robinhood/deep-dcf/OWNA/sheet", headers=public).status_code == 404
    assert c.get("/api/robinhood/deep-dcf/ZZZZ/sheet", headers=public).status_code == 404
