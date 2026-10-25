"""What Ouroboros did for one answer, read from its tasks' event streams.

`POST /api/tasks/<id>/events` replays a task tree's durable rows: the model's narration
(`progress`), each model round (`events`) and each tool call (`tools`). The rows convy shows
are turned into these types where they enter; the rest are skipped.
"""

from datetime import datetime, timedelta
from typing import Any

import msgspec
from convy import Call, NotReported, NoUsage, Reported, Thought, Usage
from msgspec import Struct

NARRATION_MARK = "💬"  # the host's prefix on a narration bubble, not the model's words


class Round(Struct, frozen=True, kw_only=True, tag_field="type", tag="llm_round"):
    """One round of a task's model loop."""

    ts: datetime
    task_id: str
    model: str = ""
    duration_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    reasoning_text: str | None = None

    @property
    def thought(self) -> str:
        """The round's readable reasoning, empty when it had none."""
        return (self.reasoning_text or "").strip()


class ToolStart(Struct, frozen=True, kw_only=True, tag_field="type", tag="tool_call_started"):
    """The host began a tool call, with the arguments the model sent."""

    ts: datetime
    task_id: str
    invocation_id: str = ""
    args: Any = None


class ToolEnd(Struct, frozen=True, kw_only=True, tag_field="type", tag="tool_call"):
    """A tool call settled: its arguments, the start of its result and its time."""

    ts: datetime
    task_id: str
    tool: str
    invocation_id: str = ""
    elapsed_ms: int | None = None
    args: Any = None
    result_preview: str = ""


class Narration(Struct, frozen=True, kw_only=True, tag_field="type", tag="send_message"):
    """A progress line; `narration` marks the model's own words."""

    ts: datetime
    task_id: str
    text: str = ""
    narration: bool = False


Row = Round | ToolStart | ToolEnd | Narration


class Ledger(Struct, frozen=True):
    """One task's own tokens by its usage ledger, which holds every model call it paid for:
    loop rounds, safety checks, naming and post-task work alike."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


class Counts(Struct, frozen=True):
    """Token counters summed over model calls or tasks: a part stays unknown unless every one
    reported it."""

    calls: tuple[Round | Ledger, ...]

    def usage(self) -> Usage | NoUsage:
        """The sum as convy's `Usage`; `NoUsage` when a call did not report its input or output."""
        if not self.calls or any(
            call.prompt_tokens is None or call.completion_tokens is None for call in self.calls
        ):
            return NoUsage()
        total_in = sum(call.prompt_tokens for call in self.calls)
        total_out = sum(call.completion_tokens for call in self.calls)
        thinking = self.part("reasoning_tokens")
        read, written = self.part("cached_tokens"), self.part("cache_write_tokens")
        match thinking:
            case int() if thinking > total_out:  # a provider counting thinking apart
                thinking = NotReported()
        match read, written:
            case int(), int() if read + written > total_in:  # cache counted apart from the input
                read = written = NotReported()
            case int(), NotReported() if read > total_in:
                read = NotReported()
        return Usage(total_in, total_out, thinking=thinking, cache_read=read, cache_write=written)

    def part(self, name: str) -> int | NotReported:
        values = [getattr(call, name) for call in self.calls]
        return NotReported() if any(value is None for value in values) else sum(values)


class Trace(Struct, frozen=True):
    """The rows of the task trees that made one answer; `roots` are their root tasks, the
    rest of the rows belong to helpers they started."""

    rows: tuple[Row, ...]
    roots: frozenset[str]

    def steps(self) -> tuple[Thought | Call, ...]:
        """What the agent thought and called, in order of time."""
        starts: dict[str, ToolStart] = {}
        reasoned: set[str] = set()
        for row in self.rows:
            match row:
                case ToolStart(invocation_id=str(invocation)) if invocation:
                    starts[invocation] = row
                case Round(thought=str(thought)) if thought:
                    reasoned.add(thought)
        timed: list[tuple[datetime, Thought | Call]] = []
        for row in self.rows:
            match row:
                case Round():
                    seconds = (row.duration_ms or 0) / 1000
                    started = row.ts - timedelta(seconds=seconds)
                    if row.thought:
                        timed.append((started, Thought(row.thought)))
                    call = Call(
                        name=self.named(f"model {row.model}", row.task_id),
                        started=started,
                        seconds=seconds,
                        usage=Counts((row,)).usage(),
                    )
                    timed.append((started, call))
                case ToolEnd():
                    seconds = max((row.elapsed_ms or 0) / 1000, 0.0)
                    start = starts.get(row.invocation_id)
                    started = start.ts if start else row.ts - timedelta(seconds=seconds)
                    # A call refused for its arguments settles with none: the start kept them.
                    args = row.args if row.args or start is None else start.args
                    call = Call(
                        name=self.named(row.tool, row.task_id),
                        started=started,
                        seconds=seconds,
                        sent=Reported(msgspec.json.encode(args).decode()),
                        received=Reported(row.result_preview),
                    )
                    timed.append((started, call))
                case Narration(narration=True):
                    text = row.text.removeprefix(NARRATION_MARK).strip()
                    if text and text not in reasoned:  # a wordless round narrates its reasoning
                        timed.append((row.ts, Thought(text)))
        timed.sort(key=lambda pair: pair[0])
        return tuple(step for _, step in timed)

    def tasks(self) -> frozenset[str]:
        """Every task with a row here: the roots and the helpers they started."""
        return self.roots | {row.task_id for row in self.rows}

    def named(self, name: str, task_id: str) -> str:
        return name if task_id in self.roots else f"{name} (helper {task_id})"
