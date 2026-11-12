"""/model in a session's topic: pick the model from buttons.

``/model`` lists the models the topic's session can switch to (Claude:
Opus / Sonnet / Haiku / Fable; Codex: the listed models in that session's
``models_cache.json``), with the current one ticked. A tap types
``/model <id>`` into the session, which both agents apply directly without
opening their pickers. ``/model <id>`` does the same for a listed id.

Guests may use it in their own group's topics (it acts as the owner there,
like ``/sessions``). Only ids from the list are ever typed, so forged button
data cannot reach the terminal. Other agents (Gemini, Pi, …) keep the plain
forwarded ``/model`` for the owner.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update

from ..config import config
from ..session_protocol.telegram import adapter_for
from .. import session_query
from ..window_query import get_window_provider, view_window
from .callback_data import CB_MODEL, CB_PANE_DELIMITER
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

CLAUDE_MODELS: tuple[tuple[str, str], ...] = (
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
    ("claude-fable-5-1", "Fable 5.1"),
)
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
        return list(CLAUDE_MODELS)
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


async def _send_model(
    acting_id: int,
    window_id: str,
    thread_id: int | None,
    chat_id: int | None,
    model_id: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> str:
    """Type ``/model <id>`` into the session; returns the reply text."""
    display = _display(window_id)
    ok, error = await send_telegram_to_window(
        acting_id,
        window_id,
        thread_id,
        f"/model {model_id}",
        chat_id,
        adapter=adapter_for(context.bot_data),
    )
    if not ok:
        logger.warning("Model switch delivery failed", window_id=window_id, error=error)
        return f"❌ Could not switch {display}: {error}"
    logger.info("Model switch sent", window_id=window_id, model=model_id)
    return f"✓ Sent /model {model_id} to {display}."


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
        reply = await _send_model(
            acting_id, window_id, thread_id, chat_id, wanted, context
        )
        await safe_reply(message, reply)
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
    reply = await _send_model(uid, window_id, thread_id, chat_id, model_id, context)
    await query.answer()
    await safe_edit(query, reply)
