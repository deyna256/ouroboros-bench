"""Ouroboros as a convy agent: one fresh server per conversation, its Main chat as the dialogue."""

import asyncio
import base64
import contextlib
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
FILE_TEXT = 20_000  # characters of a delivered text file the answer carries
# Notices after which a task goes no further without its owner: it failed or never started,
# it is paused, or its model waits for quota or a sign-in. None of them ends with task_done.
STALLS = frozenset(("task_error", "task_not_started", "task_pause_notice", "task_model_wait"))


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
    task_id: str | None = None
    is_progress: bool = False
    system_type: str | None = None
    client_message_id: str | None = None
    ingress_accepted: bool = False

    def said(self) -> str:
        return self.content.strip()


class Option(Struct, frozen=True, kw_only=True):
    label: str


class QuizFrame(Struct, frozen=True, kw_only=True, tag_field="type", tag="quiz"):
    """A question Ouroboros asks its owner; with `wait_for_answer` its task waits for the answer."""

    quiz_id: str
    question: str
    chat_id: int | None = None
    task_id: str | None = None
    options: tuple[Option, ...] = ()
    wait_for_answer: bool = False

    def said(self) -> str:
        options = (f"{n}. {option.label}" for n, option in enumerate(self.options, 1))
        return "\n".join((self.question, *options))


class DocumentFrame(Struct, frozen=True, kw_only=True, tag_field="type", tag="document"):
    """A file Ouroboros sent, its bytes inline."""

    filename: str
    mime: str = ""
    file_base64: str = ""
    caption: str = ""
    chat_id: int | None = None
    task_id: str | None = None

    def said(self) -> str:
        lines = [f"[file {self.filename}]", self.caption.strip()]
        if self.mime.startswith("text/") or self.mime.endswith(("json", "xml", "yaml", "x-sh")):
            text = base64.b64decode(self.file_base64).decode(errors="replace")
            cut = "\n[cut]" if len(text) > FILE_TEXT else ""
            lines.append(f"```\n{text[:FILE_TEXT]}{cut}\n```")
        return "\n".join(line for line in lines if line)


class PhotoFrame(Struct, frozen=True, kw_only=True, tag_field="type", tag="photo"):
    """An image Ouroboros sent: the answer carries its caption, not its pixels."""

    mime: str = ""
    caption: str = ""
    chat_id: int | None = None
    task_id: str | None = None

    def said(self) -> str:
        return f"[image {self.mime}] {self.caption.strip()}".strip()


class Link(Struct, frozen=True, kw_only=True):
    label: str
    url: str


class LinksFrame(Struct, frozen=True, kw_only=True, tag_field="type", tag="links"):
    """Link buttons Ouroboros showed."""

    actions: tuple[Link, ...] = ()
    title: str = ""
    chat_id: int | None = None
    task_id: str | None = None

    def said(self) -> str:
        return "\n".join((self.title, *(f"{link.label}: {link.url}" for link in self.actions)))


class CardFrame(Struct, frozen=True, kw_only=True, tag_field="type", tag="card"):
    """A living card; its plain version is what the answer carries."""

    fallback_text: str = ""
    chat_id: int | None = None
    task_id: str | None = None

    def said(self) -> str:
        return self.fallback_text.strip()


Frame = LogFrame | ChatFrame | QuizFrame | DocumentFrame | PhotoFrame | LinksFrame | CardFrame
FRAMES = frozenset(("log", "chat", "quiz", "document", "photo", "links", "card"))
Content = QuizFrame | DocumentFrame | PhotoFrame | LinksFrame | CardFrame


class Turn:
    """One answer being collected from the socket: Ouroboros has answered when the message was
    accepted, it said or asked something, and every task the message started has ended. Work a
    turn hands off starts before the turn's own task ends, so it is waited for too; `settle`
    seconds of quiet catch the frames that trail a task's end. Frames before the acceptance, in
    other chats or from tasks the turn did not start belong to something else."""

    def __init__(
        self, message_id: str, *, settle: float, now: float, resumed: str | None = None
    ) -> None:
        self.message_id = message_id
        self.accepted = resumed is not None  # an answer to a question needs no echo
        self.settle = settle
        self.last = now
        self.replies: list[str] = []
        self.notices: list[str] = []
        self.question: QuizFrame | None = None
        self.started: set[str] = {resumed} if resumed else set()
        self.ended: dict[str, str] = {}
        self.roots: set[str] = set()

    def read(self, frame: Frame, now: float) -> None:
        self.last = now
        match frame:
            case ChatFrame(role="user", client_message_id=self.message_id, ingress_accepted=True):
                self.accepted = True
            case ChatFrame(role="system", client_message_id=self.message_id) if not self.accepted:
                raise AgentFailure(f"Ouroboros did not accept the message: {frame.said()}")
            case _ if not self.accepted:
                pass
            case ChatFrame(system_type=str(kind)) if kind in STALLS:
                raise AgentFailure(f"Ouroboros stopped ({kind}): {frame.said()}")
            case LogFrame(data=LogData(type="task_received" | "task_started", task_id=str(task))):
                self.started.add(task)
            case LogFrame(data=LogData(type="task_done", task_id=str(task))):
                self.started.add(task)
                self.ended[task] = frame.data.status or ""
                if frame.data.root_task_id in (None, "", task):
                    self.roots.add(task)
            case ChatFrame(role="assistant", is_progress=False) if (
                self.ours(frame) and frame.said()
            ):
                (self.notices if frame.system_type else self.replies).append(frame.said())
            case ChatFrame() | LogFrame():
                pass  # progress, other rows and runtime events say nothing to the user
            case _:
                self.content(frame)

    def content(self, frame: Content) -> None:
        if self.ours(frame) and frame.said():
            self.replies.append(frame.said())
            match frame:
                case QuizFrame(wait_for_answer=True):
                    self.question = frame

    def ours(self, frame: ChatFrame | Content) -> bool:
        """In Main, and from a task this turn started, or from no task at all."""
        return frame.chat_id in (None, MAIN) and (
            not frame.task_id or frame.task_id in self.started
        )

    def done(self, now: float) -> bool:
        if not self.accepted or now - self.last < self.settle:
            return False
        if self.question is not None:
            return True  # its task waits for the answer, the next message
        if not self.ended or self.started - self.ended.keys():
            return False
        if not self.replies:
            raise AgentFailure(
                "Ouroboros ended without an answer: "
                + "; ".join(
                    self.notices or [f"{task} {status}" for task, status in self.ended.items()]
                )
            )
        return True

    def text(self) -> str:
        return "\n\n".join(self.replies)


class Chat:
    """A dialogue in Main of one Ouroboros server. `listen` reads the socket for the whole
    dialogue, between answers too: a socket nobody reads stops answering pings and is closed."""

    def __init__(self, http: httpx2.AsyncClient, socket: Any, settle: float) -> None:
        self.http = http
        self.socket = socket
        self.settle = settle
        self.frames: asyncio.Queue[Any] = asyncio.Queue()
        self.question: QuizFrame | None = None

    async def listen(self) -> None:
        try:
            while True:
                self.frames.put_nowait(await self.socket.receive_json())
        except Exception as error:  # the socket closed or broke: the next read raises it
            self.frames.put_nowait(error)

    async def answer(self, message: Message) -> Answer:
        loop = asyncio.get_running_loop()
        message_id = uuid.uuid4().hex
        question, self.question = self.question, None
        while not self.frames.empty():  # what trailed the last answer: its tasks have ended
            self.frame(self.frames.get_nowait())
        turn = Turn(
            message_id,
            settle=self.settle,
            now=loop.time(),
            resumed=question.task_id if question else None,
        )
        if question is None:
            await self.socket.send_json(
                {
                    "type": "chat",
                    "content": message.text,
                    "client_message_id": message_id,
                    "chat_id": MAIN,
                }
            )
        else:  # Ouroboros asked and waits: the message is the answer, in the user's own words
            response = await self.http.post(
                "/api/decisions",
                json={
                    "decision_id": f"quiz:{question.task_id}:{question.quiz_id}",
                    "request_id": message_id,
                    "comment": message.text,
                },
            )
            response.raise_for_status()
        while not turn.done(loop.time()):
            try:
                raw = await asyncio.wait_for(self.frames.get(), self.settle)
            except TimeoutError:
                continue
            if (frame := self.frame(raw)) is not None:
                turn.read(frame, loop.time())
        self.question = turn.question
        trace = await self.trace(frozenset(turn.roots))
        return Answer(
            turn.text(), await self.usage(frozenset(turn.ended) | trace.tasks()), trace.steps()
        )

    def frame(self, raw: Any) -> Frame | None:
        """A frame of the kinds a turn reads, or None; a broken socket raises."""
        match raw:
            case Exception():
                raise AgentFailure(f"the socket to Ouroboros closed: {raw!r}")
            case {"type": str(kind)} if kind in FRAMES:
                try:
                    return msgspec.convert(raw, Frame)
                except msgspec.ValidationError:
                    return None  # a frame of a shape this version does not know
        return None

    async def trace(self, roots: frozenset[str]) -> Trace:
        rows: list[Row] = []
        for task in sorted(roots):
            async with self.http.sse(
                f"/api/tasks/{task}/events", method="POST", json={"v": 2, "wait": 0}
            ) as events:
                async for event in events:
                    match event.json():
                        case {"data": {"type": str(kind)} as data} if kind in KINDS:
                            # a row of a shape this version does not know is skipped
                            with contextlib.suppress(msgspec.ValidationError):
                                rows.append(msgspec.convert(data, Row))
        return Trace(tuple(rows), roots)

    async def usage(self, tasks: frozenset[str]) -> Usage | NoUsage:
        """The tokens of every task the turn ran, its helpers included, by their usage ledgers.
        A task the server does not know never ran, so spent nothing."""
        ledgers = []
        for task in sorted(tasks):
            response = await self.http.get(f"/api/tasks/{task}")
            if response.status_code != 404:
                ledgers.append(msgspec.convert(response.raise_for_status().json(), Ledger))
        return Counts(tuple(ledgers)).usage()


class Ouroboros(Struct, frozen=True):
    """Ouroboros, talked to in its Main chat over the socket its own interface uses. Each
    conversation gets a server of its own (see `Server`), started when it opens: about twenty
    seconds, which count against convy's `--turn-timeout` but not in any answer's time."""

    server: Server
    settle: float = 0.5

    @asynccontextmanager
    async def conversation(self) -> AsyncIterator[Chat]:
        async with (
            self.server.started() as url,
            httpx2.AsyncClient(base_url=url, timeout=60) as http,
        ):
            try:
                async with http.websocket(
                    url.replace("http", "ws", 1) + "/ws",
                    max_message_size_bytes=64 * 2**20,  # files come inline, base64
                ) as socket:
                    chat = Chat(http, socket, self.settle)
                    listening = asyncio.create_task(chat.listen())
                    try:
                        yield chat
                    finally:
                        listening.cancel()
            except ExceptionGroup as group:  # the socket's task group wraps what the turn raised
                while isinstance(group, ExceptionGroup) and len(group.exceptions) == 1:
                    group = group.exceptions[0]
                raise group from None
