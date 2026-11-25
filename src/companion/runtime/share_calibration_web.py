# ruff: noqa: RUF001
"""One-page, loopback-only UI for the current share calibration session."""

from __future__ import annotations

import json
import secrets
import sys
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from companion.dialogue.provider import ProviderError
from companion.dialogue.usage import UsageLedgerError
from companion.memory.errors import MemoryAuthorizationError, MemoryServiceError
from companion.privacy.paired_snippets import ActiveSnippetReadError
from companion.runtime.share_calibration import (
    analyse_library,
    generate_questions,
    prepare_style_answer,
    question_failure_message,
)
from companion.runtime.share_facts import NOTE_CONSENT_TEXT, apply_fact_choice, prepare_note

if TYPE_CHECKING:
    from collections.abc import Callable

    from companion.dialogue.provider import DialogueProvider
    from companion.dialogue.usage import MonthlyUsageLedger
    from companion.runtime.share_calibration import Candidate, Question, ShareCalibration
    from companion.runtime.share_facts import ShareFacts

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_MAX_BODY = 8_192
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class AnswerRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    index: int = Field(ge=0, le=3)
    action: Literal["save", "skip", "uncertain", "decline"]
    answer: str = Field(default="", max_length=500)
    permission: Literal["allowed", "understand_only"] | None = None
    scope: Literal["durable", "temporary"] | None = None
    authorize: bool = False


class CalibrationPage:
    def __init__(
        self,
        *,
        candidates: list[Candidate],
        phase: str,
        calibration: ShareCalibration,
        provider_factory: Callable[[], DialogueProvider],
        ledger: MonthlyUsageLedger,
        facts: ShareFacts,
        user_id: str,
    ) -> None:
        self.candidates = candidates
        self.phase = phase
        self.calibration = calibration
        self.provider_factory = provider_factory
        self.ledger = ledger
        self.facts = facts
        self.user_id = user_id
        self.pending_fact: str | None = None
        self.questions: list[Question] = []
        self.uncertainty: str | None = None
        self.index = 0
        self.closed = False

    def state(self) -> dict:
        state = {"phase": self.phase, "count": len(self.candidates), "index": self.index}
        if self.uncertainty is not None:
            state["uncertainty"] = self.uncertainty
        if self.phase == "active":
            candidate = self.candidates[self.index]
            state["question"] = {
                **self.questions[self.index].model_dump(),
                "kind": candidate.kind,
                "context": candidate.context,
                "reply": candidate.reply,
                "term": candidate.term,
            }
        return state

    def generate(self, *, signals: dict | None = None) -> tuple[int, dict]:
        if self.phase != "ready":
            return 409, {"message": "本轮无法再次出题。"}
        self.phase = "failed"
        try:
            self.questions, self.uncertainty = generate_questions(
                self.candidates, self.provider_factory(), self.ledger, signals=signals,
            )
        except (ProviderError, UsageLedgerError, ValueError) as error:
            return 502, {"message": question_failure_message(error), **self.state()}
        self.phase = "active"
        return 200, self.state()

    def answer(self, data: AnswerRequest) -> tuple[int, dict]:
        if self.phase != "active" or data.index != self.index:
            return 409, {"message": "此题已经处理，请查看当前题目。"}
        if data.action == "decline":
            self.pending_fact = None
            return 200, {"message": "未授权，可选择仅本次或跳过。"}
        candidate = self.candidates[self.index]
        message = {"skip": "已跳过。", "uncertain": "已标记不确定，没有保存。"}
        if data.action == "save":
            try:
                feedback = self.save(candidate, data)
            except MemoryAuthorizationError:
                self.pending_fact = data.answer
                return 403, {
                    "code": "note_consent_required", "message": NOTE_CONSENT_TEXT,
                }
            except ValueError:
                return 400, {"message": "请检查回答、使用边界或保存范围。"}
            except (OSError, MemoryServiceError):
                return 503, {"message": "本题暂时无法保存。"}
        else:
            feedback = message[data.action]
        self.pending_fact = None
        self.index += 1
        if self.index == len(self.questions):
            self.phase = "complete"
        return 200, {"message": feedback, **self.state()}

    def save(self, candidate: Candidate, data: AnswerRequest) -> str:
        if candidate.kind != "fact":
            if data.scope is not None or data.authorize:
                raise ValueError("memory scope on style answer")
            entry = prepare_style_answer(
                candidate, self.questions[self.index], data.answer, data.permission,
            )
            self.calibration.save(entry)
            return "已保存风格。"

        if data.scope is None or data.permission is not None:
            raise ValueError("invalid fact scope")
        text, command = prepare_note(data.answer, self.user_id)
        if data.authorize:
            if data.scope != "durable" or data.answer != self.pending_fact:
                raise ValueError("no confirmed fact awaiting consent")
            self.facts.grant_notes()
        durable = apply_fact_choice(text, command, self.facts, data.scope)
        if durable is None:
            return "已记在本次聊天。"
        self.facts.save_durable(durable)
        return "已保存长期记忆。"


class _Server(HTTPServer):
    timeout = 0.25

    def __init__(self, page: CalibrationPage):
        assets = Path(__file__).with_name("share_calibration_page")
        self.assets = {
            route: ((assets / filename).read_bytes(), content_type)
            for route, (filename, content_type) in _STATIC.items()
        }
        self.page = page
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), _Handler)
        self.host_header = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.host_header}"

    def server_bind(self) -> None:
        # Binding stays on the numeric loopback address; no hostname is needed.
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = self.server_address

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2)
        return connection, address

    def handle_error(self, request, client_address):
        # Network disconnects are expected. Other handler failures close the session.
        if not isinstance(sys.exception(), OSError):
            self.page.closed = True
            print("本机校准网页发生错误。")


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format, *args):
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_error(self, code, message=None, explain=None) -> None:
        self._json(code, {"message": "校准请求无效。"}, write_body=self.command != "HEAD")

    def _send(self, status: int, payload: bytes, content_type: str, *, write_body=True) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if write_body:
            self.wfile.write(payload)

    def _json(self, status: int, body: dict, *, write_body=True) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8", write_body=write_body)

    def _host_ok(self) -> bool:
        return self.headers.get_all("Host") == [self.server.host_header]

    def do_GET(self) -> None:
        if not self._host_ok():
            self.send_error(403)
        elif self.path in self.server.assets:
            payload, content_type = self.server.assets[self.path]
            self._send(200, payload, content_type)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        tokens = self.headers.get_all("X-Calibration-Token", [])
        if (
            not self._host_ok()
            or self.headers.get_all("Origin") != [self.server.origin]
            or len(tokens) != 1 or not tokens[0].isascii()
            or not secrets.compare_digest(tokens[0], self.server.token)
        ):
            self.send_error(403)
            return

        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get_all("Content-Type") != ["application/json"]
            or "Transfer-Encoding" in self.headers
            or len(lengths) != 1
            or not lengths[0].isascii() or not lengths[0].isdecimal() or len(lengths[0]) > 4
            or not 0 < int(lengths[0]) <= _MAX_BODY
        ):
            self.send_error(400)
            return
        page = self.server.page
        try:
            data = json.loads(self.rfile.read(int(lengths[0])))
            if self.path == "/answer":
                status, body = page.answer(AnswerRequest.model_validate(data))
            elif data != {}:
                self.send_error(400)
                return
            elif self.path == "/state":
                status, body = 200, page.state()
            elif self.path == "/generate":
                status, body = page.generate()
            elif self.path == "/finish":
                page.closed = True
                status, body = 200, {"phase": "closed", "message": "已返回终端聊天。"}
            else:
                self.send_error(404)
                return
        except (ValueError, RecursionError):
            self.send_error(400)
            return
        self._json(status, body)


def run_web_calibration(
    *,
    library: Path,
    calibration: ShareCalibration,
    provider_factory: Callable[[], DialogueProvider],
    ledger: MonthlyUsageLedger,
    facts: ShareFacts,
    user_id: str,
    auto_generate: bool = False,
) -> None:
    try:
        candidates, signals = analyse_library(library)
        phase = "ready" if candidates else "empty"
    except ActiveSnippetReadError:
        candidates, signals = [], {}
        phase = "failed"
    page = CalibrationPage(
        candidates=candidates, phase=phase, calibration=calibration,
        provider_factory=provider_factory, ledger=ledger, facts=facts,
        user_id=user_id,
    )
    try:
        with _Server(page) as server:
            if auto_generate:
                if page.phase == "ready":
                    print("正在用已配置的模型分析匿名统计并出题。", flush=True)
                    _, result = page.generate(signals=signals)
                    if page.phase != "active":
                        print(result["message"])
                if page.phase != "active":
                    if page.phase == "empty":
                        print("没有找到适合校准的片段，可以继续聊天。")
                    else:
                        print("分析或出题暂未完成，可以继续聊天，稍后重新校准。")
                    return
            print("请在本机浏览器打开下面的地址。结束校准可返回聊天，也可在终端按 Ctrl+C。")
            url = f"{server.origin}/#{server.token}"
            print(url, flush=True)
            if auto_generate:
                import webbrowser

                with suppress(OSError, webbrowser.Error):
                    webbrowser.open(url)
            while not page.closed:
                server.handle_request()
    except KeyboardInterrupt:
        print("已结束网页校准。")
    except OSError:
        print("本机校准网页暂时无法打开。")
    print("已返回聊天。")
