"""unsupported_content_handler: hint for the owner and guests, silence for strangers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ccgram.handlers import inline


def _update(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=-100)),
        effective_user=SimpleNamespace(id=user_id),
    )


async def _reply_for(user_id: int, *, allowed: bool, owner: int | None):
    with (
        patch.object(inline.config, "is_user_allowed", return_value=allowed),
        patch.object(inline.config, "whisper_provider", "openai"),
        patch.object(inline.guest_access, "owner_for", return_value=owner),
        patch.object(inline, "safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        await inline.unsupported_content_handler(_update(user_id), None)  # type: ignore[arg-type]
    return mock_reply


def _text(mock: AsyncMock) -> str:
    call = mock.await_args
    assert call is not None
    return call.args[1]


async def test_owner_gets_hint_with_voice() -> None:
    mock_reply = await _reply_for(1, allowed=True, owner=None)
    text = _text(mock_reply)
    assert "videos (up to 20 MB)" in text
    assert "voice" in text


async def test_guest_gets_hint_without_voice() -> None:
    mock_reply = await _reply_for(2, allowed=False, owner=1)
    text = _text(mock_reply)
    assert "videos (up to 20 MB)" in text
    assert "voice" not in text


async def test_stranger_gets_nothing() -> None:
    mock_reply = await _reply_for(3, allowed=False, owner=None)
    mock_reply.assert_not_awaited()
