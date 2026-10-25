"""What Ouroboros did for one answer, read from its tasks' event streams.

`POST /api/tasks/<id>/events` replays a task tree's durable rows: the model's narration
(`progress`), each model round (`events`) and each tool call (`tools`). The rows convy shows
are turned into these types where they enter; the rest are skipped.
"""

from datetime import datetime, timedelta
from typing import Any

import msgspec
from convy import Call, NotReported, Reported, Thought, Usage
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


class ToolStart(Struct, frozen=True, kw_only=True, tag_field="type", tag="tool_call_started"):
    """The host began a tool call, with the arguments the model sent."""

    ts: datetime
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
KINDS = frozenset(("llm_round", "tool_call_started", "tool_call", "send_message"))


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

    def usage(self) -> Usage | None:
        """The sum as convy's `Usage`, or None when a call did not report its input or output."""
        if not self.calls or any(
            call.prompt_tokens is None or call.completion_tokens is None for call in self.calls
        ):
            return None
        total_in = sum(call.prompt_tokens for call in self.calls)
        total_out = sum(call.completion_tokens for call in self.calls)
        thinking = self.part("reasoning_tokens")
        read, written = self.part("cached_tokens"), self.part("cache_write_tokens")
        if isinstance(thinking, int) and thinking > total_out:
            thinking = NotReported()  # a provider that counts thinking apart from the output
        cached = sum(part for part in (read, written) if isinstance(part, int))
        if cached > total_in:
            read = written = NotReported()
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
        starts = {
            row.invocation_id: row
            for row in self.rows
            if isinstance(row, ToolStart) and row.invocation_id
        }
        reasoned = {
            row.reasoning_text.strip()
            for row in self.rows
            if isinstance(row, Round) and row.reasoning_text and row.reasoning_text.strip()
        }
        timed: list[tuple[datetime, Thought | Call]] = []
        for row in self.rows:
            match row:
                case Round():
                    seconds = (row.duration_ms or 0) / 1000
                    started = row.ts - timedelta(seconds=seconds)
                    if row.reasoning_text and row.reasoning_text.strip():
                        timed.append((started, Thought(row.reasoning_text.strip())))
                    usage = Counts((row,)).usage()
                    timed.append(
                        (
                            started,
                            Call(
                                name=self.named(f"model {row.model}", row.task_id),
                                started=started,
                                seconds=seconds,
                                **({"usage": usage} if usage else {}),
                            ),
                        )
                    )
                case ToolEnd():
                    seconds = max((row.elapsed_ms or 0) / 1000, 0.0)
                    start = starts.get(row.invocation_id)
                    started = start.ts if start else row.ts - timedelta(seconds=seconds)
                    # A call refused for its arguments settles with none: the start kept them.
                    args = row.args if row.args or start is None else start.args
                    timed.append(
                        (
                            started,
                            Call(
                                name=self.named(row.tool, row.task_id),
                                started=started,
                                seconds=seconds,
                                sent=Reported(msgspec.json.encode(args).decode()),
                                received=Reported(row.result_preview),
                            ),
                        )
                    )
                case Narration(narration=True):
                    text = row.text.removeprefix(NARRATION_MARK).strip()
                    if (
                        text and text not in reasoned
                    ):  # a round without words narrates its reasoning
                        timed.append((row.ts, Thought(text)))
                case _:
                    pass
        timed.sort(key=lambda pair: pair[0])
        return tuple(step for _, step in timed)

    def tasks(self) -> frozenset[str]:
        """Every task with a row here: the roots and the helpers they started."""
        return self.roots | {row.task_id for row in self.rows if not isinstance(row, ToolStart)}

    def named(self, name: str, task_id: str) -> str:
        return name if task_id in self.roots else f"{name} (helper {task_id})"
