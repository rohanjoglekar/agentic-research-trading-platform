"""The scan writes the file the options trader acts on, so what it may write is pinned here.

Two rules. A live scan that cannot reach its data source saves nothing; it does
not fall back to mock data. A mock run saves only `mock_` files, reads no
market data and never asks a model about random numbers.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs import daily_scan  # noqa: E402


def _never(*_a, **_k):
    raise AssertionError("must not be called")


@pytest.fixture
def offline(monkeypatch):
    """No network: a fixed regime, no POP bias, no research overlay, no model calls."""
    import data.market_regime as regime
    import data.pop_calibration as pop
    import data.research_overlay as overlay
    from config import settings

    monkeypatch.setattr(regime, "classify_regime", lambda *a, **k: {"regime": "calm"})
    monkeypatch.setattr(pop, "get_current_bias", lambda *a, **k: 1.0)
    monkeypatch.setattr(overlay, "load_research", lambda *a, **k: {})
    monkeypatch.setitem(settings.LLM, "use_llm_explanations", False)


def test_a_live_scan_without_its_data_source_saves_nothing(tmp_path, monkeypatch, offline):
    import data.alpaca_options_client as client

    def no_keys(*_a, **_k):
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")

    monkeypatch.setattr(client, "AlpacaOptionsClient", no_keys)
    monkeypatch.setattr(daily_scan, "generate_mock_universe", _never)     # no mock stand-in
    monkeypatch.setattr(daily_scan, "analyze_top_stocks", _never)
    df, grouped = daily_scan.run_full_scan(universe=["AAA", "BBB"], use_mock=False,
                                           save_csv=True, output_dir=str(tmp_path))
    assert df.empty and grouped == []
    assert list(tmp_path.iterdir()) == []


def test_a_mock_run_is_offline_and_asks_no_model(tmp_path, monkeypatch):
    import data.market_regime as regime
    import data.pop_calibration as pop
    from agents.recommendation_agent import RecommendationAgent
    from config import settings

    monkeypatch.setattr(regime, "classify_regime", _never)                # no VIX lookup
    monkeypatch.setattr(pop, "get_current_bias", _never)
    monkeypatch.setattr(daily_scan, "analyze_top_stocks", _never)         # no research on random prices
    monkeypatch.setattr(RecommendationAgent, "_llm_explanation", _never)  # no explanation either
    monkeypatch.setitem(settings.LLM, "use_llm_explanations", True)       # even when switched on
    monkeypatch.setitem(settings.RESEARCH_OVERLAY, "enabled", False)
    df, grouped = daily_scan.run_full_scan(use_mock=True, save_csv=False, output_dir=str(tmp_path))
    assert not df.empty and grouped
    assert list(tmp_path.iterdir()) == []


def test_a_mock_run_keeps_to_its_own_files(tmp_path, monkeypatch, offline):
    monkeypatch.setattr(daily_scan, "analyze_top_stocks", _never)         # no research on random prices
    df, grouped = daily_scan.run_full_scan(use_mock=True, save_csv=True, output_dir=str(tmp_path))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names, "the mock universe should produce at least one trade"
    assert all(n.startswith("mock_") for n in names), names
    # the two files the trader and the dashboard read are never written by a mock run
    assert not (tmp_path / "scan_results_grouped_latest.json").exists()
    assert not (tmp_path / "scan_results_latest.csv").exists()
    assert not df.empty and grouped
