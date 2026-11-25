# ruff: noqa: RUF001
"""Full local chat page with normal chat and optional blind comparison."""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from companion.contracts import DialogueResponse
from companion.dialogue.context import MAX_CURRENT_CHARS
from companion.runtime.share_blind import BlindBatch
from companion.runtime.share_blind_web import (
    BlindPage,
    BlindTicketLike,
    StartRequest,
    TurnRequest,
    VoteRequest,
)
from companion.runtime.share_web import LoopbackServer
from companion.runtime.speech_dialogue import SpeechDialogueBridgeError

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_MAX_MESSAGES = 40


class ChatRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_CURRENT_CHARS)


class ModeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    mode: Literal["chat", "blind"]


class CalibrationSession(Protocol):
    url: str

    @property
    def closed(self) -> bool: ...

    def serve(self) -> None: ...

    def close(self) -> None: ...


def _clean(text: str) -> str:
    return "\n".join("".join(c for c in line if c.isprintable()) for line in text.splitlines())


class ChatPage:
    """State machine behind the full local chat page."""

    def __init__(
        self,
        *,
        reply: Callable[[str], DialogueResponse],
        batch_factory: Callable[[], BlindBatch],
        pair_factory: Callable[[str], BlindTicketLike],
        commit_reply: Callable[[BlindTicketLike, int], None],
        calibration_factory: Callable[[], CalibrationSession] | None = None,
        character_name: str = "对方",
        model_name: str = "",
    ) -> None:
        self.reply = reply
        self.batch_factory = batch_factory
        self.pair_factory = pair_factory
        self.commit_reply = commit_reply
        self.calibration_factory = calibration_factory
        self.character_name = character_name
        self.model_name = model_name
        self.messages: list[dict[str, str]] = []
        self.mode: Literal["chat", "blind"] = "chat"
        self.blind: BlindPage | None = None
        self.calibration: CalibrationSession | None = None
        self.calibration_worker: threading.Thread | None = None
        self.message = ""
        self.closed = False

    def state(self) -> dict:
        self._poll_calibration()
        result: dict = {
            "phase": "closed" if self.closed else self.mode,
            "mode": self.mode,
            "character": self.character_name,
            "model": self.model_name,
            "messages": self.messages,
            "blind": self.blind.state() if self.blind is not None else None,
            "calibration": {
                "active": self.calibration is not None,
                "url": self.calibration.url if self.calibration is not None else None,
            },
        }
        if self.message:
            result["message"] = self.message
        return result

    def _new_blind(self) -> BlindPage:
        return BlindPage(
            batch_factory=self.batch_factory,
            pair_factory=self.pair_factory,
            commit_reply=self.commit_reply,
        )

    def _ensure_blind(self) -> BlindPage:
        if self.blind is None or self.blind.phase == "closed":
            self.blind = self._new_blind()
        return self.blind

    def _poll_calibration(self) -> None:
        session = self.calibration
        if session is None or not session.closed:
            return
        session.close()
        self.calibration = None
        if self.calibration_worker is not None:
            self.calibration_worker.join(timeout=0.5)
            self.calibration_worker = None
        self.message = "校准已结束，可以继续聊天。"

    def _busy_with_calibration(self) -> bool:
        self._poll_calibration()
        return self.calibration is not None

    def _append(self, role: str, text: str) -> None:
        self.messages.append({"role": role, "text": _clean(text)})
        self.messages = self.messages[-_MAX_MESSAGES:]

    def handle(self, path: str, data: object) -> tuple[int, dict]:
        if path == "/state":
            if data != {}:
                return 400, {"message": "请求无效。"}
            return 200, self.state()
        if path == "/chat":
            return self.chat(ChatRequest.model_validate(data))
        if path == "/mode":
            return self.set_mode(ModeRequest.model_validate(data))
        if path == "/blind/start":
            return self.blind_start(StartRequest.model_validate(data))
        if path == "/blind/turn":
            return self.blind_turn(TurnRequest.model_validate(data))
        if path == "/blind/vote":
            return self.blind_vote(VoteRequest.model_validate(data))
        if path in {"/blind/feedback", "/blind/finish", "/calibrate", "/finish"}:
            if data != {}:
                return 400, {"message": "请求无效。"}
            if path == "/blind/feedback":
                return self.blind_feedback()
            if path == "/blind/finish":
                return self.blind_finish()
            if path == "/calibrate":
                return self.start_calibration()
            return self.finish()
        return 404, {"message": "未知请求。"}

    def chat(self, data: ChatRequest) -> tuple[int, dict]:
        if self.closed:
            return 409, {"message": "聊天已经结束。"}
        if self.mode != "chat":
            return 409, {"message": "请先结束盲评模式。"}
        if self._busy_with_calibration():
            return 409, {"message": "校准页面还开着，请先完成或关闭它。"}
        self._append("you", data.text)
        try:
            response = self.reply(data.text)
        except SpeechDialogueBridgeError:
            self.message = "这次没能回复，换个说法再试吧。"
            return 502, self.state()
        self._append("assistant", response.text)
        self.message = ""
        return 200, self.state()

    def set_mode(self, data: ModeRequest) -> tuple[int, dict]:
        if self.closed:
            return 409, {"message": "聊天已经结束。"}
        if self._busy_with_calibration():
            return 409, {"message": "校准页面还开着，请先完成或关闭它。"}
        if data.mode == "blind":
            self._ensure_blind()
            self.mode = "blind"
            self.message = ""
            return 200, self.state()
        if self.blind is not None and self.blind.phase in {"ready", "waiting"}:
            return 409, {"message": "请先在盲评页结束并保存本轮，再回到普通聊天。"}
        self.mode = "chat"
        self.message = ""
        return 200, self.state()

    def blind_start(self, data: StartRequest) -> tuple[int, dict]:
        if self.closed:
            return 409, {"message": "聊天已经结束。"}
        if self._busy_with_calibration():
            return 409, {"message": "校准页面还开着，请先完成或关闭它。"}
        blind = self._ensure_blind()
        self.mode = "blind"
        status, body = blind.start(data)
        return status, self.state() if status != 404 else body

    def blind_turn(self, data: TurnRequest) -> tuple[int, dict]:
        if self.closed or self.mode != "blind" or self.blind is None:
            return 409, {"message": "当前不在盲评模式。"}
        if self._busy_with_calibration():
            return 409, {"message": "校准页面还开着，请先完成或关闭它。"}
        status, _ = self.blind.turn(data)
        return status, self.state()

    def blind_vote(self, data: VoteRequest) -> tuple[int, dict]:
        if self.closed or self.mode != "blind" or self.blind is None:
            return 409, {"message": "当前不在盲评模式。"}
        status, _ = self.blind.vote(data)
        return status, self.state()

    def blind_feedback(self) -> tuple[int, dict]:
        if self.blind is None:
            return 409, {"message": "本轮盲评还没有开始。"}
        status, _ = self.blind.feedback()
        return status, self.state()

    def blind_finish(self) -> tuple[int, dict]:
        if self.blind is None:
            self.mode = "chat"
            return 200, self.state()
        status, _ = self.blind.finish()
        if status == 200:
            self.mode = "chat"
            self.message = self.blind.message or "盲评已结束。"
        return status, self.state()

    def start_calibration(self) -> tuple[int, dict]:
        if self.closed or self.mode != "chat":
            return 409, {"message": "请先回到普通聊天再校准。"}
        if self._busy_with_calibration():
            return 409, {"message": "校准页面已经开着。"}
        if self.calibration_factory is None:
            return 409, {"message": "当前账户的本地风格校准不可用。"}
        try:
            session = self.calibration_factory()
        except (OSError, ValueError):
            return 503, {"message": "本机校准网页暂时无法打开。"}
        self.calibration = session
        self.calibration_worker = threading.Thread(target=session.serve, daemon=True)
        self.calibration_worker.start()
        self.message = "校准页面已打开。完成或关闭校准页后会回到聊天。"
        return 200, {**self.state(), "url": session.url}

    def _close_calibration(self) -> None:
        if self.calibration is None:
            return
        self.calibration.close()
        if self.calibration_worker is not None:
            self.calibration_worker.join(timeout=0.5)
        self.calibration = None
        self.calibration_worker = None

    def finish(self) -> tuple[int, dict]:
        if self.closed:
            return 200, self.state()
        self._close_calibration()
        if self.blind is not None:
            status, _ = self.blind.finish()
            if status != 200:
                return status, self.state()
        self.messages = []
        self.mode = "chat"
        self.closed = True
        self.message = "已结束聊天，可以关闭这个页面。"
        return 200, self.state()


def run_web_chat(
    *,
    reply: Callable[[str], DialogueResponse],
    batch_factory: Callable[[], BlindBatch],
    pair_factory: Callable[[str], BlindTicketLike],
    commit_reply: Callable[[BlindTicketLike, int], None],
    calibration_factory: Callable[[], CalibrationSession] | None = None,
    character_name: str = "对方",
    model_name: str = "",
    auto_open: bool = True,
) -> None:
    page = ChatPage(
        reply=reply, batch_factory=batch_factory, pair_factory=pair_factory,
        commit_reply=commit_reply, calibration_factory=calibration_factory,
        character_name=character_name, model_name=model_name,
    )
    assets = Path(__file__).with_name("share_chat_page")
    try:
        with LoopbackServer(
            page, assets_dir=assets, static=_STATIC, token_header="X-Chat-Token",
        ) as server:
            url = f"{server.origin}/#{server.token}"
            print("请在本机浏览器打开下面的地址。关闭页面或按 Ctrl+C 结束聊天。")
            print(url, flush=True)
            if auto_open:
                with suppress(OSError, webbrowser.Error):
                    webbrowser.open(url)
            while not page.closed:
                server.handle_request()
    except KeyboardInterrupt:
        print("已结束网页聊天。")
    except OSError:
        print("本机聊天网页暂时无法打开。")
    finally:
        page.finish()
    print("已返回终端。")
