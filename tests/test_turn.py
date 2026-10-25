"""When a turn is answered, from socket frames a real Ouroboros server sent for one message."""

import json
import pathlib

import msgspec
import pytest
from convy import AgentFailure

from ouroboros_convy.agent import FRAMES, Frame, Turn

FRAMES_SEEN = [
    msgspec.convert(frame, Frame)
    for frame in json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "turn_frames.json").read_text()
    )
    if frame["type"] in FRAMES
]
MESSAGE = "997ffaf9d8374db880c9ed3774f67cf8"


def turn(frames, *, accepted=False, now=0.0) -> Turn:
    collected = Turn(MESSAGE, accepted=accepted, settle=0.5, now=now)
    for frame in frames:
        collected.read(frame, now)
    return collected


def index_of(kind: str) -> int:
    return next(
        n
        for n, frame in enumerate(FRAMES_SEEN)
        if getattr(getattr(frame, "data", None), "type", None) == kind
    )


def test_the_recorded_turn_ends_at_its_task_end_after_the_quiet_settles():
    done = turn(FRAMES_SEEN[: index_of("task_done") + 1])
    assert not done.done(0.2)  # frames may still trail the task's end
    assert done.done(0.6)
    assert done.text() == "Here is my answer: 42."
    assert done.roots == {"18bb3252"}


def test_the_reply_alone_does_not_end_the_turn_while_its_task_runs():
    reply = next(
        n
        for n, frame in enumerate(FRAMES_SEEN)
        if getattr(frame, "role", "") == "assistant" and not getattr(frame, "is_progress", True)
    )
    assert not turn(FRAMES_SEEN[: reply + 1]).done(10)


def test_nothing_is_answered_before_the_message_was_accepted():
    assert not turn(FRAMES_SEEN[1:]).done(10)  # the first frame is the accepting echo


def chat(**fields):
    return msgspec.convert({"type": "chat", **fields}, Frame)


def log(**data):
    return msgspec.convert({"type": "log", "data": data}, Frame)


def test_a_refused_message_fails_the_turn():
    with pytest.raises(AgentFailure, match="not ready"):
        turn(
            [
                chat(
                    role="system",
                    client_message_id=MESSAGE,
                    content="not ready",
                    system_type="initialization_notice",
                )
            ]
        )


def test_a_task_that_ends_without_words_fails_the_turn_with_the_hosts_notice():
    frames = [
        chat(role="user", client_message_id=MESSAGE, ingress_accepted=True),
        log(type="task_received", task_id="t1"),
        chat(
            role="assistant",
            chat_id=1,
            content="Provider unavailable.",
            system_type="terminal_provider_notice",
        ),
        log(type="task_done", task_id="t1", root_task_id="t1", status="failed"),
    ]
    with pytest.raises(AgentFailure, match="Provider unavailable"):
        turn(frames).done(1)


def test_work_handed_off_in_the_turn_is_waited_for():
    frames = [
        chat(role="user", client_message_id=MESSAGE, ingress_accepted=True),
        log(type="task_received", task_id="direct"),
        log(type="task_received", task_id="work"),
        chat(role="assistant", chat_id=1, content="Started the work."),
        log(type="task_done", task_id="direct", root_task_id="direct", status="completed"),
    ]
    waiting = turn(frames)
    assert not waiting.done(1)
    waiting.read(chat(role="assistant", chat_id=1, content="The work is done."), 2)
    waiting.read(log(type="task_done", task_id="work", root_task_id="work", status="completed"), 2)
    assert waiting.done(3)
    assert waiting.text() == "Started the work.\n\nThe work is done."
    assert waiting.roots == {"direct", "work"}


def test_a_question_that_waits_for_the_answer_ends_the_turn_with_its_options():
    question = msgspec.convert(
        {
            "type": "quiz",
            "task_id": "t1",
            "quiz_id": "q1",
            "question": "Where should the backup go?",
            "options": [{"label": "/mnt/backup"}, {"label": "An S3 bucket"}],
            "wait_for_answer": True,
        },
        Frame,
    )
    asked = turn([log(type="task_received", task_id="t1"), question], accepted=True)
    assert asked.done(1)
    assert asked.question is question
    assert asked.text() == "Where should the backup go?\n1. /mnt/backup\n2. An S3 bucket"
