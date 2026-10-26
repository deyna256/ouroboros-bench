"""A fresh isolated Ouroboros server for each conversation."""

import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from convy import AgentFailure
from dotenv import dotenv_values
from msgspec import Struct

HERE = pathlib.Path(__file__).resolve().parent
BENCH = HERE.parent
CHECKOUT = BENCH / "ouroboros"
SERVERS = BENCH / ".servers"
# What a launch may see of `.env` and the environment: Ouroboros's settings and its providers'
# keys, the families Ouroboros's own server_runner treats as settings, and what a process needs
# to run. Everything else, convy's gateway key included, stays out of the sandbox.
OUROBOROS_KEYS = (
    "OUROBOROS_", "OPENROUTER_", "OPENAI_", "ANTHROPIC_", "MINIMAX_", "DEEPSEEK_", "ZAI_",
    "CLOUDRU_", "GIGACHAT_", "LOCAL_MODEL_", "USE_LOCAL_",
)  # fmt: skip
PROCESS_KEYS = frozenset(("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ"))


class Server(Struct, frozen=True):
    """An Ouroboros server of the checkout in `ouroboros/` with a profile, `min`, `mid` or
    `max`; `stub` serves a scripted local model instead of a real one. Each start clones the
    checkout's HEAD and a new data root, so no conversation sees another one's memory.

    The server gets its models and keys from `.env` and the environment, the environment
    first, as convy's own settings do; `launch.py` keeps only the model settings and the
    credentials of the providers those models need.

    With `sandbox`, the default, the server runs in a bubblewrap sandbox: Ouroboros's tools run
    real commands, and there they see the system read-only, an empty home and a private /tmp.
    Of the bench they see only the Ouroboros checkout and this package, read-only, and
    `.servers/`, and `skills` when given. The network stays open for the model's API. The
    sandbox's processes die with it, so none outlives a conversation.

    `skills` names a folder of skills (`<name>/SKILL.md` or `skill.json` each) that every server
    reads in place and turns on before the conversation, as their owner would (see `launch.py`).
    """

    profile: str
    stub: bool = False
    sandbox: bool = True
    skills: str | None = None  # a folder of skills, from the bench root; each one is turned on

    def __post_init__(self) -> None:
        if self.profile not in ("min", "mid", "max"):
            raise ValueError(f"profile must be min, mid or max, got {self.profile!r}")
        if self.skills is not None and not self.skill_folder().is_dir():
            raise ValueError(f"no skills folder at {self.skill_folder()}")

    def skill_folder(self) -> pathlib.Path | None:
        return (BENCH / self.skills).resolve() if self.skills is not None else None

    @asynccontextmanager
    async def started(self) -> AsyncIterator[str]:
        """The server's address while it runs; it stops when the block ends."""
        python = CHECKOUT / ".venv" / "bin" / "python"
        if not python.exists():
            raise AgentFailure(f"no Ouroboros environment at {python}: run `just setup`")
        SERVERS.mkdir(exist_ok=True)
        environment = self.environment()
        with tempfile.TemporaryFile() as errors:  # read only when the launch fails
            process = await asyncio.create_subprocess_exec(
                *self.jail(python),
                python,
                str(HERE / "launch.py"),
                str(CHECKOUT),
                self.profile,
                *(["--stub"] if self.stub else []),
                *(["--skills", str(self.skill_folder())] if self.skills is not None else []),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=errors,
                env=environment,
            )
            try:
                line = await process.stdout.readline()
                if not line:
                    await process.wait()
                    errors.seek(0)
                    error = errors.read().decode(errors="replace").strip()
                    raise AgentFailure(f"Ouroboros did not start: {error[-2000:]}")
                yield json.loads(line)["url"]
            finally:
                stopping = asyncio.ensure_future(self.stopped(process))
                cancelled = False
                while not stopping.done():  # the server stops however often this is cancelled
                    try:
                        await asyncio.shield(stopping)
                    except asyncio.CancelledError:
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError

    async def stopped(self, process: asyncio.subprocess.Process) -> None:
        """Stop a launch, gently first: closing stdin stops a server that is up; SIGTERM, one
        still starting; SIGKILL only a launch that ignored both, whose folder the next launch
        sweeps."""
        if process.returncode is None:
            process.stdin.close()
        for signal_it, seconds in ((None, 30), (process.terminate, 40), (process.kill, None)):
            if process.returncode is not None:
                return
            if signal_it is not None:
                signal_it()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), seconds)

    def environment(self) -> dict[str, str]:
        found = {
            **{key: value for key, value in dotenv_values(".env").items() if value is not None},
            **os.environ,
        }
        return {
            key: value
            for key, value in found.items()
            if key in PROCESS_KEYS or key.startswith(OUROBOROS_KEYS)
        }

    def jail(self, python: pathlib.Path) -> list[str]:
        """The bubblewrap command a sandboxed launch runs under; none without `sandbox`."""
        if not self.sandbox:
            return []
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise AgentFailure("no bubblewrap (bwrap) to sandbox Ouroboros: install it")
        git = subprocess.run(
            ["git", "-C", str(CHECKOUT), "rev-parse", "--absolute-git-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        shown = [
            # uv's interpreter store, under the hidden home: the venv names its interpreter
            # through the store's link to the minor version, so the whole store is shown.
            pathlib.Path(os.path.realpath(python)).parent.parent.parent,
            CHECKOUT,
            pathlib.Path(git),  # a submodule's history lives in the bench's .git/modules
            HERE,
            *([self.skill_folder()] if self.skills is not None else []),
        ]  # not the whole bench: its scenarios, results and .env stay out of the agent's reach
        resolver = pathlib.Path(os.path.realpath("/etc/resolv.conf"))
        if resolver.is_relative_to("/run"):  # systemd-resolved keeps it under the hidden /run
            shown.append(resolver.parent)
        alternates = pathlib.Path(git) / "objects" / "info" / "alternates"
        if alternates.exists():  # objects borrowed from another clone, as --reference leaves
            shown += [pathlib.Path(line) for line in alternates.read_text().split() if line]
        return [
            bwrap,
            *("--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"),
            # Sockets in /run (Docker's, the session bus, the user's agents) take commands
            # however read-only the bind, so the sandbox sees none of them.
            *("--tmpfs", "/run"),
            *("--tmpfs", str(pathlib.Path.home()), "--tmpfs", str(BENCH)),  # wherever it lives
            *(arg for path in shown for arg in ("--ro-bind", str(path), str(path))),
            *("--bind", str(SERVERS), str(SERVERS)),
            *("--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session"),
        ]

    def version(self) -> str:
        """The build a run measures: the checkout's commit, the profile, the models, this
        package's code and the skills, so `convy resume` refuses to mix two of them in one run."""
        commit = subprocess.run(
            ["git", "-C", str(CHECKOUT), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        environment = self.environment()
        models = (
            "stub"
            if self.stub
            else "+".join(
                environment.get(key, "default")
                for key in ("OUROBOROS_MODEL", "OUROBOROS_MODEL_LIGHT")
            )
        )
        code = hashlib.sha256(b"".join(path.read_bytes() for path in sorted(HERE.glob("*.py"))))
        if (skills := self.skill_folder()) is not None:  # a skill's file names matter, as its bytes
            for path in sorted(path for path in skills.rglob("*") if path.is_file()):
                code.update(f"{path.relative_to(skills)}\0".encode() + path.read_bytes())
        return f"{commit}-{self.profile}-{models}-{code.hexdigest()[:8]}"
