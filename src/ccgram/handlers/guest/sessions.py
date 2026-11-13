"""Guests starting sessions: ``/sessions new`` (or ➕) in a guest chat.

The new session is created through the session core in the chat's workspace
(the workspace routed to this chat by ``CCGRAM_TOPIC_CHAT_ROUTES``), running
``CCGRAM_GUEST_LAUNCH_COMMAND`` (Claude) or ``CCGRAM_GUEST_CODEX_LAUNCH_COMMAND``
(Codex) with the chat's label, so it starts with the guest's restricted
settings and never with the owner's. Discovery then adopts
the window and creates its topic in this chat, as for any session started on
the computer.

Each session starts named ``<label>-N`` (``session_numbers``): Claude through
a ``{name}`` placeholder in its launch command (``--name {name}``), Codex (and
Claude when the template has no placeholder) by typing ``/rename <name>`` once
its input box is on screen. Herdr tabs and topics then follow that name.

A chat holds at most ``CCGRAM_GUEST_MAX_SESSIONS`` sessions of either kind. Sessions still
starting (created, not yet adopted) count too, and starts in one chat are
serialised, so quick repeated taps cannot exceed the limit.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ...config import config
from ...multiplexer import multiplexer
from ...multiplexer.base import canonical_window_id
from ...multiplexer.reconciliation import list_windows_for_reconciliation
from ...session_protocol.models import ProtocolError
from ..topics.topic_routes import routed_workspace
from .scope import windows_in_chat
from .session_numbers import next_session_name

if TYPE_CHECKING:
    from ...session_protocol.telegram import TelegramSessionAdapter

logger = structlog.get_logger()

# Agents a guest may start, each with its own launcher; the provider is
# recorded for the new window until discovery confirms it.
GUEST_PROVIDERS = ("claude", "codex")
# A created window not adopted within this long no longer holds a slot.
PENDING_TTL_S = 120.0

DISABLED = "Starting sessions from this chat is not enabled."
DISABLED_PROVIDER = "Starting {provider} sessions from this chat is not enabled."
UNREACHABLE = "Could not reach the computer's terminals. Try again in a moment."
NO_WORKSPACE = "This chat has no workspace to start sessions in. Ask the owner."
FAILED = "Could not start a session. The owner can see why in the log."
LIMIT = (
    "This chat already has {count} sessions (the most is {max}). "
    "End one first: /sessions, then 🗑."
)
STARTED = (
    "Starting a new {provider} session, {name} ({count} of {max}). "
    "It appears here as a new topic in a few seconds."
)

# Typing ``/rename``: wait this long for the agent's input box, then try the
# send a few times (the session core may not accept input in the first
# seconds, before discovery has seen the new terminal).
READY_TIMEOUT_S = 60.0
RENAME_ATTEMPTS = 10
RENAME_RETRY_S = 3.0
_POLL_S = 1.0
# First character of the agent's input row when it waits for input.
_INPUT_GLYPHS = {"claude": "❯", "codex": "›"}
_naming_tasks: set[asyncio.Task[None]] = set()

_pending: dict[int, dict[str, float]] = {}
_locks: dict[int, asyncio.Lock] = {}


def _clock() -> float:
    return time.monotonic()


def _lock(chat_id: int) -> asyncio.Lock:
    return _locks.setdefault(chat_id, asyncio.Lock())


async def count_sessions(chat_id: int) -> int | None:
    """Live sessions bound in *chat_id* plus ones still starting; None if unknown."""
    windows = await list_windows_for_reconciliation(multiplexer)
    if windows is None:
        return None
    live = {canonical_window_id(w.window_id) for w in windows}
    bound = {canonical_window_id(w) for w in windows_in_chat(chat_id)}
    now = _clock()
    pending = _pending.get(chat_id, {})
    for window_id, started in list(pending.items()):
        key = canonical_window_id(window_id)
        # Adopted (now bound), gone, or never adopted: the slot is free again.
        if key in bound or key not in live or now - started > PENDING_TTL_S:
            del pending[window_id]
    return len(bound & live) + len(pending)


def launch_command(provider: str) -> str:
    """The configured guest launcher for *provider* ("" when not configured)."""
    if provider == "claude":
        return config.guest_launch_command
    if provider == "codex":
        return config.guest_codex_launch_command
    return ""


def enabled_providers() -> list[str]:
    """Agents a guest chat can start, in button order."""
    return [p for p in GUEST_PROVIDERS if launch_command(p)]


async def _seed_trust(provider: str, folder: str) -> None:
    """Pre-accept Claude's folder-trust prompt, as /new does.

    Not for Codex: the guest's Codex runs with its own CODEX_HOME, whose
    config already trusts the folder, and seeding would write the owner's
    ~/.codex/config.toml instead.
    """
    if provider != "claude":
        return
    # Lazy: agent_startup imports the provider registry, which reaches back
    # into the handlers package.
    from ...agent_startup import preseed_trust

    await asyncio.to_thread(preseed_trust, provider, Path(folder))


async def start_guest_session(
    chat_id: int, adapter: TelegramSessionAdapter, provider: str = "claude"
) -> str:
    """Start one *provider* session for a guest chat; returns the reply."""
    label = config.guest_chats.get(chat_id)
    if label is None or not enabled_providers():
        return DISABLED
    template = launch_command(provider)
    if not template:
        return DISABLED_PROVIDER.format(provider=_title(provider))
    async with _lock(chat_id):
        count = await count_sessions(chat_id)
        if count is None:
            return UNREACHABLE
        limit = config.guest_max_sessions
        if count >= limit:
            logger.info("Guest session limit reached", chat_id=chat_id, count=count)
            return LIMIT.format(count=count, max=limit)
        workspace = await routed_workspace(chat_id)
        if workspace is None or not workspace.cwd:
            logger.warning("Guest chat has no routed workspace", chat_id=chat_id)
            return NO_WORKSPACE
        windows = await list_windows_for_reconciliation(multiplexer) or []
        name = next_session_name(
            label, (getattr(w, "window_name", "") for w in windows)
        )
        typed_name = "{name}" not in template
        command = template.replace("{label}", shlex.quote(label)).replace(
            "{name}", shlex.quote(name)
        )
        data = {
            "provider": provider,
            "directory": workspace.cwd,
            "workspaceId": workspace.workspace_id,
        }
        try:
            await _seed_trust(provider, workspace.cwd)
            created = await adapter.create(data, launch_command=command)
        except (ProtocolError, OSError) as exc:
            logger.warning(
                "Guest session start failed", chat_id=chat_id, error=str(exc)
            )
            return FAILED
        _pending.setdefault(chat_id, {})[created.target_id] = _clock()
        logger.info(
            "Guest session started",
            chat_id=chat_id,
            provider=provider,
            window_id=created.target_id,
            workspace=workspace.label,
            name=name,
        )
        if typed_name:
            task = asyncio.create_task(
                _type_rename(adapter, provider, created.target_id, name)
            )
            _naming_tasks.add(task)
            task.add_done_callback(_naming_tasks.discard)
        return STARTED.format(
            provider=_title(provider), name=name, count=count + 1, max=limit
        )


def _input_ready(provider: str, screen: str) -> bool:
    glyph = _INPUT_GLYPHS.get(provider)
    return bool(glyph) and any(
        line.lstrip().startswith(f"{glyph} ") for line in screen.splitlines()
    )


async def _type_rename(
    adapter: TelegramSessionAdapter, provider: str, window_id: str, name: str
) -> None:
    """Name a just-started session by typing ``/rename <name>`` into it."""
    deadline = _clock() + READY_TIMEOUT_S
    while not _input_ready(provider, await multiplexer.capture_pane(window_id) or ""):
        if _clock() >= deadline:
            logger.warning(
                "Guest session rename skipped: no input box",
                window_id=window_id,
                name=name,
            )
            return
        await asyncio.sleep(_POLL_S)
    error = ""
    for attempt in range(RENAME_ATTEMPTS):
        if attempt:
            await asyncio.sleep(RENAME_RETRY_S)
        ok, error = await adapter.send_text(window_id, f"/rename {name}")
        if ok:
            logger.info("Guest session named", window_id=window_id, name=name)
            return
    logger.warning(
        "Guest session rename failed", window_id=window_id, name=name, error=error
    )


def _title(provider: str) -> str:
    return {"claude": "Claude", "codex": "Codex"}.get(provider, provider)


def _reset_guest_sessions_for_testing() -> None:
    _pending.clear()
    for task in _naming_tasks:
        task.cancel()
    _naming_tasks.clear()
    _locks.clear()
