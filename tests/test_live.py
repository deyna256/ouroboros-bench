"""Real Ouroboros servers with a scripted model: free, about twenty seconds to start each.

uv run pytest -m live
"""

import asyncio
import pathlib
import subprocess

import pytest
from convy import Call, Message, Thought, Usage

from ouroboros_convy import Ouroboros, Server
from ouroboros_convy.server import BENCH, CHECKOUT, SERVERS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (CHECKOUT / ".venv").exists(), reason="no Ouroboros environment: run `just setup`"
    ),
]


def left_behind() -> tuple[set[int], set[str]]:
    """Processes running inside `.servers/`, by working directory, and the folders there."""
    processes = set()
    for proc in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            if (proc / "cwd").readlink().is_relative_to(SERVERS):
                processes.add(int(proc.name))
        except OSError:
            pass
    return processes, {folder.name for folder in SERVERS.glob("server-*")}


@pytest.fixture
def clean():
    """Fails the test when it leaves a server or its folder behind."""
    before = left_behind()
    yield
    processes, folders = left_behind()
    assert processes <= before[0] and folders <= before[1]


async def test_a_conversation_answers_with_steps_and_tokens(clean):
    async with Ouroboros(Server("min", stub=True)).conversation() as chat:
        first = await chat.answer(Message("What is 6 times 7? Check the files first."))
        second = await chat.answer(Message("And 7 times 8?"))
    assert first.text == second.text == "Stub answer."
    assert isinstance(first.usage, Usage) and first.usage.input > 0
    tools = [step for step in first.steps if isinstance(step, Call) and step.name == "list_files"]
    assert len(tools) == 1 and "BIBLE.md" in tools[0].received.text
    assert Thought("still working") in first.steps
    assert any(isinstance(step, Call) and step.name.startswith("model ") for step in second.steps)


async def test_a_conversation_cancelled_while_its_server_starts(clean):
    async def open_one():
        async with Ouroboros(Server("min", stub=True)).conversation():
            pass

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(open_one(), 8)  # the server takes about twenty seconds to be ready


@pytest.mark.parametrize("elsewhere", [False, True], ids=["bench-in-home", "bench-out-of-home"])
def test_the_sandbox_shows_ouroboros_neither_home_nor_scenarios_nor_keys(
    monkeypatch, tmp_path, elsewhere
):
    if elsewhere:  # the bench outside the hidden home
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".ssh").mkdir()
    monkeypatch.setenv("GW_API_KEY", "convy-gateway-key")
    server = Server("min")
    python = CHECKOUT / ".venv" / "bin" / "python"
    probe = (
        "import os, pathlib, sys; paths = [pathlib.Path(p) for p in sys.argv[1:]];"
        "print([p.exists() for p in paths], 'GW_API_KEY' in os.environ)"
    )
    hidden = [pathlib.Path.home() / ".ssh", BENCH / ".env", BENCH / "scenarios", BENCH / "results"]
    hidden += [pathlib.Path("/run/docker.sock"), pathlib.Path("/run/user")]
    shown = [CHECKOUT / "server.py", SERVERS]
    environment = server.environment()
    seen = subprocess.run(
        [*server.jail(python), str(python), "-c", probe, *map(str, hidden + shown)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    assert seen == f"{[False] * len(hidden) + [True] * len(shown)} False"
