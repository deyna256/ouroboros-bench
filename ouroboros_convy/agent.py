"""Ouroboros as a convy agent: one fresh server per conversation, its Main chat as the dialogue."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import msgspec
from convy import AgentFailure, Answer, Message, NoUsage, Usage
from msgspec import Struct

from ouroboros_convy.server import Server
from ouroboros_convy.trace import KINDS, Counts, Ledger, Row, Trace

MAIN = 1  # the chat id of Main, the owner's own room


class LogData(Struct, frozen=True, kw_only=True):
    """A runtime event, as a `log` frame carries it."""

    type: str = ""
    task_id: str | None = None
    root_task_id: str | None = None
    status: str | None = None


class LogFrame(Struct, frozen=True, tag_field="type", tag="log"):
    data: LogData


class ChatFrame(Struct, frozen=True, kw_only=True, tag_field="type", tag="chat"):
    """A chat row: the owner's message echoed back, a reply, a progress line or a host notice."""

    role: str = ""
    content: str = ""
    chat_id: int | None = None
    is_progress: bool = False
    system_type: str | None = None
    client_message_id: str | None = None
    ingress_accepted: bool = False


class Option(Struct, frozen=True, kw_only=True):
    label: str


class QuizFrame(Struct, frozen=True, kw_only=True, tag_field="type", tag="quiz"):
    """A question Ouroboros asks its owner; with `wait_for_answer` its task waits for the answer."""

    task_id: str
    quiz_id: str
    question: str
    options: tuple[Option, ...] = ()
    wait_for_answer: bool = False

    def text(self) -> str:
        lines = [
            self.question,
            *(f"{n}. {option.label}" for n, option in enumerate(self.options, 1)),
        ]
        return "\n".join(lines)


Frame = LogFrame | ChatFrame | QuizFrame
FRAMES = frozenset(("log", "chat", "quiz"))


class Turn:
    """One answer being collected from the socket: Ouroboros has answered when the message was
    accepted, it said something or asked something, and every task the message started has
    ended. Work a turn hands off starts before the turn's own task ends, so it is waited for
    too; `settle` seconds of quiet catch the frames that trail a task's end."""

    def __init__(self, message_id: str, *, accepted: bool, settle: float, now: float) -> None:
        self.message_id = message_id
        self.accepted = accepted
        self.settle = settle
        self.last = now
        self.replies: list[str] = []
        self.notices: list[str] = []
        self.question: QuizFrame | None = None
        self.started: set[str] = set()
        self.ended: dict[str, str] = {}
        self.roots: set[str] = set()

    def read(self, frame: Frame, now: float) -> None:
        self.last = now
        match frame:
            case ChatFrame(role="user", client_message_id=self.message_id, ingress_accepted=True):
                self.accepted = True
            case ChatFrame(role="system", client_message_id=self.message_id) if not self.accepted:
                raise AgentFailure(f"Ouroboros did not accept the message: {frame.content}")
            case ChatFrame(role="assistant", chat_id=1, is_progress=False) if (
                frame.content.strip()
            ):  # MAIN
                (self.notices if frame.system_type else self.replies).append(frame.content.strip())
            case QuizFrame():
                self.replies.append(frame.text())
                if frame.wait_for_answer:
                    self.question = frame
            case LogFrame(data=LogData(type="task_received" | "task_started", task_id=str(task))):
                self.started.add(task)
            case LogFrame(data=LogData(type="task_done", task_id=str(task))):
                self.started.add(task)
                self.ended[task] = frame.data.status or ""
                if frame.data.root_task_id in (None, "", task):
                    self.roots.add(task)
            case _:
                pass

    def done(self, now: float) -> bool:
        if not self.accepted or now - self.last < self.settle:
            return False
        if self.question is not None:
            return True  # its task waits for the answer, the next message
        if self.started - self.ended.keys():
            return False
        if not self.replies and self.ended:
            raise AgentFailure(
                "Ouroboros ended without an answer: "
                + "; ".join(
                    self.notices or [f"{task} {status}" for task, status in self.ended.items()]
                )
            )
        return bool(self.replies)

    def text(self) -> str:
        return "\n\n".join(self.replies)


class Chat:
    """A dialogue in Main of one Ouroboros server."""

    def __init__(self, http: httpx2.AsyncClient, socket: Any, settle: float) -> None:
        self.http = http
        self.socket = socket
        self.settle = settle
        self.question: QuizFrame | None = None

    async def answer(self, message: Message) -> Answer:
        loop = asyncio.get_running_loop()
        message_id = uuid.uuid4().hex
        turn = Turn(
            message_id, accepted=self.question is not None, settle=self.settle, now=loop.time()
        )
        if self.question is None:
            await self.socket.send_json(
                {
                    "type": "chat",
                    "content": message.text,
                    "client_message_id": message_id,
                    "chat_id": MAIN,
                }
            )
        else:  # Ouroboros asked and waits: the message is the answer, in the user's own words
            question, self.question = self.question, None
            response = await self.http.post(
                "/api/decisions",
                json={
                    "decision_id": f"quiz:{question.task_id}:{question.quiz_id}",
                    "request_id": message_id,
                    "comment": message.text,
                },
            )
            if response.status_code >= 400:
                raise AgentFailure(
                    f"Ouroboros refused the answer: {response.status_code} {response.text}"
                )
        while not turn.done(loop.time()):
            try:
                raw = await self.socket.receive_json(timeout=self.settle)
            except TimeoutError:
                continue
            if isinstance(raw, dict) and raw.get("type") in FRAMES:
                turn.read(msgspec.convert(raw, Frame), loop.time())
        self.question = turn.question
        trace = await self.trace(frozenset(turn.roots))
        tasks = frozenset(turn.ended) | trace.tasks()
        return Answer(turn.text(), await self.usage(tasks), trace.steps())

    async def trace(self, roots: frozenset[str]) -> Trace:
        rows: list[Row] = []
        for task in sorted(roots):
            async with self.http.sse(
                f"/api/tasks/{task}/events", method="POST", json={"v": 2, "wait": 0}
            ) as events:
                async for event in events:
                    envelope = event.json()
                    data = envelope.get("data") if isinstance(envelope, dict) else None
                    if isinstance(data, dict) and data.get("type") in KINDS:
                        rows.append(msgspec.convert(data, Row))
        return Trace(tuple(rows), roots)

    async def usage(self, tasks: frozenset[str]) -> Usage | NoUsage:
        """The tokens of every task the turn ran, its helpers included, by their usage ledgers."""
        ledgers = []
        for task in sorted(tasks):
            response = await self.http.get(f"/api/tasks/{task}")
            ledgers.append(msgspec.convert(response.raise_for_status().json(), Ledger))
        return Counts(tuple(ledgers)).usage() or NoUsage()


class Ouroboros(Struct, frozen=True):
    """Ouroboros, talked to in its Main chat over the socket its own interface uses. Each
    conversation gets a server of its own (see `Server`), started when it opens: about a minute,
    which counts against convy's `--turn-timeout` but not in any answer's time."""

    server: Server
    settle: float = 0.5

    @asynccontextmanager
    async def conversation(self) -> AsyncIterator[Chat]:
        async with (
            self.server.started() as url,
            httpx2.AsyncClient(base_url=url, timeout=60) as http,
        ):
            try:
                async with http.websocket(url.replace("http", "ws", 1) + "/ws") as socket:
                    yield Chat(http, socket, self.settle)
            except ExceptionGroup as group:  # the socket's task group wraps what the turn raised
                while isinstance(group, ExceptionGroup) and len(group.exceptions) == 1:
                    group = group.exceptions[0]
                raise group from None
