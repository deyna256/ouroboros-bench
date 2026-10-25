"""A fresh isolated Ouroboros server for each conversation."""

import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from convy import AgentFailure
from dotenv import dotenv_values
from msgspec import Struct

HERE = pathlib.Path(__file__).resolve().parent
CHECKOUT = HERE.parent / "ouroboros"


class Server(Struct, frozen=True):
    """An Ouroboros server of the checkout in `ouroboros/` with a profile, `min`, `mid` or
    `max`; `stub` serves a scripted local model instead of a real one. Each start clones the
    checkout's HEAD and a new data root, so no conversation sees another one's memory.

    The server gets its models and keys from `.env` and the environment, the environment
    first, as convy's own settings do; `launch.py` keeps only the model settings and the
    credentials of the providers those models need."""

    profile: str
    stub: bool = False
    checkout: pathlib.Path = CHECKOUT

    def __post_init__(self) -> None:
        if self.profile not in ("min", "mid", "max"):
            raise ValueError(f"profile must be min, mid or max, got {self.profile!r}")

    @asynccontextmanager
    async def started(self) -> AsyncIterator[str]:
        """The server's address while it runs; it stops when the block ends."""
        python = self.checkout / ".venv" / "bin" / "python"
        if not python.exists():
            raise AgentFailure(f"no Ouroboros environment at {python}: run `just setup`")
        process = await asyncio.create_subprocess_exec(
            python,
            str(HERE / "launch.py"),
            str(self.checkout),
            self.profile,
            *(["--stub"] if self.stub else []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **{key: value for key, value in dotenv_values(".env").items() if value is not None},
                **os.environ,
            },
        )
        try:
            line = await process.stdout.readline()
            if not line:
                error = (await process.stderr.read()).decode(errors="replace").strip()
                raise AgentFailure(f"Ouroboros did not start: {error[-2000:]}")
            yield json.loads(line)["url"]
        finally:
            stopping = asyncio.ensure_future(self.stopped(process))
            try:
                await asyncio.shield(stopping)
            except asyncio.CancelledError:
                await stopping  # a cancelled conversation still stops its server before it ends
                raise

    async def stopped(self, process: asyncio.subprocess.Process) -> None:
        """Stop a launch, gently first: closing stdin stops a server that is up; SIGTERM, one
        still starting; SIGKILL only a launch that ignored both, whose server the next launch
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

    def version(self) -> str:
        """The checkout's commit and the profile: the build a run measures."""
        commit = subprocess.run(
            ["git", "-C", str(self.checkout), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return f"{commit}-{self.profile}{'-stub' if self.stub else ''}"
