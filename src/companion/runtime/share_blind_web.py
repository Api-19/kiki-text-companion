# ruff: noqa: RUF001
"""Blind comparison page state and its standalone runner."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from companion.contracts import DialogueResponse
from companion.dialogue.context import MAX_CURRENT_CHARS
from companion.runtime.share_blind import BLIND_NOTICE, BlindBatch, normalize_vote
from companion.runtime.share_facts import FactChoice, WebFactConfirmation, is_fact_command
from companion.runtime.share_web import LoopbackServer
from companion.runtime.speech_dialogue import SpeechDialogueBridgeError

Phase = Literal["consent", "ready", "waiting", "complete", "closed"]

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_VOTES = {"a", "b", "tie", "both_bad", "skip"}
_VOTE_MESSAGES = {
    "a": "已记录：选 A，并沿用 A。",
    "b": "已记录：选 B，并沿用 B。",
    "tie": "已记录：差不多，沿用 A。",
    "both_bad": "已记录：都不好，沿用 A。",
    "skip": "已跳过，沿用 A。",
}
_SUMMARY_KEYS = (
    "total_rounds", "comparable_rounds", "decisive_votes", "tie", "both_bad", "skipped", "failed",
)


class BlindTicketLike(Protocol):
    replies: tuple[DialogueResponse, DialogueResponse]


class StartRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    consent: str = Field(max_length=16)


class TurnRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_CURRENT_CHARS)


class VoteRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    round_id: str = Field(min_length=1, max_length=64)
    vote: str = Field(min_length=1, max_length=16)


@dataclass(frozen=True)
class _PendingRound:
    round_id: str
    ticket: BlindTicketLike
    text: str


def _display_text(text: str) -> str:
    return "\n".join("".join(c for c in line if c.isprintable()) for line in text.splitlines())


class BlindPage:
    """State machine behind the blind comparison page."""

    def __init__(
        self,
        *,
        batch_factory: Callable[[], BlindBatch],
        pair_factory: Callable[[str], BlindTicketLike],
        commit_reply: Callable[[BlindTicketLike, int], None],
        notice: str = BLIND_NOTICE,
        facts: WebFactConfirmation | None = None,
    ) -> None:
        self.batch_factory = batch_factory
        self.pair_factory = pair_factory
        self.commit_reply = commit_reply
        self.notice = notice
        self.batch: BlindBatch | None = None
        self.phase: Phase = "consent"
        self.pending: _PendingRound | None = None
        self.message = ""
        self.closed = False
        self.facts = facts
        self.saved_path: Path | None = None

    def state(self) -> dict:
        state: dict = {"phase": self.phase, "summary": self._summary()}
        if self.phase == "consent":
            state["notice"] = self.notice
        if self.pending is not None:
            state["round"] = self._round_view()
        if self.phase == "ready" and self.facts is not None:
            state["fact"] = self.facts.state()
        if self.message:
            state["message"] = self.message
        return state

    def _summary(self) -> dict | None:
        if self.batch is None:
            return None
        payload = self.batch.payload()
        keys = _SUMMARY_KEYS
        if self.saved_path is not None:
            keys += ("layered_wins", "original_wins")
        return {key: payload[key] for key in keys}

    def _round_view(self) -> dict | None:
        if self.pending is None or self.batch is None:
            return None
        presented = self.batch.pending_round
        if presented is None:
            return None
        replies = self.pending.ticket.replies
        return {
            "id": presented.round_id,
            "text": _display_text(self.pending.text),
            "replies": [
                {"label": label, "text": _display_text(replies[index].text)}
                for label, index in zip(("A", "B"), presented.order, strict=True)
            ],
        }

    def handle(self, path: str, data: object) -> tuple[int, dict]:
        if path == "/state":
            if data != {}:
                return 400, {"message": "请求无效。"}
            return 200, self.state()
        if path == "/start":
            return self.start(StartRequest.model_validate(data))
        if path == "/turn":
            return self.turn(TurnRequest.model_validate(data))
        if path == "/fact":
            return self.fact_choice(FactChoice.model_validate(data))
        if path == "/vote":
            return self.vote(VoteRequest.model_validate(data))
        if path in {"/feedback", "/finish"}:
            if data != {}:
                return 400, {"message": "请求无效。"}
            if path == "/feedback":
                return self.feedback()
            status, body = self.finish()
            if status != 200:
                print(body["message"], flush=True)
            return status, body
        return 404, {"message": "未知请求。"}

    def start(self, data: StartRequest) -> tuple[int, dict]:
        if self.phase != "consent":
            return 409, {"message": "本轮盲评已经开始。"}
        if data.consent != "同意":
            return 400, {"message": "需要输入「同意」才能开始。"}
        try:
            self.batch = self.batch_factory()
        except OSError:
            return 503, {"message": "暂时无法创建本轮盲评。"}
        self.phase = "ready"
        self.message = "已开始。输入一句话生成两条回复。"
        return 200, self.state()

    def turn(self, data: TurnRequest) -> tuple[int, dict]:
        if self.phase != "ready" or self.batch is None:
            return 409, {"message": "当前不能生成新一轮，请先完成本轮投票。"}
        if is_fact_command(data.text):
            if self.facts is None:
                return 409, {"message": "本机记忆确认暂不可用。"}
            status, self.message = self.facts.start(data.text)
            return status, self.state()
        if self.facts is not None and self.facts.state() is not None:
            return 409, {"message": "请先确认或跳过当前事实。"}
        try:
            ticket = self.pair_factory(data.text)
            presented = self.batch.present(ticket.replies)
        except SpeechDialogueBridgeError:
            self.batch.record_failure()
            self.message = "这轮未能生成可比较的两条回复，未计票。可以继续下一题。"
            return 502, self.state()
        except ValueError:
            return 409, {"message": "当前不能生成新一轮，请查看当前题目。"}
        self.pending = _PendingRound(presented.round_id, ticket, data.text)
        self.phase = "waiting"
        self.message = "请比较两条回复并选择。"
        return 200, self.state()

    def fact_choice(self, data: FactChoice) -> tuple[int, dict]:
        if self.phase != "ready" or self.facts is None:
            return 409, {"message": "请先处理当前盲评投票。"}
        try:
            status, self.message = self.facts.choose(data)
        except SpeechDialogueBridgeError:
            status, self.message = 503, "记忆未确认保存，可重试或跳过。"
        return status, self.state()

    def vote(self, data: VoteRequest) -> tuple[int, dict]:
        if self.phase != "waiting" or self.pending is None or self.batch is None:
            return 409, {"message": "当前没有待投的回复。"}
        if data.round_id != self.pending.round_id:
            return 409, {"message": "这轮已经处理，请查看当前题目。"}
        code = normalize_vote(data.vote)
        if code not in _VOTES:
            return 400, {"message": "请选择列出的选项。"}
        ticket = self.pending.ticket
        choice = self.batch.vote(data.round_id, code)
        self.pending = None
        self.phase = "ready"
        self.message = _VOTE_MESSAGES[code]
        if choice is not None:
            try:
                self.commit_reply(ticket, choice)
            except SpeechDialogueBridgeError:
                self.message = "已记录投票，但本轮回复未写入聊天上下文。"
                return 502, self.state()
        return 200, self.state()

    def feedback(self) -> tuple[int, dict]:
        if self.closed or self.phase == "closed":
            return 409, {"message": "盲评已经结束。"}
        if self.batch is None:
            return 409, {"message": "本轮盲评还没有开始。"}
        if self.pending is not None:
            return 409, {"message": "请先完成本轮投票，再保存汇总。"}
        if self.facts is not None and self.facts.state() is not None:
            return 409, {"message": "请先确认或跳过当前事实。"}
        try:
            path = self.batch.save()
        except (OSError, ValueError):
            return 503, {"message": "汇总未保存。请检查结果目录权限和可用空间。"}
        self.saved_path = path
        self.phase = "complete"
        self.message = f"匿名汇总已保存：{path}"
        return 200, {**self.state(), "file": str(path)}

    def finish(self) -> tuple[int, dict]:
        if self.phase == "closed":
            return 200, self.state()
        if self.batch is not None:
            if self.pending is not None:
                with suppress(ValueError):
                    self.batch.vote(self.pending.round_id, "skip")
                self.pending = None
                self.phase = "ready"
            try:
                self.saved_path = self.batch.save()
            except (OSError, ValueError):
                self.message = "汇总未保存。请检查结果目录权限和可用空间，再试一次。"
                return 503, self.state()
        if self.facts is not None:
            self.facts.clear()
        self.phase = "closed"
        self.closed = True
        self.message = (
            f"匿名汇总已保存：{self.saved_path}" if self.saved_path else "已结束盲评。"
        )
        return 200, self.state()


def run_web_blind(
    *,
    batch_factory: Callable[[], BlindBatch],
    pair_factory: Callable[[str], BlindTicketLike],
    commit_reply: Callable[[BlindTicketLike, int], None],
    auto_open: bool = False,
    facts: WebFactConfirmation | None = None,
) -> None:
    page = BlindPage(
        batch_factory=batch_factory, pair_factory=pair_factory, commit_reply=commit_reply,
        facts=facts,
    )
    assets = Path(__file__).with_name("share_blind_page")
    try:
        with LoopbackServer(
            page, assets_dir=assets, static=_STATIC, token_header="X-Blind-Token",
        ) as server:
            url = f"{server.origin}/#{server.token}"
            print("请在本机浏览器打开下面的地址。关闭页面或按 Ctrl+C 结束盲评。")
            print(url, flush=True)
            if auto_open:
                with suppress(OSError, webbrowser.Error):
                    webbrowser.open(url)
            while not page.closed:
                server.handle_request()
    except KeyboardInterrupt:
        print("已结束网页盲评。")
    except OSError:
        print("本机盲评网页暂时无法打开。")
    finally:
        if page.batch is not None:
            status, body = page.finish()
            if status != 200:
                print(body.get("message", "汇总未保存。"))
            elif body.get("message"):
                print(body["message"])
    print("已返回聊天。")
