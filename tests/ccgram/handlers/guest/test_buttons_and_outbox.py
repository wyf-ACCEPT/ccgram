"""Guest chats: three shared buttons, no file browser, and the session outbox."""

import json
import os
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccgram.config import config
from ccgram.handlers import callback_registry
from ccgram.handlers.callback_data import (
    CB_KEYS_PREFIX,
    CB_STATUS_GET_FILE,
    CB_STATUS_SCREENSHOT,
)
from ccgram.handlers.callback_helpers import callback_user_id
from ccgram.handlers.guest import access, outbox
from ccgram.handlers.status.status_bubble import build_status_keyboard

OWNER = 1644026884
GUEST = 555
GUEST_CHAT = -1009990001
THREAD = 2


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "guest_chats", {GUEST_CHAT: "QFY"})
    monkeypatch.setattr(config, "allowed_users", {OWNER})
    monkeypatch.setattr(config, "group_id", None)
    access._reset_guest_access_for_testing()
    outbox._reset_outbox_for_testing()
    yield
    access._reset_guest_access_for_testing()


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


# --- keyboard ----------------------------------------------------------------


def test_guest_chat_keyboard_has_three_buttons() -> None:
    markup = build_status_keyboard(
        "hc-t9", history=["earlier command"], is_group=True, guest_chat=True
    )
    labels = _labels(markup)
    assert len(labels) == 3
    assert not any("Get File" in label or "↑" in label for label in labels)


def test_other_chats_keep_get_file_and_recall() -> None:
    labels = _labels(
        build_status_keyboard("hc-t9", history=["earlier command"], is_group=True)
    )
    assert any("Get File" in label for label in labels)
    assert any("↑" in label for label in labels)


# --- button authorization ----------------------------------------------------


def _callback(user_id: int, data: str):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = SimpleNamespace(
        chat=SimpleNamespace(id=GUEST_CHAT, type="supergroup")
    )
    update = MagicMock()
    update.callback_query = query
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_chat = SimpleNamespace(id=GUEST_CHAT)
    return update, query


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    acted: list[int | None] = []

    async def handler(update, _context) -> None:
        acted.append(callback_user_id(update))

    monkeypatch.setattr(callback_registry, "_find_handler", lambda _d: handler)
    monkeypatch.setattr(callback_registry, "thread_router", MagicMock())
    # The guest's session is bound in the guest chat (guest.scope checks it).
    monkeypatch.setattr(
        outbox.session_query,
        "iter_bound_topics",
        lambda: [(OWNER, GUEST_CHAT, THREAD, "hc-t9")],
    )
    return acted


async def test_guest_screenshot_acts_as_owner(seen) -> None:
    access.allow(OWNER, GUEST, GUEST_CHAT, "QFY")
    update, _query = _callback(GUEST, f"{CB_STATUS_SCREENSHOT}hc-t9")
    await callback_registry.dispatch(update, MagicMock())
    assert seen == [OWNER]


async def test_guest_cannot_press_keys_or_get_file(seen) -> None:
    access.allow(OWNER, GUEST, GUEST_CHAT, "QFY")
    for data in (f"{CB_KEYS_PREFIX}enter:hc-t9", f"{CB_STATUS_GET_FILE}hc-t9"):
        update, query = _callback(GUEST, data)
        await callback_registry.dispatch(update, MagicMock())
        query.answer.assert_awaited_once_with("Only the owner can use these buttons.")
    assert seen == []


async def test_stranger_cannot_press_shared_buttons(seen) -> None:
    update, query = _callback(777, f"{CB_STATUS_SCREENSHOT}hc-t9")
    await callback_registry.dispatch(update, MagicMock())
    assert seen == []
    query.answer.assert_awaited_once()


async def test_owner_presses_act_as_owner(seen) -> None:
    update, _query = _callback(OWNER, f"{CB_KEYS_PREFIX}enter:hc-t9")
    await callback_registry.dispatch(update, MagicMock())
    assert seen == [OWNER]


async def test_get_file_is_refused_in_a_guest_chat() -> None:
    from ccgram.handlers.status import status_bar_actions

    _update, query = _callback(OWNER, f"{CB_STATUS_GET_FILE}hc-t9")
    await status_bar_actions._handle_get_file(
        query, OWNER, f"{CB_STATUS_GET_FILE}hc-t9", MagicMock(), MagicMock()
    )
    query.answer.assert_awaited_once_with(
        "Not available in a guest chat", show_alert=True
    )


# --- outbox ------------------------------------------------------------------


def _write(path, text: str = "hello", *, age: float = 60.0) -> None:
    path.write_text(text)
    old = os.path.getmtime(path) - age
    os.utime(path, (old, old))


def test_pending_files_refuses_links_and_young_files(tmp_path) -> None:
    box = tmp_path / "outbox"
    box.mkdir()
    _write(box / "report.md")
    _write(box / "fresh.md", age=0.0)
    _write(box / ".hidden")
    secret = tmp_path / "secret.env"
    _write(secret, "KEY=1")
    (box / "link.env").symlink_to(secret)
    os.link(secret, box / "hard.env")
    assert [p.name for p in outbox.pending_files(box)] == ["report.md"]


def test_symlinked_outbox_is_ignored(tmp_path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    _write(real / "x.md")
    (tmp_path / "outbox").symlink_to(real)
    assert outbox.pending_files(tmp_path / "outbox") == []


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch, tmp_path):
    cwd = tmp_path / "guestdir"
    (cwd / "outbox").mkdir(parents=True)
    monkeypatch.setattr(
        outbox.session_query,
        "iter_bound_topics",
        lambda: [(OWNER, GUEST_CHAT, THREAD, "hc-t9"), (OWNER, -100123, 5, "hc-t1")],
    )
    monkeypatch.setattr(
        outbox,
        "view_window",
        lambda w: SimpleNamespace(cwd=str(cwd), transcript_path=None),
    )
    return cwd


async def test_flush_sends_and_moves_to_sent(session) -> None:
    _write(session / "outbox" / "plan.docx")
    client = MagicMock()
    client.send_document = AsyncMock()
    client.send_message = AsyncMock()
    await outbox.flush_outboxes(client)
    client.send_document.assert_awaited_once()
    kwargs = client.send_document.await_args.kwargs
    assert kwargs["chat_id"] == GUEST_CHAT
    assert kwargs["message_thread_id"] == THREAD
    assert kwargs["filename"] == "plan.docx"
    # A long upload timeout: a timed-out upload may still arrive.
    assert kwargs["write_timeout"] == outbox.UPLOAD_TIMEOUT_S
    assert kwargs["read_timeout"] == outbox.UPLOAD_TIMEOUT_S
    assert (session / "outbox" / "sent" / "plan.docx").exists()
    assert not (session / "outbox" / "plan.docx").exists()


async def test_failing_file_moves_to_failed_after_retries(session) -> None:
    from telegram.error import BadRequest

    _write(session / "outbox" / "bad.pdf")
    client = MagicMock()
    client.send_document = AsyncMock(side_effect=BadRequest("nope"))
    for _ in range(outbox.MAX_ATTEMPTS):
        await outbox.flush_outboxes(client)
    assert (session / "outbox" / "failed" / "bad.pdf").exists()


async def test_no_guest_chats_means_no_work(
    monkeypatch: pytest.MonkeyPatch, session
) -> None:
    monkeypatch.setattr(config, "guest_chats", {})
    _write(session / "outbox" / "plan.docx")
    client = MagicMock()
    client.send_document = AsyncMock()
    await outbox.flush_outboxes(client)
    client.send_document.assert_not_awaited()


# --- several sessions sharing one folder --------------------------------------


@pytest.fixture
def shared(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Three sessions of one guest folder, each in its own topic."""
    cwd = tmp_path / "guestdir"
    (cwd / "outbox").mkdir(parents=True)
    transcripts = {w: tmp_path / f"{w}.jsonl" for w in ("hc-t43", "hc-t46", "hc-t47")}
    for i, path in enumerate(transcripts.values()):
        _write(path, '{"type": "session_meta"}\n', age=300 - i * 100)
    threads = {"hc-t43": 80, "hc-t46": 98, "hc-t47": 105}
    monkeypatch.setattr(
        outbox.session_query,
        "iter_bound_topics",
        lambda: [(OWNER, GUEST_CHAT, t, w) for w, t in threads.items()],
    )
    monkeypatch.setattr(
        outbox,
        "view_window",
        lambda w: SimpleNamespace(cwd=str(cwd), transcript_path=transcripts[w]),
    )
    return SimpleNamespace(cwd=cwd, transcripts=transcripts, threads=threads)


def _client() -> MagicMock:
    client = MagicMock()
    client.send_document = AsyncMock()
    client.send_message = AsyncMock()
    return client


async def test_a_file_goes_to_the_session_that_wrote_it(shared) -> None:
    # The Claude session hc-t47 wrote the file; hc-t43 comes first in the
    # binding list and is the session the file used to go to.
    with open(shared.transcripts["hc-t47"], "a") as f:
        f.write('{"tool": "Write", "file_path": "outbox/风格A_深空鎏金.png"}\n')
    old = os.path.getmtime(shared.transcripts["hc-t47"]) - 200
    os.utime(shared.transcripts["hc-t47"], (old, old))
    _write(shared.cwd / "outbox" / "风格A_深空鎏金.png")
    client = _client()
    await outbox.flush_outboxes(client)
    assert client.send_document.await_args.kwargs["message_thread_id"] == 105


async def test_an_escaped_name_in_the_transcript_counts(shared) -> None:
    with open(shared.transcripts["hc-t46"], "a") as f:
        f.write(json.dumps({"path": "outbox/风格B.png"}) + "\n")  # \\u-escaped
    _write(shared.cwd / "outbox" / "风格B.png")
    client = _client()
    await outbox.flush_outboxes(client)
    assert client.send_document.await_args.kwargs["message_thread_id"] == 98


async def test_a_file_nobody_names_goes_to_the_most_recently_active(shared) -> None:
    _write(shared.cwd / "outbox" / "notes.md")
    client = _client()
    await outbox.flush_outboxes(client)
    # hc-t47's transcript was written last (age 100 s).
    assert client.send_document.await_args.kwargs["message_thread_id"] == 105


async def test_each_shared_file_is_sent_once(shared) -> None:
    _write(shared.cwd / "outbox" / "a.md")
    _write(shared.cwd / "outbox" / "b.md")
    client = _client()
    await outbox.flush_outboxes(client)
    assert client.send_document.await_count == 2
