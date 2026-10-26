"""Start one isolated Ouroboros server and hold it until stdin closes.

Runs inside Ouroboros's own environment (`ouroboros/.venv`), not convy's: it reuses Ouroboros's
benchmark runner (`devtools/benchmarks/common/server_runner.py`), which clones the repository,
seeds a fresh data root and waits for the supervisor. It prints one JSON line, `{"url": ...}`,
then waits; closing stdin, SIGTERM, SIGHUP or Ctrl+C stops the server and deletes its clone
and data. They live in `.servers/` of the bench repository, on disk rather than in a tmpfs
`/tmp`, and each start sweeps what a killed launch left there.

    python launch.py <ouroboros checkout> <profile> [--stub] [--skills <folder>]

The profile sets how much Ouroboros puts into its context and how hard its model thinks. Models
and keys come from the environment: OUROBOROS_MODEL, OUROBOROS_MODEL_LIGHT and the provider's
key, such as OPENROUTER_API_KEY. `--stub` serves a scripted local model instead: free, for tests.
`--skills` turns on each skill in a folder, read in place, before the conversation.
"""

import argparse
import contextlib
import fcntl
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

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
BUILTIN = {"id": "builtin", "name": "Built in", "kind": "builtin", "trust": "trusted"}


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


def turn_on(url: str) -> None:
    """Turn on every skill read from `skills/`, as its owner would: skip the paid review of its
    exact bytes (the deterministic preflight still runs), grant what its manifest asks for, enable.
    Putting a skill in the bench is the owner's decision; a skill that will not turn on fails the
    launch, so no conversation runs without it."""

    def call(method: str, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            url + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            sys.exit(f"{method} {path}: {error.code} {error.read().decode(errors='replace')[:500]}")

    for skill in call("GET", "/api/extensions")["skills"]:
        if skill.get("source") != "user_repo":
            continue
        name, revision = skill["name"], {"expected_content_hash": skill["content_hash"]}
        call("POST", f"/api/owner/skills/{name}/attest-review", revision)
        grants = skill.get("grants") or {}
        wanted = [*grants.get("requested_keys", []), *grants.get("requested_permissions", [])]
        if wanted:
            call("POST", f"/api/skills/{name}/grants", {"items": wanted, **revision})
        done = call("POST", f"/api/skills/{name}/toggle", {"enabled": True, **revision})
        if done.get("enabled") is not True:
            sys.exit(f"skill {name} did not turn on: {json.dumps(done, ensure_ascii=False)[:500]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=pathlib.Path)
    parser.add_argument("profile", choices=sorted(PROFILES))
    parser.add_argument("--stub", action="store_true")
    parser.add_argument("--skills", type=pathlib.Path, help="a folder of skills to turn on")
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
    # Closed stdin stops the launch at any point, even while its server starts: in the sandbox a
    # signal from outside reaches bubblewrap, which ends everything without this cleanup.
    threading.Thread(
        target=lambda: (sys.stdin.read(), os.kill(os.getpid(), signal.SIGTERM)), daemon=True
    ).start()
    SERVERS.mkdir(exist_ok=True)
    with contextlib.ExitStack() as cleanup:
        # One launch starts at a time: a server binds the port it was given seconds later, and
        # the sweep must not take a folder whose launch has not locked it yet.
        starting = os.open(SERVERS / "starting.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(starting, fcntl.LOCK_EX)
        sweep()
        root = pathlib.Path(tempfile.mkdtemp(prefix="server-", dir=SERVERS))
        cleanup.callback(shutil.rmtree, root, ignore_errors=True)
        lock = os.open(root / "lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(lock, fcntl.LOCK_EX)  # held until this process ends, however it ends
        clone, data = root / "clone", root / "data"
        data.mkdir()
        for command in (
            ["git", "clone", "-q", "--shared", str(checkout), str(clone)],
            ["git", "-C", str(clone), "checkout", "-q", "-B", "ouroboros"],
            ["git", "-C", str(clone), "remote", "remove", "origin"],
        ):
            subprocess.run(command, check=True)
        overrides = {**COMMON, **PROFILES[args.profile]}
        # Skills come from `--skills` alone, read in place, each turned on before the conversation
        # (`turn_on`). No hub is read and no starter skill installed, so a run does not change
        # with what a hub recommends on the day.
        folder = {"id": "bench", "name": "Bench", "kind": "local", "trust": "trusted"}
        sources = [BUILTIN, *([{**folder, "path": str(args.skills)}] if args.skills else [])]
        overrides["OUROBOROS_SKILL_SOURCES"] = json.dumps(sources)
        overrides["OUROBOROS_STARTER_SKILLS"] = "[]"
        if args.stub:
            from tests.system_e2e import harness

            stub = cleanup.enter_context(
                harness.ScriptedStubModel(
                    script=[{"tool": "list_files", "arguments": {"path": "."}}],
                    final_answer="Stub answer.",
                )
            )
            settings = harness.keyless_settings(stub, **overrides)
        else:
            overrides["TOTAL_BUDGET"] = float(os.environ.get("OUROBOROS_BUDGET_USD", 10))
            settings = build_isolated_settings(dict(os.environ), **overrides)
        path = data / "settings.json"
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        seed_owner_state(data)
        (data / state.ISOLATED_BENCHMARK_SENTINEL).write_text("convy\n", encoding="utf-8")
        server = IsolatedServer(clone, data, path, settings_authoritative_env=True)
        server.start(ready_timeout=300)
        cleanup.callback(server.stop)
        fcntl.flock(starting, fcntl.LOCK_UN)
        turn_on(server.base_url)
        print(json.dumps({"url": server.base_url}), flush=True)
        threading.Event().wait()  # until stdin closes or a signal comes


if __name__ == "__main__":
    main()
