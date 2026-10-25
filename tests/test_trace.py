"""A turn's steps and tokens, from rows a real Ouroboros server recorded (a scripted model:
one tool call, then the answer)."""

import json
import pathlib

import msgspec
import pytest
from convy import Call, NotReported, NoUsage, Reported, Thought, Usage

from ouroboros_convy.trace import Counts, Ledger, Round, Row, Trace

ROWS = json.loads((pathlib.Path(__file__).parent / "fixtures" / "turn_events.json").read_text())
ROOT = "18bb3252"
ROUND = next(row for row in ROWS if row["type"] == "llm_round")


def trace(*extra: dict) -> Trace:
    return Trace(tuple(msgspec.convert(row, Row) for row in (*ROWS, *extra)), frozenset({ROOT}))


def test_steps_follow_the_recorded_turn_in_time_order():
    steps = trace().steps()
    assert [type(step).__name__ for step in steps] == ["Call", "Thought", "Call", "Call"]
    first, thought, tool, last = steps
    assert first.name == last.name == "model openai-compatible/mock-model"
    assert first.usage == Usage(10, 5)
    assert thought == Thought("still working")  # the host's 💬 prefix is not the model's words
    assert tool.name == "list_files"
    assert tool.sent == Reported('{"path":"."}')
    assert isinstance(tool.received, Reported) and "BIBLE.md" in tool.received.text
    assert tool.seconds == pytest.approx(0.027)


def test_a_round_starts_its_duration_before_it_was_logged():
    first = trace().steps()[0]
    assert isinstance(first, Call) and first.seconds == pytest.approx(2.843)
    assert first.started.isoformat() == "2026-10-25T17:19:19.210811+00:00"


def test_reasoning_of_a_round_comes_before_its_call_and_is_not_repeated_by_narration():
    reasoning = {
        **ROUND,
        "ts": "2026-10-25T17:19:22.753000+00:00",
        "duration_ms": 1,
        "reasoning_text": "still working",
        "reasoning_tokens": 2,
    }
    steps = trace(reasoning).steps()
    assert [step for step in steps if isinstance(step, Thought)] == [Thought("still working")]
    index = steps.index(Thought("still working"))
    assert isinstance(steps[index + 1], Call) and steps[index + 1].usage.thinking == 2


def test_a_helpers_steps_name_the_helper():
    helper = {**ROUND, "task_id": "child1"}
    names = {step.name for step in trace(helper).steps() if isinstance(step, Call)}
    assert "model openai-compatible/mock-model (helper child1)" in names
    assert trace(helper).tasks() == {ROOT, "child1"}


def test_a_call_refused_for_its_arguments_shows_what_the_model_sent():
    start, end = (
        next(row for row in ROWS if row["type"] == kind)
        for kind in ("tool_call_started", "tool_call")
    )
    refused = Trace(
        tuple(
            msgspec.convert(row, Row)
            for row in (
                {**start, "args": {"path": 7}},
                {**end, "args": {}, "result_preview": "TOOL_ARG_ERROR: path must be a string"},
            )
        ),
        frozenset({ROOT}),
    )
    [call] = refused.steps()
    assert call.sent == Reported('{"path":7}')
    assert call.received == Reported("TOOL_ARG_ERROR: path must be a string")


def test_counts_keep_a_part_unknown_unless_every_one_reported_it():
    known = Ledger(100, 20, cached_tokens=60, cache_write_tokens=10, reasoning_tokens=5)
    silent = Ledger(50, 10)
    assert Counts((known,)).usage() == Usage(100, 20, thinking=5, cache_read=60, cache_write=10)
    assert Counts((known, silent)).usage() == Usage(150, 30)
    assert Counts((known, Ledger(None, 3))).usage() == NoUsage()
    assert Counts(()).usage() == NoUsage()


def test_counts_refuse_parts_that_cannot_be_parts():
    # A provider counting thinking apart from the output, or cache apart from the input.
    usage = Counts((Ledger(10, 5, cached_tokens=40, reasoning_tokens=9),)).usage()
    assert usage == Usage(
        10, 5, thinking=NotReported(), cache_read=NotReported(), cache_write=NotReported()
    )


def test_round_rows_enter_as_their_own_type():
    assert isinstance(msgspec.convert(ROUND, Row), Round)
