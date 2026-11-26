"""Sandbox plumbing: clean environment, timeouts, safe reads, CLI result parsing.

These run unprivileged with DEEP_DCF_UNSANDBOXED=1 (children run as the current
user). The uid switch itself needs root and is exercised on the VPS.
"""
import json
import os
import stat
import time

import pytest

from deep_dcf import sandbox
from tests.deep_dcf_helpers import make_settings

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="unsandboxed test mode is refused as root")


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEP_DCF_UNSANDBOXED", "1")
    st = make_settings(tmp_path)
    for d in (st.sandbox_home, st.runs_dir, st.tools_dir, st.skill_dir, st.venv_bin):
        d.mkdir(parents=True, exist_ok=True)
    return st


def _fake_claude(s, body: str):
    path = s.claude_bin
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)


def test_child_environment_is_built_from_scratch(s, monkeypatch, tmp_path):
    monkeypatch.setenv("ALPACA_SECRET_KEY", "broker-secret")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-secret")
    env = sandbox.base_env(s, tmp_path)
    assert "broker-secret" not in json.dumps(env) and "discord-secret" not in json.dumps(env)
    assert "ANTHROPIC_API_KEY" not in env                       # added only for the model run
    assert env["HOME"] == str(s.sandbox_home) and env["PATH"].startswith(str(s.venv_bin))
    assert json.loads(env["DCF_ACCEPT_JSON"])["tv_fraction_max"] == 0.9
    # setting this switched on the CLI's own sandbox and broke every Bash command (VPS, 2026-11-15)
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in env


def test_without_root_or_the_test_flag_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEP_DCF_UNSANDBOXED", raising=False)
    with pytest.raises(sandbox.SandboxError):
        sandbox.run_cmd(make_settings(tmp_path), ["true"], cwd=tmp_path, env={}, timeout=5)


def test_timeout_kills_the_whole_process_group(s, tmp_path):
    marker = tmp_path / "still_alive"
    script = f"(sleep 3; touch {marker}) & sleep 30"
    t0 = time.monotonic()
    pr = sandbox.run_cmd(s, ["/bin/sh", "-c", script], cwd=tmp_path,
                         env={"PATH": "/usr/bin:/bin"}, timeout=1)
    assert pr.timed_out and time.monotonic() - t0 < 15
    time.sleep(3.5)
    assert not marker.exists()                                  # the background child died too


def test_run_dir_names_are_validated(s):
    with pytest.raises(sandbox.SandboxError):
        sandbox.make_run_dir(s, "../etc", "20261115-000000-X")
    with pytest.raises(sandbox.SandboxError):
        sandbox.make_run_dir(s, "ACME", "../../x")
    d = sandbox.make_run_dir(s, "BRK.B", "20261115-000000-BRK_B")
    assert d.is_dir() and (d / ".tmp").is_dir() and d.parent.name == "BRK.B"
    with pytest.raises(FileExistsError):
        sandbox.make_run_dir(s, "BRK.B", "20261115-000000-BRK_B")


def test_read_file_refuses_symlinks_oversize_and_non_files(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    assert sandbox.read_file(real, 10) == b"{}"
    link = tmp_path / "summary.json"
    link.symlink_to(real)
    with pytest.raises(OSError):
        sandbox.read_file(link, 10)                             # O_NOFOLLOW
    with pytest.raises(sandbox.SandboxError):
        sandbox.read_file(real, 1)
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(sandbox.SandboxError):
        sandbox.read_file(tmp_path / "pipe", 10)                # must not block on a FIFO
    with pytest.raises(sandbox.SandboxError):
        sandbox.read_file(tmp_path, 10)                         # a directory is not a file


def test_write_input_never_overwrites_or_follows_a_link(tmp_path):
    sandbox.write_input(tmp_path, "inputs.json", "{}")
    assert stat.S_IMODE((tmp_path / "inputs.json").stat().st_mode) & 0o600 == 0o600
    with pytest.raises(FileExistsError):
        sandbox.write_input(tmp_path, "inputs.json", "{}")
    (tmp_path / "evil").symlink_to(tmp_path / "target")
    with pytest.raises(OSError):
        sandbox.write_input(tmp_path, "evil", "x")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("stdout, cost", [
    ('{"type":"result","subtype":"success","is_error":false,"total_cost_usd":3.21,"num_turns":40}', 3.21),
    ('warming up\n{"type":"system"}\n{"type":"result","total_cost_usd":0.5}\n', 0.5),
    ('[{"type":"system"},{"type":"result","total_cost_usd":1.25}]', 1.25),
    ('{\n "type": "result",\n "total_cost_usd": 2.0\n}', 2.0),
])
def test_cli_result_is_found_in_every_output_shape(stdout, cost):
    assert sandbox.parse_claude_result(stdout)["total_cost_usd"] == cost


@pytest.mark.parametrize("stdout", ["", "Segmentation fault", '{"type":"assistant"}', "null", "[]"])
def test_unparseable_cli_output_is_none(stdout):
    assert sandbox.parse_claude_result(stdout) is None


def test_model_run_passes_prompt_on_stdin_and_key_only_in_env(s, tmp_path):
    run_dir = sandbox.make_run_dir(s, "ACME", "20261115-010101-ACME")
    _fake_claude(s, f'''
printf '%s\\n' "$@" > "{tmp_path}/argv.txt"
cat > "{tmp_path}/stdin.txt"
printf '%s' "$ANTHROPIC_API_KEY" > "{tmp_path}/key.txt"
pwd > "{tmp_path}/cwd.txt"
echo '{{"type":"result","subtype":"success","is_error":false,"total_cost_usd":2.75,"num_turns":31,"result":"base 36"}}'
''')
    out = sandbox.run_claude(s, run_dir=run_dir, model="claude-sonnet-5-5", budget_usd=5.0,
                             prompt="PROMPT-BODY " + "x" * 200_000, system_prompt="SYSTEM-RULES",
                             api_key="sk-test-key")
    assert (out["cost_usd"], out["cost_source"], out["is_error"]) == (2.75, "reported", False)
    argv = (tmp_path / "argv.txt").read_text()
    assert "sk-test-key" not in argv                             # never visible in `ps`
    assert "PROMPT-BODY" not in argv and "SYSTEM-RULES" in argv
    # the CLI gets 75% of the reserved $5: it can finish one turn past its budget
    assert "--bare" in argv and "--max-budget-usd\n3.75" in argv and "--no-session-persistence" in argv
    assert "WebFetch,WebSearch" in argv.split("--disallowedTools\n")[1]
    assert "Web" not in argv.split("--tools\n")[1].split("\n")[0]
    assert (tmp_path / "stdin.txt").read_text().startswith("PROMPT-BODY")
    assert (tmp_path / "key.txt").read_text() == "sk-test-key"
    assert os.path.realpath((tmp_path / "cwd.txt").read_text().strip()) == os.path.realpath(run_dir)


def test_cli_that_dies_at_once_without_a_result_cost_nothing_and_is_an_infra_failure(s):
    run_dir = sandbox.make_run_dir(s, "ACME", "20261115-020202-ACME")
    _fake_claude(s, "cat > /dev/null\necho 'node: not found' >&2\nexit 127\n")
    out = sandbox.run_claude(s, run_dir=run_dir, model="claude-sonnet-5-5", budget_usd=8.0,
                             prompt="p", system_prompt="s", api_key="k")
    assert (out["cost_usd"], out["cost_source"]) == (0.0, "cli_failed_fast")
    assert out["infra_failure"] and out["is_error"] and "not found" in out["stderr_tail"]


def test_run_that_times_out_without_a_cost_report_is_charged_at_the_full_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEP_DCF_UNSANDBOXED", "1")
    st = make_settings(tmp_path, run_timeout_s=1)
    for d in (st.sandbox_home, st.runs_dir):
        d.mkdir(parents=True, exist_ok=True)
    run_dir = sandbox.make_run_dir(st, "ACME", "20261115-020203-ACME")
    _fake_claude(st, "cat > /dev/null\nsleep 30\n")
    out = sandbox.run_claude(st, run_dir=run_dir, model="claude-sonnet-5-5", budget_usd=8.0,
                             prompt="p", system_prompt="s", api_key="k")
    assert (out["cost_usd"], out["cost_source"], out["timed_out"]) == (8.0, "assumed_cap", True)
    assert not out["infra_failure"]                              # the model's run, not the plumbing


def test_api_refusal_at_zero_cost_is_an_infra_failure_but_a_paid_error_is_not(s):
    run_dir = sandbox.make_run_dir(s, "ACME", "20261115-020204-ACME")
    _fake_claude(s, "cat > /dev/null\necho '{\"type\":\"result\",\"is_error\":true,\"total_cost_usd\":0,\"result\":\"API Error: 400\"}'\nexit 1\n")
    out = sandbox.run_claude(s, run_dir=run_dir, model="m", budget_usd=4.0, prompt="p", system_prompt="s", api_key="k")
    assert out["infra_failure"] and out["cost_usd"] == 0.0 and out["cost_source"] == "reported"
    _fake_claude(s, "cat > /dev/null\necho '{\"type\":\"result\",\"is_error\":true,\"subtype\":\"error_max_budget_usd\",\"total_cost_usd\":3.1}'\n")
    out = sandbox.run_claude(s, run_dir=run_dir, model="m", budget_usd=4.0, prompt="p", system_prompt="s", api_key="k")
    assert not out["infra_failure"] and out["cost_usd"] == 3.1


def test_reap_never_touches_the_runners_own_processes(s):
    import subprocess
    child = subprocess.Popen(["sleep", "30"])
    try:
        sandbox.reap(s)                                          # unsandboxed: must be a no-op
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_negative_or_non_numeric_cost_is_not_trusted(s):
    run_dir = sandbox.make_run_dir(s, "ACME", "20261115-030303-ACME")
    _fake_claude(s, "cat > /dev/null\necho '{\"type\":\"result\",\"total_cost_usd\":-4}'\n")
    out = sandbox.run_claude(s, run_dir=run_dir, model="m", budget_usd=5.0, prompt="p",
                             system_prompt="s", api_key="k")
    assert (out["cost_usd"], out["cost_source"]) == (5.0, "assumed_cap")


def test_missing_key_or_missing_binary_never_starts(s):
    run_dir = sandbox.make_run_dir(s, "ACME", "20261115-040404-ACME")
    with pytest.raises(sandbox.SandboxError):
        sandbox.run_claude(s, run_dir=run_dir, model="m", budget_usd=5, prompt="p",
                           system_prompt="s", api_key="")
    with pytest.raises(OSError):                                 # no fake_claude written
        sandbox.run_claude(s, run_dir=run_dir, model="m", budget_usd=5, prompt="p",
                           system_prompt="s", api_key="k")


def test_check_wrapper_turns_garbage_and_timeouts_into_an_error_verdict(s):
    run_dir = sandbox.make_run_dir(s, "ACME", "20261115-050505-ACME")
    py = s.venv_bin / "python3"
    py.write_text("#!/bin/sh\necho 'Traceback: boom'\nexit 1\n"); py.chmod(0o755)
    out = sandbox.run_check(s, run_dir=run_dir, trusted_inputs=run_dir / "x.json")
    assert out["verdict"] == "ERROR" and "no verdict" in out["errors"][0]
    py.write_text('#!/bin/sh\necho \'{"verdict":"PASS","values":{"price":30}}\'\n')
    out = sandbox.run_check(s, run_dir=run_dir, trusted_inputs=run_dir / "x.json")
    assert out == {"verdict": "PASS", "values": {"price": 30}, "errors": [], "warnings": []}
    py.write_text("#!/bin/sh\nsleep 30\n")
    assert sandbox.run_check(s, run_dir=run_dir, trusted_inputs=run_dir / "x.json",
                             timeout=1)["verdict"] == "ERROR"


def test_prune_keeps_the_newest_runs_and_their_input_copies(s):
    for i in range(5):
        d = sandbox.make_run_dir(s, "ACME", f"2026111{i}-000000-ACME")
        sandbox.write_input(d.parent, f"{d.name}.inputs.json", "{}")
    sandbox.prune_runs(s, "ACME", keep=2)
    left = sorted(p.name for p in (s.runs_dir / "ACME").iterdir())
    assert left == ["20261113-000000-ACME", "20261113-000000-ACME.inputs.json",
                    "20261114-000000-ACME", "20261114-000000-ACME.inputs.json"]


def test_preflight_names_everything_that_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEP_DCF_UNSANDBOXED", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEP_DCF_ANTHROPIC_API_KEY", raising=False)
    problems = " | ".join(sandbox.preflight(make_settings(tmp_path)))
    for needle in ("Claude CLI not found", "sandbox venv missing", "recalc.py", "check_delivery.py",
                   "dcf-model skill missing", "no Anthropic API key"):
        assert needle in problems, needle


def test_dedicated_key_wins_and_placeholders_are_ignored(tmp_path, monkeypatch):
    st = make_settings(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-shared")
    monkeypatch.delenv("DEEP_DCF_ANTHROPIC_API_KEY", raising=False)
    assert st.api_key() == ("sk-shared", "ANTHROPIC_API_KEY")
    monkeypatch.setenv("DEEP_DCF_ANTHROPIC_API_KEY", "sk-dedicated")
    assert st.api_key() == ("sk-dedicated", "DEEP_DCF_ANTHROPIC_API_KEY")
    monkeypatch.setenv("DEEP_DCF_ANTHROPIC_API_KEY", "your_key_here")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-dummy")
    assert st.api_key() == ("", "")
