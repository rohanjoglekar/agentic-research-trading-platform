"""Batch engine requests: schema, prompt, cost accounting, answer parsing."""
import json
from types import SimpleNamespace

import pytest

from deep_dcf import batch
from tests.deep_dcf_helpers import make_settings
from tests.test_deep_dcf_build_model import INPUTS, answer

UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
               "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "pattern"}


def _walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def test_schema_is_strict_and_uses_only_supported_keywords():
    for path, node in _walk(batch.SCHEMA):
        if isinstance(node, dict) and node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert sorted(node.get("required", [])) == sorted(node.get("properties", {})), path
        if isinstance(node, dict):
            assert not (UNSUPPORTED & set(node)), (path, UNSUPPORTED & set(node))


def test_schema_matches_what_the_builder_validates():
    scen = batch.SCHEMA["properties"]["scenarios"]["properties"]["base"]["properties"]
    assert set(scen) == set(batch.build_model.VECTORS) | set(batch.build_model.SCALARS)


def test_request_is_batchable_cached_and_structured(tmp_path):
    s = make_settings(tmp_path, engine="batch")
    p = batch.request_params("claude-sonnet-5-5", INPUTS, s)
    assert p["model"] == "claude-sonnet-5-5" and p["max_tokens"] == s.batch_max_tokens
    assert p["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert p["thinking"] == {"type": "adaptive"}
    assert p["output_config"]["effort"] == "medium"
    assert p["output_config"]["format"] == {"type": "json_schema", "schema": batch.SCHEMA}
    # things a batch request must not carry (the API rejects them)
    for banned in ("stream", "fallbacks", "speed", "temperature", "top_p", "tool_choice"):
        assert banned not in p
    # the system prompt is identical for every company → one cache entry
    q = batch.request_params("claude-sonnet-5-5", {**INPUTS, "symbol": "OTHER"}, s)
    assert q["system"] == p["system"]


def test_system_prompt_is_long_enough_to_cache():
    assert len(batch.SYSTEM_PROMPT) / 4 > 512        # the cacheable minimum on these models


def test_user_message_is_in_millions_and_never_leaks_targets():
    inputs = json.loads(json.dumps(INPUTS))
    inputs["consensus"] = {"forward_eps": 3.1}
    inputs["dcf_fair_value"] = 99.99                   # must not reach the model: not a whitelisted key
    msg = batch.user_message(inputs)
    assert '"revenue_musd":2000.0' in msg and '"shares_outstanding_millions":100.0' in msg
    assert '"net_debt_musd":600.0' in msg
    assert "99.99" not in msg and "target" not in msg.lower()
    assert "2000000000" not in msg                     # no raw dollars


def test_history_ratios_are_precomputed():
    rows = batch.history_ratios(INPUTS)
    assert rows[1]["revenue_growth"] == round(1.95 / 1.8 - 1, 4)
    assert rows[1]["operating_margin"] == round(0.56 / 1.95, 4)
    assert "capex_pct" not in rows[1]                  # a missing value is omitted, not invented


RATES = {"claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10,
                               "cache_write_5m": 2.5, "cache_write_1h": 4.0}}


def test_usage_cost_is_half_price_with_cache_tiers(tmp_path):
    s = make_settings(tmp_path, engine="batch", token_rates=RATES)
    usage = SimpleNamespace(input_tokens=3000, output_tokens=5000, cache_read_input_tokens=2000,
                            cache_creation_input_tokens=1000,
                            cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=1000,
                                                           ephemeral_5m_input_tokens=0))
    want = (3000 * 2 + 5000 * 10 + 2000 * 0.10 + 1000 * 4.0) / 1e6 * 0.5
    assert batch.usage_cost_usd(usage, "claude-sonnet-5-5", s) == pytest.approx(want)
    # unknown split → priced as the dearer 1-hour write
    no_split = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000}
    assert batch.usage_cost_usd(no_split, "claude-sonnet-5-5", s) == pytest.approx(1000 * 4.0 / 1e6 * 0.5)


def test_unknown_model_is_priced_at_the_dearest_rates(tmp_path):
    s = make_settings(tmp_path, engine="batch")
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert batch.usage_cost_usd(usage, "claude-new-model", s) == pytest.approx(4.0 * 0.5)


def test_reservation_bounds_the_worst_possible_bill(tmp_path):
    """Whatever the model does, it cannot cost more than was reserved: output
    is capped at max_tokens, and input is priced as if every token were a
    1-hour cache write at a pessimistic characters-per-token ratio."""
    s = make_settings(tmp_path, engine="batch")
    for model in ("claude-sonnet-5-5", "claude-opus-5-5"):
        p = batch.request_params(model, INPUTS, s)
        reserved = batch.reservation_usd(p, s)
        chars = len(json.dumps(p["system"])) + len(json.dumps(p["messages"]))
        worst_tokens = int(chars / 2.0)                # real JSON is ≥2 chars/token
        usage = {"input_tokens": 0, "output_tokens": p["max_tokens"], "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": worst_tokens}
        assert batch.usage_cost_usd(usage, model, s) <= reserved
        assert reserved < 1.0                          # and it is small: this is the point


def _msg(text=None, stop="end_turn", category=None):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(stop_reason=stop, content=content,
                           stop_details=SimpleNamespace(category=category) if category else None)


def test_answers_are_parsed_or_classified():
    data, err, st = batch.parse_answer(_msg(json.dumps(answer())))
    assert st == "ok" and data["pretax_cost_of_debt"] == 0.05 and err is None
    assert batch.parse_answer(_msg(stop="refusal", category="bio"))[2] == "refused"
    assert batch.parse_answer(_msg('{"scen', stop="max_tokens"))[2] == "truncated"
    assert batch.parse_answer(_msg("not json"))[2] == "unparseable"
    assert batch.parse_answer(_msg("[1,2]"))[2] == "unparseable"
    assert batch.parse_answer(_msg(None))[2] == "unparseable"
