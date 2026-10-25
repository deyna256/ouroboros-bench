"""Start one isolated Ouroboros server and hold it until stdin closes.

Runs inside Ouroboros's own environment (`ouroboros/.venv`), not convy's: it reuses Ouroboros's
benchmark runner (`devtools/benchmarks/common/server_runner.py`), which clones the repository,
seeds a fresh data root and waits for the supervisor. It prints one JSON line, `{"url": ...}`,
then waits; closing stdin, SIGTERM, SIGHUP or Ctrl+C stops the server and deletes its clone
and data. They live in `.servers/` of the bench repository, on disk rather than in a tmpfs
`/tmp`, and each start sweeps what a killed launch left there.

    python launch.py <ouroboros checkout> <profile> [--stub]

The profile sets how much Ouroboros puts into its context and how hard its model thinks. Models
and keys come from the environment: OUROBOROS_MODEL, OUROBOROS_MODEL_LIGHT and the provider's
key, such as OPENROUTER_API_KEY. `--stub` serves a scripted local model instead: free, for tests.
"""

import argparse
import fcntl
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile

PROFILES = {
    "min": {"OUROBOROS_CONTEXT_MODE": "nano", "OUROBOROS_EFFORT_TASK": "low"},
    "mid": {"OUROBOROS_CONTEXT_MODE": "low", "OUROBOROS_EFFORT_TASK": "medium"},
    "max": {"OUROBOROS_CONTEXT_MODE": "max", "OUROBOROS_EFFORT_TASK": "high"},
}
# Light: Ouroboros may not rewrite itself during a run, so every conversation meets the same build.
# Three workers per server: the size Ouroboros's own CL-Bench runs settled on; ten, the default,
# lets a few servers at once fill a laptop's memory.
COMMON = {
    "OUROBOROS_RUNTIME_MODE": "light",
    "OUROBOROS_CONTEXT_MODE_AUTO_LOW": "false",
    "OUROBOROS_MAX_WORKERS": int(os.environ.get("OUROBOROS_MAX_WORKERS", 3)),
}
SERVERS = pathlib.Path(__file__).resolve().parent.parent / ".servers"


def sweep() -> None:
    """Remove what launches killed with SIGKILL left behind: each launch holds a lock on its
    folder while it lives, so a folder whose lock is free is a dead launch's. Server processes
    still running inside it, found by working directory, are killed first; in the sandbox there
    are none, since its namespace dies with the launch.
    ponytail: /proc is Linux only; elsewhere a killed launch's server needs a manual kill."""
    for folder in SERVERS.glob("server-*"):
        try:
            lock = os.open(folder / "lock", os.O_RDWR)
        except OSError:
            continue  # a launch that has not taken its lock yet
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            continue  # its launch is alive
        finally:
            os.close(lock)
        for proc in pathlib.Path("/proc").glob("[0-9]*"):
            try:
                if pathlib.Path(os.readlink(proc / "cwd")).is_relative_to(folder):
                    os.killpg(os.getpgid(int(proc.name)), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        shutil.rmtree(folder, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=pathlib.Path)
    parser.add_argument("profile", choices=sorted(PROFILES))
    parser.add_argument("--stub", action="store_true")
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    sys.path.insert(0, str(checkout))
    from devtools.benchmarks.common.server_runner import (
        IsolatedServer,
        build_isolated_settings,
        seed_owner_state,
    )
    from supervisor import state

    for sig in (signal.SIGTERM, signal.SIGHUP):  # run the cleanup below, as Ctrl+C does
        signal.signal(sig, lambda *_: sys.exit(1))
    SERVERS.mkdir(exist_ok=True)
    sweep()
    root = pathlib.Path(tempfile.mkdtemp(prefix="server-", dir=SERVERS))
    lock = os.open(root / "lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(lock, fcntl.LOCK_EX)  # held until this process ends, however it ends
    stub = None
    try:
        clone, data = root / "clone", root / "data"
        data.mkdir()
        for command in (
            ["git", "clone", "-q", "--shared", str(checkout), str(clone)],
            ["git", "-C", str(clone), "checkout", "-q", "-B", "ouroboros"],
            ["git", "-C", str(clone), "remote", "remove", "origin"],
        ):
            subprocess.run(command, check=True)
        overrides = {**COMMON, **PROFILES[args.profile]}
        if args.stub:
            from tests.system_e2e import harness

            stub = harness.ScriptedStubModel(
                script=[{"tool": "list_files", "arguments": {"path": "."}}],
                final_answer="Stub answer.",
            )
            stub.__enter__()
            settings = harness.keyless_settings(stub, **overrides)
        else:
            settings = build_isolated_settings(dict(os.environ), **overrides)
            settings.setdefault("TOTAL_BUDGET", float(os.environ.get("OUROBOROS_BUDGET_USD", 10)))
        path = data / "settings.json"
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        seed_owner_state(data)
        (data / state.ISOLATED_BENCHMARK_SENTINEL).write_text("convy\n", encoding="utf-8")
        server = IsolatedServer(clone, data, path, settings_authoritative_env=True)
        server.start(ready_timeout=300)
        try:
            print(json.dumps({"url": server.base_url, **server.attestation}), flush=True)
            sys.stdin.read()
        finally:
            server.stop()
    finally:
        if stub is not None:
            stub.__exit__(None, None, None)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
