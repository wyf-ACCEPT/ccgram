"""Names for guest sessions: ``<label>-N``, one counter per guest.

Claude and Codex sessions share the counter, and a number is never handed out
twice: ended sessions keep their history (Kill closes, never deletes), so a
reused name would be ambiguous in ``/resume`` lists. The last number used per
guest lives in ``~/.ccgram/guest_session_numbers.json``; names already in use
(``taken``) push it further, which also seeds it the first time.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

import structlog

from ...utils import atomic_write_json, ccgram_dir

logger = structlog.get_logger()

STATE_FILE = "guest_session_numbers.json"


def _load() -> dict[str, int]:
    path = ccgram_dir() / STATE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Guest session numbers unreadable", error=str(exc))
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, int)}


def next_session_name(label: str, taken: Iterable[str]) -> str:
    """Claim the next ``<label>-N`` for *label* and remember it."""
    prefix = label.lower()
    pattern = re.compile(rf"{re.escape(prefix)}-(\d+)")
    numbers = _load()
    highest = numbers.get(label, 0)
    for name in taken:
        match = pattern.fullmatch(name)
        if match:
            highest = max(highest, int(match.group(1)))
    number = highest + 1
    numbers[label] = number
    atomic_write_json(ccgram_dir() / STATE_FILE, numbers)
    return f"{prefix}-{number}"
