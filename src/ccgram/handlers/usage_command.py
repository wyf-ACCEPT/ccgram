"""``/usage`` — the owner's Claude and Codex plan limits, read from local files.

Neither source needs a credential:

- Claude: a snapshot of the ``rate_limits`` Claude Code hands its status line,
  written by claude-hud (``display.externalUsageWritePath``) to
  ``CCGRAM_CLAUDE_USAGE_FILE`` (default ``~/.ccgram/claude-usage.json``):
  ``{"updated_at": ISO, "five_hour": {"used_percentage", "resets_at"},
  "seven_day": {...}}``. It is as fresh as the last status-line render.
- Codex: the newest ``rate_limits`` record in a Codex rollout transcript
  (``primary`` / ``secondary`` windows with ``used_percent``,
  ``window_minutes`` and ``resets_at`` in epoch seconds).

Owner only: a guest shares the owner's sessions, not the owner's account view.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telegram import Update

from ..config import config
from ..providers.codex import codex_session_roots
from .messaging_pipeline.message_sender import safe_reply

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# Newest rollout files checked for a Codex rate_limits record, and how much of
# each file's tail is read (token_count events are frequent, so the newest
# record is near the end).
_CODEX_FILES_CHECKED = 12
_CODEX_TAIL_BYTES = 512 * 1024
_BAR_CELLS = 10


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used_percent: float | None
    resets_at: datetime | None


@dataclass(frozen=True)
class UsageReport:
    source: str
    as_of: datetime
    windows: tuple[UsageWindow, ...]


def claude_usage_file() -> Path:
    raw = os.getenv("CCGRAM_CLAUDE_USAGE_FILE", "").strip()
    return Path(raw).expanduser() if raw else config.config_dir / "claude-usage.json"


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _percent(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def parse_claude_snapshot(raw: object) -> UsageReport | None:
    """The claude-hud usage snapshot, or None when it is not one."""
    if not isinstance(raw, dict):
        return None
    as_of = _parse_time(raw.get("updated_at"))
    if as_of is None:
        return None
    windows = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        entry = raw.get(key)
        if isinstance(entry, dict):
            windows.append(
                UsageWindow(
                    label,
                    _percent(entry.get("used_percentage")),
                    _parse_time(entry.get("resets_at")),
                )
            )
    return UsageReport("Claude", as_of, tuple(windows)) if windows else None


def _window_label(minutes: object) -> str:
    if not isinstance(minutes, int) or minutes <= 0:
        return "?"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def parse_codex_record(line: str) -> UsageReport | None:
    """A rollout line carrying ``rate_limits``, or None."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    as_of = _parse_time(record.get("timestamp"))
    if not isinstance(limits, dict) or as_of is None:
        return None
    windows = []
    for key in ("primary", "secondary"):
        entry = limits.get(key)
        if isinstance(entry, dict):
            windows.append(
                UsageWindow(
                    _window_label(entry.get("window_minutes")),
                    _percent(entry.get("used_percent")),
                    _parse_time(entry.get("resets_at")),
                )
            )
    return UsageReport("Codex", as_of, tuple(windows)) if windows else None


def read_claude_usage(path: Path | None = None) -> UsageReport | None:
    try:
        raw = json.loads((path or claude_usage_file()).read_text())
    except OSError, ValueError:
        return None
    return parse_claude_snapshot(raw)


def _tail_lines(path: Path) -> list[str]:
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - _CODEX_TAIL_BYTES))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    # The first line is likely cut by the seek unless the read began at 0.
    return lines if size <= _CODEX_TAIL_BYTES else lines[1:]


def read_codex_usage(roots: list[Path] | None = None) -> UsageReport | None:
    """The newest Codex rate_limits record across the newest rollout files."""
    files: list[tuple[float, Path]] = []
    for root in codex_session_roots() if roots is None else roots:
        for path in root.rglob("rollout-*.jsonl"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
    files.sort(reverse=True)
    best: UsageReport | None = None
    for _, path in files[:_CODEX_FILES_CHECKED]:
        try:
            lines = _tail_lines(path)
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            report = parse_codex_record(line)
            if report is not None:
                if best is None or report.as_of > best.as_of:
                    best = report
                break
    return best


def _duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _clock(moment: datetime, now: datetime) -> str:
    local, today = moment.astimezone(), now.astimezone()
    if local.date() == today.date():
        return local.strftime("%H:%M")
    return local.strftime("%a %m-%d %H:%M")


def _bar(percent: float) -> str:
    filled = round(min(max(percent, 0.0), 100.0) / 100 * _BAR_CELLS)
    return "▓" * filled + "░" * (_BAR_CELLS - filled)


def _window_line(window: UsageWindow, now: datetime) -> str:
    used = window.used_percent
    shown = "?" if used is None else f"{used:.0f}%"
    if window.resets_at is not None and window.resets_at <= now:
        return (
            f"• {window.label}: reset at {_clock(window.resets_at, now)} (was {shown})"
        )
    bar = f"{_bar(used)} " if used is not None else ""
    line = f"• {window.label}: {bar}{shown}"
    if window.resets_at is not None:
        line += (
            f" · resets {_clock(window.resets_at, now)}"
            f" (in {_duration((window.resets_at - now).total_seconds())})"
        )
    return line


def format_usage(
    claude: UsageReport | None,
    codex: UsageReport | None,
    now: datetime | None = None,
    claude_path: Path | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    lines = ["📊 Usage"]
    for name, report in (("Claude", claude), ("Codex", codex)):
        lines.append("")
        if report is None:
            if name == "Claude":
                path = claude_path or claude_usage_file()
                lines.append(
                    f"Claude: no snapshot at {path}. claude-hud writes it "
                    "(display.externalUsageWritePath) after a Claude session replies."
                )
            else:
                lines.append("Codex: no rate limits found in recent Codex sessions.")
            continue
        age = _duration((now - report.as_of).total_seconds())
        lines.append(f"{name} (as of {_clock(report.as_of, now)}, {age} ago)")
        lines.extend(_window_line(w, now) for w in report.windows)
    return "\n".join(lines)


async def usage_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the owner's Claude and Codex plan limits."""
    user = update.effective_user
    message = update.message
    if not user or not message:
        return
    if not config.is_user_allowed(user.id):
        return
    # File reads (a walk of the Codex session tree) stay off the event loop.
    claude, codex = await asyncio.gather(
        asyncio.to_thread(read_claude_usage), asyncio.to_thread(read_codex_usage)
    )
    text = format_usage(claude, codex)
    await safe_reply(message, text)
