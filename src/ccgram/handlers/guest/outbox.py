"""Guest outbox: files the session leaves in ``outbox/`` go to the guest chat.

A guest chat has no file browser ("Get File"), so the session delivers files
itself: it saves them to ``<session cwd>/outbox/`` and CCGram sends each one to
the bound topic as a document, then moves it to ``outbox/sent/``.

Several sessions may share a folder (a guest can run up to three), so each
file goes to the topic of the session that wrote it: the one whose transcript
names the file, the most recently written if several do, else the most
recently active session. Uploads get a long timeout: a timed-out upload may
still arrive, and a retry would then post it twice.

Only plain files directly inside ``outbox/`` are sent. Symbolic links and files
with more than one hard link are refused (they could point at anything on the
computer), and the check is repeated on the open file descriptor.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import time
from pathlib import Path

import structlog
from telegram.error import RetryAfter, TelegramError

from ... import session_query
from ...config import config
from ...telegram_client import TelegramClient
from ...window_query import view_window

logger = structlog.get_logger()

OUTBOX_DIR = "outbox"
SENT_DIR = "sent"
FAILED_DIR = "failed"
MAX_BYTES = 50 * 1024 * 1024  # Bot API upload limit
# A file younger than this may still be being written.
SETTLE_S = 3.0
MAX_ATTEMPTS = 3
# Seconds for one upload: a few MB through a proxy can take well over the
# default, and a timed-out upload may still arrive (then retry duplicates it).
UPLOAD_TIMEOUT_S = 120.0
# How much of each transcript to search for a file's name.
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024

_attempts: dict[str, int] = {}


def _clock() -> float:
    return time.time()


def _real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def pending_files(outbox: Path, now: float | None = None) -> list[Path]:
    """Files ready to send, oldest first. Refuses links and young files."""
    if not _real_dir(outbox):
        return []
    now = _clock() if now is None else now
    ready: list[tuple[float, Path]] = []
    with contextlib.suppress(OSError):
        for entry in os.scandir(outbox):
            if entry.name.startswith("."):
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                if stat.S_ISLNK(info.st_mode):
                    logger.warning("Outbox link refused", name=entry.name)
                continue
            if info.st_nlink != 1:
                logger.warning("Outbox hard link refused", name=entry.name)
                continue
            if now - info.st_mtime < SETTLE_S:
                continue
            ready.append((info.st_mtime, Path(entry.path)))
    return [path for _mtime, path in sorted(ready)]


def _move(path: Path, folder: str) -> None:
    dest_dir = path.parent / folder
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / path.name
    stem, suffix, n = path.stem, path.suffix, 1
    while dest.exists():
        dest = dest_dir / f"{stem}-{n}{suffix}"
        n += 1
    shutil.move(str(path), str(dest))


async def _send_one(
    client: TelegramClient, chat_id: int, thread_id: int, path: Path
) -> bool:
    """Send one file; False when the round should stop (rate limited)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        logger.warning("Outbox file unreadable", name=path.name, error=str(exc))
        return True
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            logger.warning("Outbox file refused after open", name=path.name)
            return True
        if info.st_size > MAX_BYTES:
            await client.send_message(
                chat_id=chat_id,
                text=f"⚠️ {path.name} is too large to send "
                f"({info.st_size / 1e6:.0f} MB; the limit is 50 MB).",
                message_thread_id=thread_id,
            )
            _move(path, FAILED_DIR)
            return True
        try:
            await client.send_document(
                chat_id=chat_id,
                document=handle,
                filename=path.name,
                message_thread_id=thread_id,
                read_timeout=UPLOAD_TIMEOUT_S,
                write_timeout=UPLOAD_TIMEOUT_S,
            )
        except RetryAfter:
            return False
        except TelegramError as exc:
            key = str(path)
            _attempts[key] = _attempts.get(key, 0) + 1
            logger.warning(
                "Outbox send failed",
                name=path.name,
                attempt=_attempts[key],
                error=str(exc),
            )
            if _attempts[key] >= MAX_ATTEMPTS:
                _attempts.pop(key, None)
                _move(path, FAILED_DIR)
            return True
    _attempts.pop(str(path), None)
    _move(path, SENT_DIR)
    logger.info("Outbox file sent", name=path.name, chat_id=chat_id, size=info.st_size)
    return True


class _Session:
    """A bound guest-chat session that may own files in an outbox."""

    def __init__(
        self, chat_id: int, thread_id: int, window_id: str, transcript: Path | None
    ) -> None:
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.window_id = window_id
        self.transcript = transcript

    def last_written(self) -> float:
        try:
            return self.transcript.stat().st_mtime if self.transcript else 0.0
        except OSError:
            return 0.0

    def names(self, filename: str) -> bool:
        """Whether the end of this session's transcript names *filename*."""
        if self.transcript is None:
            return False
        try:
            with open(self.transcript, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - TRANSCRIPT_TAIL_BYTES))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return False
        # Transcripts are JSON: the name may be stored raw or \u-escaped.
        return filename in tail or json.dumps(filename)[1:-1] in tail


def owner_of(path: Path, sessions: list[_Session]) -> _Session:
    """The session a file in a shared outbox belongs to."""
    naming = [s for s in sessions if s.names(path.name)]
    return max(naming or sessions, key=_Session.last_written)


def _sessions_by_outbox() -> dict[Path, list[_Session]]:
    groups: dict[Path, list[_Session]] = {}
    seen: set[str] = set()
    for user_id, chat_id, thread_id, window_id in session_query.iter_bound_topics():
        if chat_id is None:
            chat_id = session_query.resolve_chat_id(user_id, thread_id)
        if chat_id not in config.guest_chats or window_id in seen:
            continue
        seen.add(window_id)
        view = view_window(window_id)
        if view is None or not view.cwd:
            continue
        outbox = Path(view.cwd) / OUTBOX_DIR
        groups.setdefault(outbox, []).append(
            _Session(chat_id, thread_id, window_id, view.transcript_path)
        )
    return groups


async def flush_outboxes(client: TelegramClient) -> None:
    """Send what guest-chat sessions left in their outboxes, each to its owner."""
    if not config.guest_chats:
        return
    for outbox, sessions in _sessions_by_outbox().items():
        for path in pending_files(outbox):
            owner = owner_of(path, sessions)
            if len(sessions) > 1:
                logger.info(
                    "Outbox file routed",
                    name=path.name,
                    window_id=owner.window_id,
                    candidates=len(sessions),
                )
            if not await _send_one(client, owner.chat_id, owner.thread_id, path):
                return


def _reset_outbox_for_testing() -> None:
    _attempts.clear()
