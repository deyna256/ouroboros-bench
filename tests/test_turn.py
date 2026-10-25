"""When a turn is answered and what it says, from socket frames a real Ouroboros server sent for
one message, and from frames its client contract describes."""

import base64
import json
import pathlib

import msgspec
import pytest
from convy import AgentFailure

from ouroboros_convy.agent import FRAMES, Frame, Turn

RECORDED = [
    msgspec.convert(frame, Frame)
    for frame in json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "turn_frames.json").read_text()
    )
    if frame["type"] in FRAMES
]
MESSAGE = "997ffaf9d8374db880c9ed3774f67cf8"


def frame(kind: str, **fields) -> Frame:
    return msgspec.convert({"type": kind, **fields}, Frame)


def log(**data) -> Frame:
    return frame("log", data=data)


ACCEPTED = frame("chat", role="user", client_message_id=MESSAGE, ingress_accepted=True)


def turn(frames, *, now=0.0, resumed=None) -> Turn:
    collected = Turn(MESSAGE, settle=0.5, now=now, resumed=resumed)
    for each in frames:
        collected.read(each, now)
    return collected


def test_the_recorded_turn_ends_at_its_task_end_after_the_quiet_settles():
    end = next(
        n
        for n, each in enumerate(RECORDED)
        if getattr(each, "data", None) and each.data.type == "task_done"
    )
    done = turn(RECORDED[: end + 1])
    assert not done.done(0.2)  # frames may still trail the task's end
    assert done.done(0.6)
    assert done.text() == "Here is my answer: 42."
    assert done.roots == {"18bb3252"}


def test_the_reply_alone_does_not_end_the_turn_while_its_task_runs():
    reply = next(
        n
        for n, each in enumerate(RECORDED)
        if getattr(each, "role", "") == "assistant" and not getattr(each, "is_progress", True)
    )
    assert not turn(RECORDED[: reply + 1]).done(10)


def test_nothing_counts_before_the_message_was_accepted():
    waiting = turn(
        [
            frame("chat", role="assistant", chat_id=1, content="An old reply."),
            log(type="task_done", task_id="old", root_task_id="old", status="completed"),
        ]
    )
    assert not waiting.done(10)
    assert waiting.text() == "" and not waiting.ended


def test_a_refused_message_fails_the_turn():
    refusal = frame(
        "chat",
        role="system",
        client_message_id=MESSAGE,
        content="not ready",
        system_type="initialization_notice",
    )
    with pytest.raises(AgentFailure, match="not ready"):
        turn([refusal])


def test_a_task_that_ends_without_words_fails_the_turn_with_the_hosts_notice():
    frames = [
        ACCEPTED,
        log(type="task_received", task_id="t1"),
        frame(
            "chat",
            role="assistant",
            chat_id=1,
            task_id="t1",
            content="Provider unavailable.",
            system_type="terminal_provider_notice",
        ),
        log(type="task_done", task_id="t1", root_task_id="t1", status="failed"),
    ]
    with pytest.raises(AgentFailure, match="Provider unavailable"):
        turn(frames).done(1)


@pytest.mark.parametrize("kind", ["task_error", "task_not_started", "task_model_wait"])
def test_a_task_that_cannot_go_on_fails_the_turn_at_once(kind):
    # These tasks never send task_done: waiting for it would hold the turn until its timeout.
    stalled = frame(
        "chat", role="system", chat_id=1, task_id="t1", system_type=kind, content="Quota exhausted."
    )
    with pytest.raises(AgentFailure, match=f"{kind}.*Quota exhausted"):
        turn([ACCEPTED, log(type="task_received", task_id="t1"), stalled])


def test_work_handed_off_in_the_turn_is_waited_for():
    waiting = turn(
        [
            ACCEPTED,
            log(type="task_received", task_id="direct"),
            log(type="task_received", task_id="work"),
            frame(
                "chat", role="assistant", chat_id=1, task_id="direct", content="Started the work."
            ),
            log(type="task_done", task_id="direct", root_task_id="direct", status="completed"),
        ]
    )
    assert not waiting.done(1)
    waiting.read(frame("chat", role="assistant", chat_id=1, task_id="work", content="Done."), 2)
    waiting.read(log(type="task_done", task_id="work", root_task_id="work", status="completed"), 2)
    assert waiting.done(3)
    assert waiting.text() == "Started the work.\n\nDone."
    assert waiting.roots == {"direct", "work"}


def test_words_from_other_tasks_and_chats_are_not_the_answer():
    told = turn(
        [
            ACCEPTED,
            log(type="task_received", task_id="t1"),
            frame("chat", role="assistant", chat_id=1, task_id="scheduled", content="Backup ran."),
            frame("chat", role="assistant", chat_id=7, task_id="t1", content="In a project room."),
            frame("chat", role="assistant", chat_id=1, task_id="t1", content="Here it is."),
            log(type="task_done", task_id="t1", root_task_id="t1", status="completed"),
        ]
    )
    assert told.text() == "Here it is."


def test_a_delivered_file_image_and_links_are_part_of_the_answer():
    script = "#!/bin/sh\ntar czf /mnt/backup/photos.tgz ~/photos\n"
    told = turn(
        [
            ACCEPTED,
            log(type="task_received", task_id="t1"),
            frame(
                "document",
                task_id="t1",
                filename="backup.sh",
                mime="text/x-shellscript",
                file_base64=base64.b64encode(script.encode()).decode(),
                caption="The script",
            ),
            frame("photo", task_id="t1", mime="image/png", image_base64="", caption="A chart"),
            frame(
                "links",
                task_id="t1",
                title="Docs",
                actions=[{"label": "tar", "url": "https://x.y/tar"}],
            ),
            log(type="task_done", task_id="t1", root_task_id="t1", status="completed"),
        ]
    )
    assert told.done(1)
    assert told.text() == (
        f"[file backup.sh]\nThe script\n```\n{script}\n```\n\n[image image/png] A chart"
        "\n\nDocs\ntar: https://x.y/tar"
    )


def test_a_question_that_waits_for_the_answer_ends_the_turn_with_its_options():
    question = frame(
        "quiz",
        task_id="t1",
        quiz_id="q1",
        question="Where should the backup go?",
        options=[{"label": "/mnt/backup"}, {"label": "An S3 bucket"}],
        wait_for_answer=True,
    )
    asked = turn([ACCEPTED, log(type="task_received", task_id="t1"), question])
    assert asked.done(1)
    assert asked.question is question
    assert asked.text() == "Where should the backup go?\n1. /mnt/backup\n2. An S3 bucket"


def test_the_answer_to_a_question_waits_for_the_task_it_resumed():
    resumed = turn(
        [frame("chat", role="assistant", chat_id=1, task_id="t1", content="Done.")], resumed="t1"
    )
    assert not resumed.done(10)  # the task goes on after the answer, without a new start
    resumed.read(log(type="task_done", task_id="t1", root_task_id="t1", status="completed"), 10)
    assert resumed.done(11) and resumed.text() == "Done."
