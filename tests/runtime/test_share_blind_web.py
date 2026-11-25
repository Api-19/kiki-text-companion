import json
import socket
import threading
from http.client import HTTPConnection
from pathlib import Path

from companion.contracts import DialogueResponse
from companion.runtime import share_blind
from companion.runtime.share_blind import BlindBatch
from companion.runtime.share_blind_web import (
    BlindPage,
    StartRequest,
    TurnRequest,
    VoteRequest,
)
from companion.runtime.share_web import LoopbackServer
from companion.runtime.speech_dialogue import BlindTicket, SpeechDialogueBridgeError


def _reply(text):
    return DialogueResponse(
        text=text, disclosed_ai_identity=False, billable=True, blocked_reason=None,
    )


def _ticket(original="原稿", layered="分层稿", text="合成提问"):
    return BlindTicket((_reply(original), _reply(layered)), "synthetic-user", text, ())


def _page(tmp_path, *, pair_factory=None, commit_reply=None):
    commits = []

    def default_pair(text):
        return _ticket(text=text)

    def default_commit(ticket, choice):
        commits.append((ticket, choice))

    page = BlindPage(
        batch_factory=lambda: BlindBatch(tmp_path / "results"),
        pair_factory=pair_factory or default_pair,
        commit_reply=commit_reply or default_commit,
    )
    return page, commits


def _started(tmp_path, *, pair_factory=None, commit_reply=None):
    page, commits = _page(tmp_path, pair_factory=pair_factory, commit_reply=commit_reply)
    status, body = page.start(StartRequest(consent="同意"))
    assert status == 200, body
    return page, commits


def test_consent_is_required_and_failed_pair_is_counted(tmp_path):
    page, _ = _page(
        tmp_path,
        pair_factory=lambda text: (_ for _ in ()).throw(SpeechDialogueBridgeError("synthetic")),
    )
    status, body = page.start(StartRequest(consent="不"))
    assert status == 400 and page.phase == "consent"
    assert page.batch is None

    status, body = page.start(StartRequest(consent="同意"))
    assert status == 200 and body["phase"] == "ready"

    status, body = page.turn(TurnRequest(text="合成提问"))
    assert status == 502 and body["phase"] == "ready"
    assert page.batch.payload()["failed"] == 1
    assert page.batch.payload()["comparable_rounds"] == 0
    assert "round" not in body


def test_present_vote_commits_without_exposing_route(tmp_path, monkeypatch):
    monkeypatch.setattr(share_blind.secrets, "randbelow", lambda _: 0)
    page, commits = _started(tmp_path)

    status, body = page.turn(TurnRequest(text="合成提问"))
    assert status == 200 and body["phase"] == "waiting"
    view = body["round"]
    assert set(view) == {"id", "text", "replies"}
    assert view["text"] == "合成提问"
    assert [item["label"] for item in view["replies"]] == ["A", "B"]
    assert [item["text"] for item in view["replies"]] == ["分层稿", "原稿"]
    assert "order" not in json.dumps(body)

    round_id = view["id"]
    status, body = page.vote(VoteRequest(round_id=round_id, vote="B"))
    assert status == 200 and body["phase"] == "ready"
    assert len(commits) == 1
    committed_ticket, choice = commits[0]
    assert committed_ticket.replies[choice].text == "原稿"
    payload = page.batch.payload()
    assert payload["original_wins"] == 1
    assert payload["presentation"] == {"a_votes": 0, "b_votes": 1}

    status, _ = page.vote(VoteRequest(round_id=round_id, vote="A"))
    assert status == 409


def test_one_pending_round_and_feedback_has_no_text(tmp_path):
    page, commits = _started(tmp_path)
    status, body = page.turn(TurnRequest(text="合成提问一"))
    assert status == 200
    status, body = page.turn(TurnRequest(text="合成提问二"))
    assert status == 409 and page.phase == "waiting"

    round_id = page.state()["round"]["id"]
    status, _ = page.vote(VoteRequest(round_id="wrong", vote="A"))
    assert status == 409
    status, body = page.vote(VoteRequest(round_id=round_id, vote="skip"))
    assert status == 200 and body["phase"] == "ready"
    assert len(commits) == 1

    status, body = page.feedback()
    assert status == 200 and body["phase"] == "complete"
    saved = page.batch.path
    assert saved.is_file()
    assert "合成提问" not in saved.read_text(encoding="utf-8")
    assert "分层稿" not in saved.read_text(encoding="utf-8")
    assert json.loads(saved.read_text(encoding="utf-8"))["skipped"] == 1

    status, body = page.finish()
    assert status == 200 and body["phase"] == "closed"
    assert page.closed


def test_finish_with_pending_counts_skip_and_saves(tmp_path):
    page, commits = _started(tmp_path)
    page.turn(TurnRequest(text="合成提问"))
    status, body = page.finish()
    assert status == 200 and body["phase"] == "closed"
    assert commits == []
    assert page.batch.path.is_file()
    assert json.loads(page.batch.path.read_text(encoding="utf-8"))["skipped"] == 1


def test_loopback_server_security_and_full_round(tmp_path):
    page, commits = _page(tmp_path)
    with LoopbackServer(
        page, assets_dir=Path(share_blind.__file__).with_name("share_blind_page"),
        static={
            "/": ("index.html", "text/html; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        },
        token_header="X-Blind-Token",
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
            sent = {"Origin": origin, "X-Blind-Token": server.token}
            sent.update(headers)
            body = None if data is None else json.dumps(data)
            if body is not None:
                sent["Content-Type"] = "application/json"
            connection.request(method, path, body, sent)
            response = connection.getresponse()
            assert response.getheader("Cache-Control") == "no-store"
            payload = response.read()
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = payload
            return response.status, parsed

        try:
            status, _ = request("GET", "/")
            assert status == 200
            status, body = request("GET", "/", Host="example.test")
            assert status == 403
            assert body["message"]

            status, _ = request("POST", "/state", {}, Origin="https://example.test")
            assert status == 403
            status, _ = request("POST", "/state", {}, **{"X-Blind-Token": "wrong"})
            assert status == 403
            status, body = request("POST", "/turn", {"text": "合成提问"})
            assert status == 409
            status, body = request("POST", "/start", {"consent": "同意"})
            assert status == 200 and body["phase"] == "ready"
            status, body = request("POST", "/turn", {"text": "合成提问"})
            assert status == 200 and body["phase"] == "waiting"
            round_id = body["round"]["id"]

            status, body = request("POST", "/vote", {"round_id": round_id, "vote": "nope"})
            assert status == 400
            status, body = request("POST", "/vote", {"round_id": round_id, "vote": "A"})
            assert status == 200 and body["phase"] == "ready"
            assert len(commits) == 1

            status, body = request("POST", "/finish", {})
            assert status == 200 and body["phase"] == "closed"
            assert body["summary"]["comparable_rounds"] == 1
        finally:
            connection.close()
            page.closed = True
            worker.join(timeout=3)

    assert not worker.is_alive()
    assert page.batch.path.is_file()


def test_static_assets_are_self_contained():
    page = Path(share_blind.__file__).with_name("share_blind_page")
    html = (page / "index.html").read_text(encoding="utf-8")
    css = (page / "style.css").read_text(encoding="utf-8")
    js = (page / "app.js").read_text(encoding="utf-8")
    for text in (html, css, js):
        assert "http://" not in text
        assert "https://" not in text
    assert "/style.css" in html and "/app.js" in html
