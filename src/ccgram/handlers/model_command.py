"""/model in a session's topic: pick the model from buttons.

``/model`` lists the models the topic's session can switch to (Claude:
Opus / Sonnet / Haiku / Fable; Codex: the listed models in that session's
``models_cache.json``), with the current one ticked. A tap switches the
session for this session only: Codex gets ``/model <id>``; Claude gets its
picker driven to the model and "s" (its ``/model <id>`` would also save the
model as the default for new sessions). When Claude asks whether to re-read
a cached conversation, the question comes to Telegram as two buttons.
``/model <id>`` does the same for a listed id.

Guests may use it in their own group's topics (it acts as the owner there,
like ``/sessions``). Only ids from the list are ever typed, so forged button
data cannot reach the terminal. Other agents (Gemini, Pi, …) keep the plain
forwarded ``/model`` for the owner.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update

from ..config import config
from ..multiplexer import multiplexer
from ..session_protocol.telegram import TelegramSessionAdapter, adapter_for
from .. import session_query
from ..window_query import get_window_provider, view_window
from .callback_data import CB_MODEL, CB_MODEL_CONFIRM, CB_PANE_DELIMITER
from .callback_helpers import (
    callback_user_id,
    get_thread_id,
    parse_target,
    user_owns_window,
)
from .callback_registry import register
from .callback_tokens import compact_callback_data, resolve_callback_data
from .guest import access as guest_access
from .messaging_pipeline.message_sender import safe_edit, safe_reply
from .telegram_origin import send_telegram_to_window

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# (model id, label in Claude Code's /model picker), in the picker's order.
# The label is what the switch looks for on screen; the id ticks the current
# model. CCGRAM_CLAUDE_MODELS replaces the list ("id=Label,id=Label").
CLAUDE_MODELS: tuple[tuple[str, str], ...] = (
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-fable-5-2", "Fable 5.2"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
)


def claude_models() -> list[tuple[str, str]]:
    """The Claude models offered: CCGRAM_CLAUDE_MODELS, else the defaults."""
    return config.claude_models or list(CLAUDE_MODELS)


_BUTTONS_PER_ROW = 2
# The current model is near the end of the transcript; never read it all.
_TAIL_BYTES = 512 * 1024


def _codex_home(transcript_path: Path | None) -> Path:
    """The CODEX_HOME a Codex transcript belongs to (guests have their own)."""
    if transcript_path is not None:
        for parent in transcript_path.parents:
            if parent.name == "sessions" and parent.parent.name == ".codex":
                return parent.parent
    home = os.environ.get("CODEX_HOME")
    return Path(home).expanduser() if home else Path.home() / ".codex"


def codex_models(transcript_path: Path | None) -> list[tuple[str, str]]:
    """Listed models from the session's ``models_cache.json`` ([] if none)."""
    cache = _codex_home(transcript_path) / "models_cache.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return []
    entries = data.get("models", []) if isinstance(data, dict) else data
    out: list[tuple[str, str]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        slug = entry.get("slug")
        if isinstance(slug, str) and slug:
            name = entry.get("display_name")
            out.append((slug, name if isinstance(name, str) and name else slug))
    return out


def model_choices(provider: str, transcript_path: Path | None) -> list[tuple[str, str]]:
    """``(id, label)`` pairs the session can switch to; [] when unsupported."""
    if provider == "claude":
        return claude_models()
    if provider == "codex":
        return codex_models(transcript_path)
    return []


def current_model(provider: str, transcript_path: Path | None) -> str | None:
    """The model the session last used, read from the end of its transcript."""
    if transcript_path is None:
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        model = None
        if provider == "claude" and record.get("type") == "assistant":
            message = record.get("message")
            model = message.get("model") if isinstance(message, dict) else None
        elif provider == "codex" and record.get("type") == "turn_context":
            payload = record.get("payload")
            model = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(model, str) and model and not model.startswith("<"):
            return model
    return None


def _same_model(model_id: str, current: str | None) -> bool:
    # Claude records dated ids ("claude-opus-5-5-20261001") for an alias.
    return current is not None and (
        current == model_id or current.startswith(f"{model_id}-")
    )


def _keyboard(
    window_id: str, choices: list[tuple[str, str]], current: str | None
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"✓ {label}" if _same_model(model_id, current) else label,
            callback_data=compact_callback_data(
                CB_MODEL,
                f"{CB_MODEL}{window_id}{CB_PANE_DELIMITER}{model_id}",
                window_id,
            ),
        )
        for model_id, label in choices
    ]
    rows = [
        buttons[i : i + _BUTTONS_PER_ROW]
        for i in range(0, len(buttons), _BUTTONS_PER_ROW)
    ]
    return InlineKeyboardMarkup(rows)


def _display(window_id: str) -> str:
    view = view_window(window_id)
    return (view.window_name if view else "") or window_id


def _acting_user(update: Update) -> int | None:
    """The owner for an allowed user or a guest in their group, else None."""
    user = update.effective_user
    if user is None:
        return None
    if config.is_user_allowed(user.id):
        return user.id
    chat_id = update.effective_chat.id if update.effective_chat else None
    return guest_access.owner_for(user.id, chat_id)


# --- Claude: the picker, "s" = this session only --------------------------------
#
# Claude Code's "/model <id>" also saves the model as the default for new
# sessions (the user's ~/.claude/settings.json). Its picker can apply a model
# to this session only ("s"), so the switch drives the picker instead: open
# it, move the cursor to the model, check the cursor is on it, press "s".
# Every step reads the screen first; anything unexpected closes the picker.

_PICKER_TITLE = "Select model"
_CONFIRM_TITLE = "Switch model?"
_ROW_RE = re.compile(r"^\s*(?P<cursor>\u276f)?\s*(?P<num>\d+)\.\s+(?P<rest>\S.*?)\s*$")
_POLL_S = 0.3


def _norm(label: str) -> str:
    return re.sub(r"\s+", "", label.replace("\u2714", "")).casefold()


def picker_rows(screen: str) -> tuple[dict[int, str], int | None]:
    """Rows of an open model picker (number -> label) and the cursor row."""
    rows: dict[int, str] = {}
    cursor: int | None = None
    in_picker = False
    for line in screen.splitlines():
        if _PICKER_TITLE in line:
            in_picker, rows, cursor = True, {}, None
            continue
        if not in_picker:
            continue
        match = _ROW_RE.match(line.replace("\u2193", " ").replace("\u2191", " "))
        if match is None:
            continue
        number = int(match["num"])
        rows[number] = (
            re.split(r"\s{2,}", match["rest"])[0].replace("\u2714", "").strip()
        )
        if match["cursor"]:
            cursor = number
    return rows, cursor


def confirm_open(screen: str) -> bool:
    return _CONFIRM_TITLE in screen and "Yes, switch to" in screen


async def _screen(window_id: str) -> str:
    return await multiplexer.capture_pane(window_id) or ""


async def _wait_for(
    window_id: str, ready: Callable[[str], bool], timeout: float
) -> str | None:
    deadline = time.monotonic() + timeout
    while True:
        screen = await _screen(window_id)
        if ready(screen):
            return screen
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(_POLL_S)


async def _keys(
    adapter: TelegramSessionAdapter, window_id: str, keys: list[str]
) -> bool:
    for i in range(0, len(keys), 32):
        ok, _error = await adapter.send_keys(window_id, keys[i : i + 32])
        if not ok:
            return False
    return True


async def _switch_claude(
    adapter: TelegramSessionAdapter,
    acting_id: int,
    window_id: str,
    thread_id: int | None,
    chat_id: int | None,
    label: str,
) -> str:
    """Pick *label* for this session only; "done", "confirm" or an error."""
    ok, error = await send_telegram_to_window(
        acting_id, window_id, thread_id, "/model", chat_id, adapter=adapter
    )
    if not ok:
        return f"could not open the model menu: {error}"
    screen = await _wait_for(window_id, lambda s: bool(picker_rows(s)[0]), 6.0)
    if screen is None:
        return "the model menu did not open"
    rows, cursor = picker_rows(screen)
    target = next((n for n, text in rows.items() if _norm(text) == _norm(label)), None)
    if target is None or cursor is None:
        await _keys(adapter, window_id, ["Escape"])
        return f"{label} is not in the model menu"
    step = "Down" if target > cursor else "Up"
    if target != cursor and not await _keys(
        adapter, window_id, [step] * abs(target - cursor)
    ):
        await _keys(adapter, window_id, ["Escape"])
        return "could not move in the model menu"
    if await _wait_for(window_id, lambda s: picker_rows(s)[1] == target, 3.0) is None:
        await _keys(adapter, window_id, ["Escape"])
        return f"could not select {label} in the model menu"
    if not await _keys(adapter, window_id, ["s"]):
        return "could not apply the model"
    screen = await _wait_for(
        window_id, lambda s: confirm_open(s) or not picker_rows(s)[0], 4.0
    )
    if screen is not None and confirm_open(screen):
        return "confirm"
    return "done"


def _confirm_keyboard(window_id: str) -> InlineKeyboardMarkup:
    def button(text: str, answer: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text,
            callback_data=compact_callback_data(
                CB_MODEL_CONFIRM,
                f"{CB_MODEL_CONFIRM}{window_id}{CB_PANE_DELIMITER}{answer}",
                window_id,
            ),
        )

    return InlineKeyboardMarkup(
        [[button("Switch anyway", "yes"), button("Keep the current model", "no")]]
    )


async def _send_model(
    acting_id: int,
    window_id: str,
    thread_id: int | None,
    chat_id: int | None,
    model_id: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Switch the session to *model_id*; returns the reply and its buttons."""
    display = _display(window_id)
    adapter = adapter_for(context.bot_data)
    provider = get_window_provider(window_id) or ""
    if provider == "claude":
        label = dict(claude_models()).get(model_id, model_id)
        outcome = await _switch_claude(
            adapter, acting_id, window_id, thread_id, chat_id, label
        )
        logger.info(
            "Model switch", window_id=window_id, model=model_id, outcome=outcome
        )
        if outcome == "done":
            return f"\u2713 {display} now uses {label} (this session only).", None
        if outcome == "confirm":
            return (
                f"Switching {display} to {label} makes it re-read the whole "
                "conversation once (slower, more tokens). Switch anyway?",
                _confirm_keyboard(window_id),
            )
        return f"\u274c Could not switch {display}: {outcome}.", None
    # Codex applies "/model <id>" to this session only.
    ok, error = await send_telegram_to_window(
        acting_id, window_id, thread_id, f"/model {model_id}", chat_id, adapter=adapter
    )
    if not ok:
        logger.warning("Model switch delivery failed", window_id=window_id, error=error)
        return f"\u274c Could not switch {display}: {error}", None
    logger.info("Model switch sent", window_id=window_id, model=model_id)
    return f"\u2713 Sent /model {model_id} to {display}.", None


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model [id] in a session's topic."""
    message = update.message
    acting_id = _acting_user(update)
    if message is None or acting_id is None:
        return
    is_owner = update.effective_user is not None and config.is_user_allowed(
        update.effective_user.id
    )
    thread_id = get_thread_id(update)
    chat_id = update.effective_chat.id if update.effective_chat else None
    window_id = (
        session_query.resolve_window_for_topic(acting_id, thread_id, chat_id)
        if thread_id is not None and chat_id is not None
        else None
    )
    if window_id is None:
        await safe_reply(message, "Use /model inside a session's topic.")
        return

    provider = get_window_provider(window_id) or ""
    view = view_window(window_id)
    transcript = view.transcript_path if view else None
    choices = model_choices(provider, transcript)
    if not choices:
        if is_owner:
            # Lazy: the forwarder pulls in the provider menu machinery.
            from .commands import forward_command_handler

            await forward_command_handler(update, context)
        else:
            await safe_reply(message, "Model buttons cover Claude and Codex sessions.")
        return

    args = getattr(context, "args", None) or []
    if args:
        wanted = args[0]
        if wanted not in {model_id for model_id, _ in choices}:
            listed = ", ".join(model_id for model_id, _ in choices)
            await safe_reply(message, f"Unknown model {wanted!r}. Choose: {listed}")
            return
        reply, markup = await _send_model(
            acting_id, window_id, thread_id, chat_id, wanted, context
        )
        await safe_reply(message, reply, reply_markup=markup)
        return

    current = current_model(provider, transcript)
    display = _display(window_id)
    text = f"Model for {display}" + (f" (now {current})" if current else "") + ":"
    await safe_reply(message, text, reply_markup=_keyboard(window_id, choices, current))


@register(CB_MODEL)
async def _model_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query: CallbackQuery | None = update.callback_query
    if query is None or not query.data:
        return
    # A guest's press in their group acts as the owner (callback_registry).
    uid = callback_user_id(update)
    if uid is None:
        return
    data = resolve_callback_data(query.data, uid, user_owns_window)
    if data is None or not data.startswith(CB_MODEL):
        await query.answer("This button has expired", show_alert=True)
        return
    window_id, model_id = parse_target(data[len(CB_MODEL) :])
    if not window_id or not model_id or not user_owns_window(uid, window_id):
        await query.answer("Not your session", show_alert=True)
        return
    provider = get_window_provider(window_id) or ""
    view = view_window(window_id)
    choices = model_choices(provider, view.transcript_path if view else None)
    if model_id not in {m for m, _ in choices}:
        await query.answer("That model is not available here", show_alert=True)
        return
    message = query.message
    chat_id = message.chat.id if message else None
    thread_id = getattr(message, "message_thread_id", None)
    # The picker takes a few seconds; answer the tap first.
    await query.answer("Switching\u2026")
    reply, markup = await _send_model(
        uid, window_id, thread_id, chat_id, model_id, context
    )
    await safe_edit(query, reply, reply_markup=markup)


@register(CB_MODEL_CONFIRM)
async def _confirm_callback(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Answer Claude's "Switch model?" question (cache warning) from Telegram."""
    query: CallbackQuery | None = update.callback_query
    if query is None or not query.data:
        return
    uid = callback_user_id(update)
    if uid is None:
        return
    data = resolve_callback_data(query.data, uid, user_owns_window)
    if data is None or not data.startswith(CB_MODEL_CONFIRM):
        await query.answer("This button has expired", show_alert=True)
        return
    window_id, answer = parse_target(data[len(CB_MODEL_CONFIRM) :])
    if (
        not window_id
        or answer not in {"yes", "no"}
        or not user_owns_window(uid, window_id)
    ):
        await query.answer("Not your session", show_alert=True)
        return
    screen = await _screen(window_id)
    if not confirm_open(screen):
        await query.answer("That question is no longer open", show_alert=True)
        await safe_edit(query, "The model question was already answered.")
        return
    # "Yes, switch" is the first, preselected option; Esc goes back.
    key = "Enter" if answer == "yes" else "Escape"
    ok = await _keys(adapter_for(_context.bot_data), window_id, [key])
    await query.answer()
    display = _display(window_id)
    if not ok:
        await safe_edit(query, f"\u274c Could not answer the question in {display}.")
    elif answer == "yes":
        await safe_edit(query, f"\u2713 {display} switched (this session only).")
    else:
        await safe_edit(query, f"Kept the current model in {display}.")
