# ruff: noqa: RUF001
import json
import socket
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from companion.contracts import DialogueResponse
from companion.runtime import share_blind, share_chat_web
from companion.runtime.share_blind import BlindBatch
from companion.runtime.share_blind_web import StartRequest, TurnRequest, VoteRequest
from companion.runtime.share_chat_web import ChatPage, ChatRequest, ModeRequest
from companion.runtime.share_web import LoopbackServer
from companion.runtime.speech_dialogue import BlindTicket, SpeechDialogueBridgeError


def _reply(text):
    return DialogueResponse(
        text=text, disclosed_ai_identity=False, billable=True, blocked_reason=None,
    )


def _ticket(original="原稿", layered="分层稿"):
    return BlindTicket((_reply(original), _reply(layered)), "synthetic-user", "合成提问", ())


def _page(tmp_path, *, reply=None, calibration_factory=None):
    commits = []

    def default_commit(ticket, choice):
        commits.append((ticket, choice))

    page = ChatPage(
        reply=reply or (lambda text: _reply(f"回复：{text}")),
        batch_factory=lambda: BlindBatch(tmp_path / "results"),
        pair_factory=lambda text: _ticket(),
        commit_reply=default_commit,
        calibration_factory=calibration_factory,
        character_name="合成角色",
        model_name="合成模型",
    )
    return page, commits


def test_normal_chat_appends_messages_and_reports_failure(tmp_path):
    page, _ = _page(tmp_path)
    status, body = page.chat(ChatRequest(text="你好"))
    assert status == 200
    assert [item["role"] for item in body["messages"]] == ["you", "assistant"]
    assert body["messages"][1]["text"] == "回复：你好"
    assert body["character"] == "合成角色" and body["model"] == "合成模型"

    def broken(text):
        raise SpeechDialogueBridgeError("synthetic")

    broken_page, _ = _page(tmp_path, reply=broken)
    status, body = broken_page.chat(ChatRequest(text="这条会失败"))
    assert status == 502
    assert [item["role"] for item in body["messages"]] == ["you"]
    assert body["message"]


def test_blind_mode_round_and_finish(tmp_path, monkeypatch):
    monkeypatch.setattr(share_blind.secrets, "randbelow", lambda _: 0)
    page, commits = _page(tmp_path)

    status, body = page.set_mode(ModeRequest(mode="blind"))
    assert status == 200 and body["mode"] == "blind" and body["blind"]["phase"] == "consent"

    status, body = page.blind_start(StartRequest(consent="同意"))
    assert status == 200 and body["blind"]["phase"] == "ready"

    status, body = page.blind_turn(TurnRequest(text="合成提问"))
    assert status == 200 and body["blind"]["phase"] == "waiting"
    round_id = body["blind"]["round"]["id"]
    assert body["blind"]["round"]["text"] == "合成提问"

    status, body = page.blind_vote(VoteRequest(round_id=round_id, vote="A"))
    assert status == 200 and body["blind"]["phase"] == "ready"
    assert commits and commits[0][0].replies[commits[0][1]].text == "分层稿"

    status, body = page.set_mode(ModeRequest(mode="chat"))
    assert status == 409
    status, body = page.blind_finish()
    assert status == 200 and body["mode"] == "chat"
    assert page.blind.batch.path.is_file()
    assert json.loads(page.blind.batch.path.read_text(encoding="utf-8"))["decisive_votes"] == 1

    status, body = page.set_mode(ModeRequest(mode="chat"))
    assert status == 200


def test_mode_switch_is_blocked_while_a_round_is_pending(tmp_path):
    page, _ = _page(tmp_path)
    page.set_mode(ModeRequest(mode="blind"))
    page.blind_start(StartRequest(consent="同意"))
    page.blind_turn(TurnRequest(text="合成提问"))
    status, body = page.set_mode(ModeRequest(mode="chat"))
    assert status == 409
    assert "盲评" in body["message"]


class _FakeCalibration:
    url = "http://127.0.0.1:1/#token"

    def __init__(self):
        self._closed = False
        self.started = threading.Event()

    @property
    def closed(self):
        return self._closed

    def serve(self):
        self.started.set()

    def close(self):
        self._closed = True


def test_calibration_blocks_chat_until_page_closes(tmp_path):
    session = _FakeCalibration()
    page, _ = _page(tmp_path, calibration_factory=lambda: session)
    status, body = page.start_calibration()
    assert status == 200 and body["calibration"]["active"] is True
    assert body["url"] == session.url
    assert session.started.wait(timeout=1)

    status, body = page.chat(ChatRequest(text="校准期间不能聊天"))
    assert status == 409

    session._closed = True
    state = page.state()
    assert state["calibration"]["active"] is False
    assert "校准已结束" in state["message"]
    status, body = page.chat(ChatRequest(text="现在可以了"))
    assert status == 200


def test_finish_saves_an_unvoted_blind_round(tmp_path):
    page, _ = _page(tmp_path)
    page.set_mode(ModeRequest(mode="blind"))
    page.blind_start(StartRequest(consent="同意"))
    page.blind_turn(TurnRequest(text="合成提问"))
    status, body = page.finish()
    assert status == 200 and body["phase"] == "closed"
    assert page.blind.batch.path.is_file()
    assert json.loads(page.blind.batch.path.read_text(encoding="utf-8"))["skipped"] == 1
    assert page.messages == []


def test_loopback_server_chat_security_and_full_round(tmp_path, monkeypatch):
    monkeypatch.setattr(share_blind.secrets, "randbelow", lambda _: 0)
    page, commits = _page(tmp_path)
    assets = Path(share_chat_web.__file__).with_name("share_chat_page")
    with LoopbackServer(
        page,
        assets_dir=assets,
        static={
            "/": ("index.html", "text/html; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        },
        token_header="X-Chat-Token",
    ) as server:
        def serve():
            while not page.closed:
                server.handle_request()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)

        def connect_loopback(address, timeout, source_address=None):
            return socket.create_connection(address, timeout=timeout)

        connection._create_connection = connect_loopback
        origin = server.origin

        def request(method, path, data=None, **headers):
            sent = {"Origin": origin, "X-Chat-Token": server.token}
            sent.update(headers)
            body = None if data is None else json.dumps(data)
            if body is not None:
                sent["Content-Type"] = "application/json"
            connection.request(method, path, body, sent)
            response = connection.getresponse()
            assert response.getheader("Cache-Control") == "no-store"
            assert "frame-ancestors 'none'" in response.getheader("Content-Security-Policy")
            payload = response.read()
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = payload
            return response.status, parsed

        try:
            status, html = request("GET", "/")
            assert status == 200 and b"<html" in html
            status, _ = request("GET", "/", Host="example.test")
            assert status == 403
            status, _ = request("GET", "/missing.js")
            assert status == 404

            status, _ = request("POST", "/state", {}, Origin="https://example.test")
            assert status == 403
            status, _ = request("POST", "/state", {}, **{"X-Chat-Token": "wrong"})
            assert status == 403

            status, body = request("POST", "/state", {})
            assert status == 200 and body["phase"] == "chat"
            assert body["messages"] == []

            status, body = request("POST", "/chat", {"text": "合成提问"})
            assert status == 200
            assert [item["role"] for item in body["messages"]] == ["you", "assistant"]

            status, body = request("POST", "/mode", {"mode": "blind"})
            assert status == 200 and body["blind"]["phase"] == "consent"
            status, body = request("POST", "/blind/start", {"consent": "同意"})
            assert status == 200 and body["blind"]["phase"] == "ready"
            status, body = request("POST", "/blind/turn", {"text": "合成提问"})
            assert status == 200 and body["blind"]["phase"] == "waiting"
            round_id = body["blind"]["round"]["id"]
            assert "route" not in json.dumps(body)
            status, body = request(
                "POST", "/blind/vote", {"round_id": round_id, "vote": "A"},
            )
            assert status == 200 and body["blind"]["phase"] == "ready"
            assert len(commits) == 1
            status, body = request("POST", "/blind/finish", {})
            assert status == 200 and body["mode"] == "chat"

            status, body = request("POST", "/finish", {})
            assert status == 200 and body["phase"] == "closed"
        finally:
            connection.close()
            page.closed = True
            worker.join(timeout=3)

    assert not worker.is_alive()
    assert page.blind.batch.path.is_file()
    assert json.loads(page.blind.batch.path.read_text(encoding="utf-8"))["decisive_votes"] == 1


def test_chat_page_assets_are_self_contained():
    page = Path(share_chat_web.__file__).with_name("share_chat_page")
    html = (page / "index.html").read_text(encoding="utf-8")
    css = (page / "style.css").read_text(encoding="utf-8")
    js = (page / "app.js").read_text(encoding="utf-8")
    for text in (html, css, js):
        assert "http://" not in text
        assert "https://" not in text
    assert "/style.css" in html and "/app.js" in html


def test_web_keeps_routes_hidden_until_export_and_reports_saved_path(tmp_path):
    page, _ = _page(tmp_path)
    page.blind_start(StartRequest(consent="同意"))
    _, body = page.blind_turn(TurnRequest(text="合成提问"))
    round_id = body["blind"]["round"]["id"]
    _, body = page.blind_vote(VoteRequest(round_id=round_id, vote="A"))
    assert "layered_wins" not in body["blind"]["summary"]
    assert "original_wins" not in page.handle("/state", {})[1]["blind"]["summary"]
    status, body = page.finish()
    assert status == 200
    path = page.blind.batch.path
    assert str(path) in body["message"]
    assert str(path) in page.finish()[1]["message"]
    assert "layered_wins" in body["blind"]["summary"]


def test_web_fact_command_without_service_is_not_sent(tmp_path):
    sent = []
    page, _ = _page(tmp_path, reply=lambda text: sent.append(text) or _reply("好"))
    for prefix in ("记住", "请记住", "帮我记住"):
        page.chat(ChatRequest(text=prefix + "这件事：合成事实"))
    assert sent == []


@pytest.mark.parametrize("variant", ["chat", "blind"])
def test_finish_failure_stays_blind_and_retries_single_report(tmp_path, monkeypatch, variant):
    if variant == "chat":
        page, _ = _page(tmp_path)
        start, turn = "/blind/start", "/blind/turn"
    else:
        from companion.runtime.share_blind_web import BlindPage

        page = BlindPage(
            batch_factory=lambda: BlindBatch(tmp_path / "results"),
            pair_factory=lambda _: _ticket(), commit_reply=lambda *_: None,
        )
        start, turn = "/start", "/turn"
    assert page.handle(start, {"consent": "同意"})[0] == 200
    assert page.handle(turn, {"text": "合成提问"})[0] == 200
    batch = page.blind.batch if variant == "chat" else page.batch
    save = batch.save

    def fail():
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(batch, "save", fail)
    status, body = page.handle("/finish", {})
    assert status == 503 and "未保存" in body["message"] and not page.closed
    assert "layered_wins" not in json.dumps(body)
    assert "original_wins" not in json.dumps(page.handle("/state", {})[1])
    monkeypatch.setattr(batch, "save", save)
    status, body = page.handle("/finish", {})
    assert status == 200 and body["phase"] == "closed"
    assert str(batch.path) in body["message"] == page.handle("/finish", {})[1]["message"]
    assert list(batch.path.parent.glob("*.json")) == [batch.path]
    payload = batch.path.read_text(encoding="utf-8")
    assert str(batch.path) not in payload and "合成提问" not in payload
    assert json.loads(payload)["skipped"] == 1
