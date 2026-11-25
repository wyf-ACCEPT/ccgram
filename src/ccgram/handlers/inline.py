"""Top-level inline-query and unsupported-content handlers.

These two callbacks live at the root of ``handlers/`` because they have
no natural feature subpackage:

- ``inline_query_handler`` echoes the user's typed text as a sendable
  inline result so any client can dispatch a queued message even when
  not yet inside a topic.
- ``unsupported_content_handler`` replies to media types the bot does
  not consume (stickers, locations, etc.) with an actionable hint, to the
  owner and to a guest in their guest chat alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import structlog
from telegram import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from ..config import config
from .guest import access as guest_access
from .messaging_pipeline.message_sender import safe_reply

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()


async def inline_query_handler(
    update: Update, _context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Echo query text as a sendable inline result."""
    if not update.inline_query:
        return
    user = update.effective_user
    if not user or not config.is_user_allowed(user.id):
        return
    text = update.inline_query.query.strip()
    if not text:
        await update.inline_query.answer([])
        return

    result = InlineQueryResultArticle(
        id="cmd",
        title=text,
        description="Tap to send",
        input_message_content=InputTextMessageContent(message_text=text),
    )
    await update.inline_query.answer([result], cache_time=0, is_personal=True)


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to messages the bot cannot pass on (stickers, polls, locations, ...).

    A guest gets the same hint in their guest chat; silence there made it look
    as if the session had received the message. Strangers still get nothing.
    """
    message = update.message
    if not message:
        return
    user = update.effective_user
    if not user:
        return
    if not config.is_user_allowed(user.id):
        if guest_access.owner_for(user.id, message.chat.id) is None:
            return
        logger.info(
            "Unsupported content from a guest", user_id=user.id, chat_id=message.chat.id
        )
    else:
        logger.debug("Unsupported content from user %d", user.id)
    supported = "text, photos, files, videos (up to 20 MB)"
    if config.whisper_provider and config.is_user_allowed(user.id):
        supported += " or voice"
    await safe_reply(
        message,
        f"\u26a0 This kind of message can't be passed to the session. Use {supported}.",
    )
