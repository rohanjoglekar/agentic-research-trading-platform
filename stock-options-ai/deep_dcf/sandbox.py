"""Run the model, LibreOffice and the checker as an unprivileged user.

The runner itself is root (it reads the trading .env files and writes the
dashboard data dir). The headless Claude session gets a Bash tool, so it must
NOT be root on a box that holds broker credentials. Every child process here:

  • runs as the dedicated system user (default `forge-dcf`), which cannot read
    /root at all (mode 700), with no supplementary groups and no-new-privileges;
  • gets a clean, explicit environment — nothing inherited from the runner, so
    the only secret it ever sees is the one Anthropic API key it needs;
  • works in its own run directory; tools, the Python venv and the skill live
    under a root-owned prefix it can read but not change;
  • has no web tools (the CLI is started without WebFetch/WebSearch), so there
    is no untrusted content for a prompt injection to arrive in.

Files coming back OUT of a run directory are opened with O_NOFOLLOW and must be
regular files under a size cap, so a symlink planted in the run dir cannot make
root publish something else.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from deep_dcf.config import Settings

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
CLAUDE_TOOLS = "Bash,Read,Write,Edit,Glob,Grep"
KEEP_RUNS_PER_SYMBOL = 3
FAST_FAIL_S = 30.0          # a CLI that dies this quickly without a result did no work


class SandboxError(RuntimeError):
    pass


@dataclass
class ProcResult:
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


def unsandboxed_ok() -> bool:
    """Local development only: run children as the current user. Refused when
    the runner is root, so it can never weaken the production path."""
    return os.environ.get("DEEP_DCF_UNSANDBOXED") == "1" and os.geteuid() != 0


def _ids(s: Settings) -> Optional[tuple[int, int]]:
    if unsandboxed_ok():
        return None
    if os.geteuid() != 0:
        raise SandboxError("the deep-DCF runner must run as root to drop to the sandbox user")
    try:
        pw = pwd.getpwnam(s.sandbox_user)
    except KeyError as e:
        raise SandboxError(f"sandbox user {s.sandbox_user!r} does not exist — run "
                           "infrastructure/setup_deep_dcf.sh") from e
    if pw.pw_uid == 0:
        raise SandboxError("sandbox user must not be root")
    return pw.pw_uid, pw.pw_gid


def base_env(s: Settings, run_dir: Path) -> dict:
    """The child environment. Built from scratch — never os.environ."""
    return {
        "HOME": str(s.sandbox_home),
        "USER": s.sandbox_user, "LOGNAME": s.sandbox_user,
        "PATH": f"{s.venv_bin}:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "TMPDIR": str(run_dir / ".tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DCF_TOOLS": str(s.tools_dir),
        "DCF_SKILL": str(s.skill_dir),
        "DCF_ACCEPT_JSON": json.dumps(s.accept, sort_keys=True),
        # NOT set: CLAUDE_CODE_SUBPROCESS_ENV_SCRUB. It does more than scrub the
        # key from the commands the CLI runs — it turns on the CLI's own
        # bubblewrap sandbox, which needs socat and unprivileged user namespaces
        # (restricted on Ubuntu 24.04). With it set, every Bash command failed
        # ("sandbox failed to initialize"), i.e. no model could be built. The
        # isolation here is the unprivileged user, not the CLI's sandbox.
        # keep the CLI quiet and unchanging between runs
        "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def _sandbox_pids(uid: int) -> list[int]:
    """Every live process owned by `uid` (Linux /proc)."""
    pids = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return pids
    for name in names:
        if name.isdigit():
            try:
                if os.stat(f"/proc/{name}").st_uid == uid:
                    pids.append(int(name))
            except OSError:
                continue
    return pids


def reap(s: Settings) -> None:
    """Kill every process still owned by the sandbox user.

    Killing the child's process group is not enough: LibreOffice is started in
    its own session, and the model may background a command, so a process can
    outlive the run. A survivor could keep spending, or rewrite the delivered
    files between the acceptance check and the publish. The sandbox user exists
    only for this job and the runner holds a global lock, so after each child
    returns nothing of that uid has any business running."""
    try:
        ids = _ids(s)
    except SandboxError:
        return
    if ids is None or ids[0] == 0 or ids[0] == os.getuid():
        return                                   # never the runner's own uid
    for _ in range(40):
        pids = _sandbox_pids(ids[0])
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.1)
    logger.error(f"[deep-dcf] sandbox processes survived SIGKILL: {_sandbox_pids(ids[0])}")


def _kill_group(proc: subprocess.Popen, grace: float) -> None:
    """End the child's process group: optional SIGTERM grace, then SIGKILL."""
    if grace > 0:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=grace)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def run_cmd(s: Settings, cmd: list[str], *, cwd: Path, env: dict, timeout: float,
            stdin_text: Optional[str] = None) -> ProcResult:
    """Run `cmd` as the sandbox user, in its own process group, with a hard
    timeout. However it ends — normally, on timeout, or because the runner is
    interrupted — the group is killed and every sandbox-user process reaped."""
    ids = _ids(s)
    kwargs: dict = {}
    if ids is not None:
        kwargs.update(user=ids[0], group=ids[1], extra_groups=[], umask=0o077)
        setpriv = shutil.which("setpriv")
        if setpriv:
            cmd = [setpriv, "--no-new-privs", "--"] + list(cmd)
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env, start_new_session=True,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
    timed_out, out, err = False, "", ""
    try:
        try:
            out, err = proc.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        _kill_group(proc, grace=10 if timed_out else 0)
        reap(s)
    if timed_out:
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out, err = "", ""
    return ProcResult(returncode=proc.returncode, stdout=out or "", stderr=err or "",
                      timed_out=timed_out, duration_s=time.monotonic() - t0)


def make_run_dir(s: Settings, symbol: str, run_id: str) -> Path:
    """<runs>/<SYM>/ is root-owned; <runs>/<SYM>/<run_id>/ belongs to the sandbox
    user. The parent being root-owned means the run dir cannot be swapped out."""
    if not SYMBOL_RE.match(symbol):
        raise SandboxError(f"refusing unusual symbol {symbol!r}")
    if not re.match(r"^[A-Za-z0-9_\-]{6,64}$", run_id):
        raise SandboxError(f"bad run id {run_id!r}")
    ids = _ids(s)
    sym_dir = s.runs_dir / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    run_dir = sym_dir / run_id
    run_dir.mkdir(mode=0o700)
    (run_dir / ".tmp").mkdir(mode=0o700)
    if ids is not None:
        os.chmod(sym_dir, 0o755)
        for p in (run_dir, run_dir / ".tmp"):
            os.chown(p, ids[0], ids[1])
    return run_dir


def write_input(run_dir: Path, name: str, text: str) -> Path:
    """A root-written, world-readable file in the run dir (the model reads it)."""
    path = run_dir / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def read_file(path: Path, max_bytes: int) -> bytes:
    """Read a regular file without following symlinks, under a size cap."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SandboxError(f"{path.name} is not a regular file")
        if st.st_size > max_bytes:
            raise SandboxError(f"{path.name} is {st.st_size} bytes (limit {max_bytes})")
        chunks, remaining = [], st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def parse_claude_result(stdout: str) -> Optional[dict]:
    """The `--output-format json` result object (last JSON object line with
    type == "result"), or None when the CLI produced nothing parseable."""
    for line in reversed([ln for ln in (stdout or "").splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
        if isinstance(obj, list):               # some CLI versions emit one JSON array
            for item in reversed(obj):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    except ValueError:
        pass
    return None


def run_claude(s: Settings, *, run_dir: Path, model: str, budget_usd: float, prompt: str,
               system_prompt: str, api_key: str) -> dict:
    """One headless model run. → {result, cost_usd, cost_source, timed_out, …}.

    `--bare` makes the CLI authenticate with ANTHROPIC_API_KEY only (never a
    subscription login), so a run is billed to that key and nothing else. The prompt
    goes in on stdin: it carries the whole skill text, and a single argv string
    is capped at 128 KB by the kernel.

    `budget_usd` is what the ledger RESERVED. The CLI is given a fraction of it
    (it checks its budget between turns and can finish one turn over), so the
    real cost stays inside the reservation.

    `infra_failure` means the CLI or the API failed rather than the model: it
    refused before doing any work, or it died without a result. That says
    nothing about the stock and must not be counted against it.
    """
    if not api_key:
        raise SandboxError("no Anthropic API key configured")
    env = base_env(s, run_dir)
    env["ANTHROPIC_API_KEY"] = api_key
    cmd = [s.claude_bin, "-p", "--bare",
           "--model", model,
           "--max-budget-usd", f"{budget_usd * s.cli_budget_fraction:.2f}",
           "--output-format", "json",
           "--no-session-persistence",
           "--tools", CLAUDE_TOOLS,
           "--allowedTools", CLAUDE_TOOLS,
           "--disallowedTools", "WebFetch,WebSearch",
           "--append-system-prompt", system_prompt]
    pr = run_cmd(s, cmd, cwd=run_dir, env=env, timeout=s.run_timeout_s, stdin_text=prompt)
    result = parse_claude_result(pr.stdout)
    cost = result.get("total_cost_usd") if result else None
    reported = isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
    if reported:
        cost_usd, cost_source = float(cost), "reported"
    elif result is None and not pr.timed_out and pr.duration_s < FAST_FAIL_S:
        # e.g. exit 127 "node: not found": it never reached the API
        cost_usd, cost_source = 0.0, "cli_failed_fast"
    else:
        cost_usd, cost_source = float(budget_usd), "assumed_cap"
    if not reported:
        logger.warning(f"[deep-dcf] CLI reported no cost (exit {pr.returncode}, "
                       f"timed_out={pr.timed_out}, {pr.duration_s:.0f}s); booking "
                       f"${cost_usd:.2f} ({cost_source}). stderr tail: {pr.stderr.strip()[-300:]!r}")
    refused = bool(result and result.get("is_error")) and reported and cost_usd == 0.0
    return {
        "result": result,
        "cost_usd": cost_usd,
        "cost_source": cost_source,
        "infra_failure": refused or (result is None and not pr.timed_out),
        "timed_out": pr.timed_out,
        "returncode": pr.returncode,
        "duration_s": pr.duration_s,
        "is_error": bool(result.get("is_error")) if result else True,
        "subtype": (result or {}).get("subtype"),
        "num_turns": (result or {}).get("num_turns"),
        "final_text": str((result or {}).get("result") or "")[:2000],
        "stderr_tail": pr.stderr.strip()[-600:],
    }


def run_check(s: Settings, *, run_dir: Path, trusted_inputs: Path, timeout: float = 900) -> dict:
    """The authoritative acceptance check: tools/check_delivery.py, run as the
    sandbox user against the run dir, with the runner's own copy of the inputs
    and thresholds (so the model cannot have edited either)."""
    env = base_env(s, run_dir)
    cmd = [str(s.venv_bin / "python3"), str(s.tools_dir / "check_delivery.py"),
           "--dir", str(run_dir), "--inputs", str(trusted_inputs),
           "--accept-json", json.dumps(s.accept, sort_keys=True)]
    pr = run_cmd(s, cmd, cwd=run_dir, env=env, timeout=timeout)
    if pr.timed_out:
        return {"verdict": "ERROR", "errors": [f"acceptance check timed out after {timeout:.0f}s"],
                "warnings": [], "values": {}}
    try:
        out = json.loads(pr.stdout)
        if not isinstance(out, dict) or out.get("verdict") not in ("PASS", "FAIL", "ERROR"):
            raise ValueError("unexpected shape")
    except ValueError:
        return {"verdict": "ERROR",
                "errors": [f"acceptance check produced no verdict (exit {pr.returncode}): "
                           f"{(pr.stderr or pr.stdout).strip()[-300:]}"],
                "warnings": [], "values": {}}
    out.setdefault("errors", [])
    out.setdefault("warnings", [])
    out.setdefault("values", {})
    return out


def prune_runs(s: Settings, symbol: str, keep: int = KEEP_RUNS_PER_SYMBOL) -> None:
    """Keep the newest few run dirs per symbol (forensics), delete the rest."""
    sym_dir = s.runs_dir / symbol
    try:
        runs = sorted((p for p in sym_dir.iterdir() if p.is_dir() and not p.is_symlink()),
                      key=lambda p: p.name)
    except OSError:
        return
    for old in runs[:-keep] if keep > 0 else runs:
        shutil.rmtree(old, ignore_errors=True)
        for extra in sym_dir.glob(f"{old.name}.*inputs.json"):      # incl. recheck copies
            try:
                extra.unlink()
            except OSError:
                pass


def run_builder(s: Settings, *, run_dir: Path, trusted_inputs: Path, timeout: float = 180) -> dict:
    """tools/build_model.py on the model's assumptions.json, as the sandbox
    user. → {"ok": bool, "errors": [...], "values": {...}, "crash": bool}."""
    env = base_env(s, run_dir)
    cmd = [str(s.venv_bin / "python3"), str(s.tools_dir / "build_model.py"),
           "--dir", str(run_dir), "--inputs", str(trusted_inputs),
           "--assumptions", str(run_dir / "assumptions.json")]
    pr = run_cmd(s, cmd, cwd=run_dir, env=env, timeout=timeout)
    if pr.timed_out:
        return {"ok": False, "errors": [f"workbook build timed out after {timeout:.0f}s"], "crash": True}
    try:
        out = json.loads(pr.stdout)
        if not isinstance(out, dict) or "ok" not in out:
            raise ValueError("unexpected shape")
    except ValueError:
        return {"ok": False, "crash": True,
                "errors": [f"builder produced no result (exit {pr.returncode}): "
                           f"{(pr.stderr or pr.stdout).strip()[-300:]}"]}
    out.setdefault("errors", [])
    out["crash"] = bool(out.get("crash"))
    return out


def preflight(s: Settings, engine: Optional[str] = None) -> list[str]:
    """Everything a paid run needs, checked before a cent is spent. The batch
    engine never starts the Claude CLI, so only the agentic engine needs it."""
    engine = engine or s.engine
    agentic = engine == "agentic"
    problems: list[str] = []
    try:
        _ids(s)
    except SandboxError as e:
        problems.append(str(e))
    if agentic and not Path(s.claude_bin).exists():
        problems.append(f"Claude CLI not found at {s.claude_bin}")
    if not (s.venv_bin / "python3").exists():
        problems.append(f"sandbox venv missing at {s.venv_bin} — run infrastructure/setup_deep_dcf.sh")
    tools = ("recalc.py", "check_delivery.py") + (() if agentic else ("build_model.py",))
    for tool in tools:
        if not (s.tools_dir / tool).is_file():
            problems.append(f"tool {tool} missing in {s.tools_dir} — run infrastructure/setup_deep_dcf.sh")
    if agentic:
        for rel in ("SKILL.md", "scripts/validate_dcf.py"):
            if not (s.skill_dir / rel).is_file():
                problems.append(f"dcf-model skill missing at {s.skill_dir} ({rel})")
    else:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            problems.append("the anthropic SDK is not installed in the runner's venv")
    if not (shutil.which("soffice") or Path("/usr/bin/soffice").exists()):
        problems.append("LibreOffice (soffice) is not installed")
    if not unsandboxed_ok():
        for d in (s.sandbox_home, s.runs_dir):
            if not d.is_dir():
                problems.append(f"{d} does not exist — run infrastructure/setup_deep_dcf.sh")
    key, _ = s.api_key()
    if not key:
        problems.append("no Anthropic API key (DEEP_DCF_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY)")
    if not problems:
        # Everything exists — but does it RUN as the sandbox user? (A venv or a
        # CLI readable only by root passes every check above and fails every run.)
        env = base_env(s, s.sandbox_home)
        env["TMPDIR"] = "/tmp"
        checks = [("python3 with openpyxl", [str(s.venv_bin / "python3"), "-c", "import openpyxl"]),
                  ("LibreOffice", ["soffice", "--version"])]
        if agentic:
            checks.insert(0, ("the Claude CLI", [s.claude_bin, "--version"]))
        for what, argv in checks:
            try:
                pr = run_cmd(s, argv, cwd=s.sandbox_home, env=env, timeout=90)
            except Exception as e:
                problems.append(f"{what} could not be started as the sandbox user: {e!r}")
                continue
            if pr.timed_out or pr.returncode != 0:
                problems.append(f"{what} does not run as the sandbox user (exit {pr.returncode}): "
                                f"{(pr.stderr or pr.stdout).strip()[-160:]}")
    return problems
