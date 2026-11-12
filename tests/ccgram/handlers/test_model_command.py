"""/model in a topic: buttons for the session's models; guests too."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccgram.config import config
from ccgram.handlers import callback_registry, model_command
from ccgram.handlers.callback_data import CB_MODEL
from ccgram.handlers.guest import access

OWNER = 1644026884
GUEST = 555
GUEST_CHAT = -1009990001
OWN_CHAT = -1004477516122
WINDOW = "hc-t9"


def _codex_home(root: Path) -> Path:
    home = root / "admin" / "codex" / "XY" / ".codex"
    (home / "sessions" / "2026" / "11" / "12").mkdir(parents=True)
    (home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-6-astra",
                        "display_name": "GPT-6-Astra",
                        "visibility": "list",
                    },
                    {
                        "slug": "gpt-6-luna",
                        "display_name": "GPT-6-Luna",
                        "visibility": "list",
                    },
                    {
                        "slug": "gpt-reserve",
                        "display_name": "Reserve",
                        "visibility": "hide",
                    },
                ]
            }
        )
    )
    return home


def _codex_transcript(home: Path, model: str) -> Path:
    path = home / "sessions" / "2026" / "11" / "12" / "rollout.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"type": "session_meta", "payload": {"id": "x"}},
                {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
                {"type": "turn_context", "payload": {"model": model}},
            )
        )
        + "\n"
    )
    return path


def _claude_transcript(root: Path, model: str) -> Path:
    path = root / "claude.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {"model": model}}) + "\n"
    )
    return path


# --- reading models ---------------------------------------------------------------


def test_codex_models_come_from_the_sessions_own_codex_home(tmp_path: Path) -> None:
    home = _codex_home(tmp_path)
    transcript = _codex_transcript(home, "gpt-6-luna")
    assert model_command.codex_models(transcript) == [
        ("gpt-6-astra", "GPT-6-Astra"),
        ("gpt-6-luna", "GPT-6-Luna"),
    ]


def test_codex_models_without_a_cache_is_empty(tmp_path: Path) -> None:
    assert (
        model_command.codex_models(tmp_path / ".codex" / "sessions" / "a.jsonl") == []
    )


def test_current_codex_model_is_the_latest_turn(tmp_path: Path) -> None:
    transcript = _codex_transcript(_codex_home(tmp_path), "gpt-6-luna")
    assert model_command.current_model("codex", transcript) == "gpt-6-luna"


def test_current_claude_model_is_the_last_reply(tmp_path: Path) -> None:
    transcript = _claude_transcript(tmp_path, "claude-sonnet-5-5")
    assert model_command.current_model("claude", transcript) == "claude-sonnet-5-5"


def test_other_agents_have_no_buttons() -> None:
    assert model_command.model_choices("gemini", None) == []


# --- /model ------------------------------------------------------------------------


@pytest.fixture
def topic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A Claude session bound in the guest chat, plus send/reply spies."""
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "allowed_users", {OWNER})
    monkeypatch.setattr(config, "guest_chats", {GUEST_CHAT: "XY"})
    monkeypatch.setattr(config, "group_id", None)
    access._reset_guest_access_for_testing()
    transcript = _claude_transcript(tmp_path, "claude-opus-5-5-20261001")
    state = SimpleNamespace(provider="claude", transcript=transcript)
    monkeypatch.setattr(
        model_command.session_query,
        "resolve_window_for_topic",
        lambda user, thread, chat: (
            WINDOW if (user, chat) == (OWNER, GUEST_CHAT) else None
        ),
    )
    monkeypatch.setattr(model_command, "get_window_provider", lambda _w: state.provider)
    monkeypatch.setattr(
        model_command,
        "view_window",
        lambda _w: SimpleNamespace(
            transcript_path=state.transcript, window_name="xy-task"
        ),
    )
    state.send = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(model_command, "send_telegram_to_window", state.send)
    state.reply = AsyncMock()
    monkeypatch.setattr(model_command, "safe_reply", state.reply)
    state.edit = AsyncMock()
    monkeypatch.setattr(model_command, "safe_edit", state.edit)
    monkeypatch.setattr(model_command, "adapter_for", lambda _d: MagicMock())
    yield state
    access._reset_guest_access_for_testing()


def _command(user_id: int, chat_id: int, *args: str, thread_id: int | None = 7):
    update = MagicMock()
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_chat = SimpleNamespace(id=chat_id)
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.message.is_topic_message = thread_id is not None
    context = MagicMock()
    context.args = list(args)
    return update, context


@pytest.fixture(autouse=True)
def _thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_command,
        "get_thread_id",
        lambda update: update.message.message_thread_id if update.message else None,
    )


def _buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_owner_gets_buttons_with_the_current_model_ticked(topic) -> None:
    update, context = _command(OWNER, GUEST_CHAT)
    await model_command.model_command(update, context)
    text = topic.reply.await_args.args[1]
    assert "xy-task" in text and "claude-opus-5-5-20261001" in text
    buttons = _buttons(topic.reply.await_args.kwargs["reply_markup"])
    assert buttons[0] == ("✓ Opus 5.5", f"{CB_MODEL}{WINDOW}|claude-opus-5-5")
    assert [label for label, _ in buttons[1:]] == [
        "Sonnet 5.5",
        "Haiku 5.5",
        "Fable 5.1",
    ]
    topic.send.assert_not_awaited()


async def test_a_guest_in_their_group_gets_the_buttons(topic) -> None:
    access.allow(OWNER, GUEST, GUEST_CHAT, "XY")
    update, context = _command(GUEST, GUEST_CHAT)
    await model_command.model_command(update, context)
    assert "reply_markup" in topic.reply.await_args.kwargs


async def test_a_stranger_gets_nothing(topic) -> None:
    update, context = _command(777, GUEST_CHAT)
    await model_command.model_command(update, context)
    topic.reply.assert_not_awaited()


async def test_outside_a_topic_explains(topic) -> None:
    update, context = _command(OWNER, GUEST_CHAT, thread_id=None)
    await model_command.model_command(update, context)
    assert "inside a session's topic" in topic.reply.await_args.args[1]


async def test_model_with_a_listed_id_switches_directly(topic) -> None:
    update, context = _command(OWNER, GUEST_CHAT, "claude-sonnet-5-5")
    await model_command.model_command(update, context)
    assert topic.send.await_args.args[3] == "/model claude-sonnet-5-5"
    assert topic.reply.await_args.args[1].startswith("✓ Sent /model")


async def test_model_with_an_unlisted_id_is_refused(topic) -> None:
    update, context = _command(OWNER, GUEST_CHAT, "opus; rm -rf ~")
    await model_command.model_command(update, context)
    topic.send.assert_not_awaited()
    assert "Unknown model" in topic.reply.await_args.args[1]


async def test_codex_session_lists_its_own_models(topic, tmp_path: Path) -> None:
    home = _codex_home(tmp_path / "g")
    topic.provider = "codex"
    topic.transcript = _codex_transcript(home, "gpt-6-luna")
    update, context = _command(OWNER, GUEST_CHAT)
    await model_command.model_command(update, context)
    buttons = _buttons(topic.reply.await_args.kwargs["reply_markup"])
    assert buttons == [
        ("GPT-6-Astra", f"{CB_MODEL}{WINDOW}|gpt-6-astra"),
        ("✓ GPT-6-Luna", f"{CB_MODEL}{WINDOW}|gpt-6-luna"),
    ]


async def test_other_agents_fall_back_to_forwarding_for_the_owner(
    topic, monkeypatch: pytest.MonkeyPatch
) -> None:
    topic.provider = "gemini"
    forward = AsyncMock()
    monkeypatch.setattr("ccgram.handlers.commands.forward_command_handler", forward)
    update, context = _command(OWNER, GUEST_CHAT)
    await model_command.model_command(update, context)
    forward.assert_awaited_once()


async def test_other_agents_are_explained_to_a_guest(topic) -> None:
    topic.provider = "gemini"
    access.allow(OWNER, GUEST, GUEST_CHAT, "XY")
    update, context = _command(GUEST, GUEST_CHAT)
    await model_command.model_command(update, context)
    assert "Claude and Codex" in topic.reply.await_args.args[1]


# --- buttons -------------------------------------------------------------------------


def _press(data: str, user_id: int = OWNER, chat_id: int = GUEST_CHAT):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"), message_thread_id=7
    )
    update = MagicMock()
    update.callback_query = query
    update.effective_user = SimpleNamespace(id=user_id)
    update.effective_chat = SimpleNamespace(id=chat_id)
    return update, query


@pytest.fixture
def owns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_command, "user_owns_window", lambda _u, w: w == WINDOW)
    monkeypatch.setattr(
        model_command, "resolve_callback_data", lambda data, _u, _o: data
    )


async def test_pressing_a_model_types_it_into_the_session(topic, owns) -> None:
    update, query = _press(f"{CB_MODEL}{WINDOW}|claude-haiku-5-5")
    await model_command._model_callback(update, MagicMock())
    assert topic.send.await_args.args[1] == WINDOW
    assert topic.send.await_args.args[3] == "/model claude-haiku-5-5"
    assert "Sent /model claude-haiku-5-5" in topic.edit.await_args.args[1]


async def test_a_forged_model_never_reaches_the_terminal(topic, owns) -> None:
    update, query = _press(f"{CB_MODEL}{WINDOW}|x\n/exit")
    await model_command._model_callback(update, MagicMock())
    topic.send.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        "That model is not available here", show_alert=True
    )


async def test_a_session_you_do_not_own_is_refused(topic, owns) -> None:
    update, query = _press(f"{CB_MODEL}hc-t5|claude-haiku-5-5")
    await model_command._model_callback(update, MagicMock())
    topic.send.assert_not_awaited()


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch, topic) -> list[int | None]:
    """Guest presses through the registry: which ones reach a handler."""
    from ccgram.handlers.callback_helpers import callback_user_id
    from ccgram.handlers.guest import scope

    acted: list[int | None] = []

    async def handler(update, _context) -> None:
        acted.append(callback_user_id(update))

    monkeypatch.setattr(callback_registry, "_find_handler", lambda _d: handler)
    monkeypatch.setattr(callback_registry, "thread_router", MagicMock())
    monkeypatch.setattr(
        scope.session_query,
        "iter_bound_topics",
        lambda: [(OWNER, GUEST_CHAT, 7, WINDOW), (OWNER, OWN_CHAT, 4, "hc-t5")],
    )
    return acted


async def test_a_guest_may_press_a_model_for_their_groups_session(seen) -> None:
    access.allow(OWNER, GUEST, GUEST_CHAT, "XY")
    update, _query = _press(f"{CB_MODEL}{WINDOW}|claude-haiku-5-5", user_id=GUEST)
    await callback_registry.dispatch(update, MagicMock())
    assert seen == [OWNER]


async def test_a_guest_may_not_switch_the_owners_other_sessions(seen) -> None:
    access.allow(OWNER, GUEST, GUEST_CHAT, "XY")
    update, query = _press(f"{CB_MODEL}hc-t5|claude-haiku-5-5", user_id=GUEST)
    await callback_registry.dispatch(update, MagicMock())
    assert seen == []
    query.answer.assert_awaited_once_with("Only the owner can use these buttons.")
