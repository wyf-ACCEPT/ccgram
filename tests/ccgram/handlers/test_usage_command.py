"""/usage: Claude snapshot + newest Codex rate_limits, owner only."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from ccgram.handlers import usage_command as usage

NOW = datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _claude_raw(**overrides: object) -> dict:
    raw: dict = {
        "updated_at": _iso(NOW - timedelta(minutes=3)),
        "five_hour": {
            "used_percentage": 42,
            "resets_at": _iso(NOW + timedelta(hours=2)),
        },
        "seven_day": {
            "used_percentage": 70.4,
            "resets_at": _iso(NOW + timedelta(days=4, hours=18)),
        },
    }
    raw.update(overrides)
    return raw


def _codex_line(ts: datetime, used: float, minutes: int = 10080) -> str:
    return json.dumps(
        {
            "timestamp": _iso(ts),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": used,
                        "window_minutes": minutes,
                        "resets_at": int((NOW + timedelta(days=1)).timestamp()),
                    },
                    "secondary": None,
                },
            },
        }
    )


class TestParsing:
    def test_claude_snapshot(self) -> None:
        report = usage.parse_claude_snapshot(_claude_raw())
        assert report is not None
        assert [w.label for w in report.windows] == ["5h", "7d"]
        assert report.windows[0].used_percent == 42
        assert report.windows[1].resets_at == NOW + timedelta(days=4, hours=18)

    def test_claude_snapshot_without_time_is_rejected(self) -> None:
        assert usage.parse_claude_snapshot(_claude_raw(updated_at=None)) is None
        assert usage.parse_claude_snapshot(["not", "a", "dict"]) is None

    def test_codex_record_labels_windows(self) -> None:
        report = usage.parse_codex_record(_codex_line(NOW, 11.0))
        assert report is not None
        assert [(w.label, w.used_percent) for w in report.windows] == [("7d", 11.0)]
        five_hour = usage.parse_codex_record(_codex_line(NOW, 5.0, minutes=300))
        assert five_hour is not None and five_hour.windows[0].label == "5h"

    def test_codex_line_without_limits_is_ignored(self) -> None:
        assert usage.parse_codex_record('{"timestamp": "x", "payload": {}}') is None
        assert usage.parse_codex_record("not json") is None


class TestReading:
    def test_reads_claude_file(self, tmp_path: Path) -> None:
        path = tmp_path / "claude-usage.json"
        path.write_text(json.dumps(_claude_raw()))
        report = usage.read_claude_usage(path)
        assert report is not None and report.source == "Claude"
        assert usage.read_claude_usage(tmp_path / "missing.json") is None

    def test_codex_picks_the_newest_record(self, tmp_path: Path) -> None:
        old = tmp_path / "2026/11/05/rollout-a.jsonl"
        new = tmp_path / "2026/11/24/rollout-b.jsonl"
        for path in (old, new):
            path.parent.mkdir(parents=True)
        # The older file was written last, but its record is older.
        new.write_text(_codex_line(NOW - timedelta(hours=1), 30.0) + "\n")
        old.write_text(
            _codex_line(NOW - timedelta(hours=5), 10.0)
            + "\n"
            + _codex_line(NOW - timedelta(hours=2), 20.0)
            + "\n{}\n"
        )
        report = usage.read_codex_usage([tmp_path])
        assert report is not None
        assert report.windows[0].used_percent == 30.0

    def test_codex_reads_only_the_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout-big.jsonl"
        filler = ("x" * 1000 + "\n") * 600  # > the 512 KiB tail
        path.write_text(_codex_line(NOW, 99.0) + "\n" + filler)
        assert usage.read_codex_usage([tmp_path]) is None
        with path.open("a") as f:
            f.write(_codex_line(NOW, 12.0) + "\n")
        report = usage.read_codex_usage([tmp_path])
        assert report is not None and report.windows[0].used_percent == 12.0

    def test_env_overrides_claude_path(self, tmp_path: Path) -> None:
        with patch.dict(
            os.environ, {"CCGRAM_CLAUDE_USAGE_FILE": str(tmp_path / "u.json")}
        ):
            assert usage.claude_usage_file() == tmp_path / "u.json"


class TestFormat:
    def test_full_report(self) -> None:
        claude = usage.parse_claude_snapshot(_claude_raw())
        codex = usage.parse_codex_record(_codex_line(NOW - timedelta(hours=1), 11.0))
        text = usage.format_usage(claude, codex, now=NOW)
        assert "Claude (as of" in text and "3m ago" in text
        assert "• 5h: ▓▓▓▓░░░░░░ 42%" in text
        assert "(in 2h 0m)" in text
        assert "• 7d: ▓▓▓▓▓▓▓░░░ 70%" in text and "(in 4d 18h)" in text
        assert "Codex (as of" in text and "1h 0m ago" in text
        assert "• 7d: ▓░░░░░░░░░ 11%" in text

    def test_passed_reset_says_so(self) -> None:
        raw = _claude_raw(
            five_hour={
                "used_percentage": 97,
                "resets_at": _iso(NOW - timedelta(minutes=10)),
            }
        )
        text = usage.format_usage(usage.parse_claude_snapshot(raw), None, now=NOW)
        assert "• 5h: reset at" in text and "(was 97%)" in text

    def test_missing_sources_explain_themselves(self, tmp_path: Path) -> None:
        text = usage.format_usage(None, None, now=NOW, claude_path=tmp_path / "u.json")
        assert f"no snapshot at {tmp_path / 'u.json'}" in text
        assert "externalUsageWritePath" in text
        assert "Codex: no rate limits found" in text


class TestCommand:
    def _update(self) -> SimpleNamespace:
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=5), message=SimpleNamespace()
        )

    async def _run(self, allowed: bool) -> AsyncMock:
        with (
            patch.object(usage.config, "is_user_allowed", return_value=allowed),
            patch.object(
                usage,
                "claude_usage_tracker",
                MagicMock(observe=MagicMock(return_value=None)),
            ),
            patch.object(usage, "read_codex_usage", return_value=None),
            patch.object(usage, "safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            await usage.usage_command(self._update(), None)  # type: ignore[arg-type]
        return mock_reply

    async def test_owner_gets_report(self) -> None:
        mock_reply = await self._run(allowed=True)
        call = mock_reply.await_args
        assert call is not None and call.args[1].startswith("📊 Usage")

    async def test_others_get_nothing(self) -> None:
        mock_reply = await self._run(allowed=False)
        mock_reply.assert_not_awaited()


def _win(
    label: str, used: float | None, reset_in_h: float | None
) -> "usage.UsageWindow":
    reset = None if reset_in_h is None else NOW + timedelta(hours=reset_in_h)
    return usage.UsageWindow(label, used, reset)


class TestMerge:
    """Stale sessions must not drag the numbers back (see module docs)."""

    def test_same_window_keeps_the_higher_reading(self) -> None:
        old, new = _win("7d", 79, 90), _win("7d", 71, 90)
        assert usage.merge_window(old, new, NOW) == old
        assert usage.merge_window(new, old, NOW) == old

    def test_later_reset_is_a_newer_window(self) -> None:
        old, new = _win("5h", 90, 1), _win("5h", 2, 5)
        assert usage.merge_window(old, new, NOW) == new
        # ...and a reading from the previous window does not come back.
        assert usage.merge_window(new, old, NOW) == new

    def test_empty_reading_never_replaces_a_known_one(self) -> None:
        old = _win("5h", 2, 4)
        assert usage.merge_window(old, _win("5h", None, None), NOW) == old
        assert usage.merge_window(old, None, NOW) == old
        assert usage.merge_window(None, old, NOW) == old

    def test_expired_window_gives_way(self) -> None:
        old, new = _win("5h", 97, -0.5), _win("5h", 1, None)
        assert usage.merge_window(old, new, NOW) == new

    def test_reports_merge_per_label(self) -> None:
        old = usage.UsageReport(
            "Claude", NOW - timedelta(minutes=5), (_win("5h", 2, 4), _win("7d", 79, 90))
        )
        new = usage.UsageReport(
            "Claude", NOW, (_win("5h", None, None), _win("7d", 71, 90))
        )
        merged = usage.merge_reports(old, new, NOW)
        assert merged is not None
        assert merged.as_of == NOW
        assert [(w.label, w.used_percent) for w in merged.windows] == [
            ("5h", 2),
            ("7d", 79),
        ]


class TestTracker:
    def test_merges_snapshots_and_survives_restart(self, tmp_path: Path) -> None:
        source, store = tmp_path / "claude-usage.json", tmp_path / "merged.json"
        fresh = _claude_raw()
        fresh["five_hour"]["used_percentage"] = 2
        fresh["seven_day"]["used_percentage"] = 79
        source.write_text(json.dumps(fresh))
        tracker = usage.ClaudeUsageTracker(source, store)
        first = tracker.observe(NOW)
        assert first is not None

        stale = _claude_raw(five_hour={"used_percentage": None, "resets_at": None})
        stale["updated_at"] = _iso(NOW)
        source.write_text(json.dumps(stale))
        os.utime(source, (NOW.timestamp() + 10, NOW.timestamp() + 10))
        merged = tracker.observe(NOW)
        assert merged is not None
        assert [(w.label, w.used_percent) for w in merged.windows] == [
            ("5h", 2),
            ("7d", 79),
        ]

        restarted = usage.ClaudeUsageTracker(tmp_path / "missing.json", store)
        again = restarted.observe(NOW)
        assert again is not None
        assert [(w.label, w.used_percent) for w in again.windows] == [
            ("5h", 2),
            ("7d", 79),
        ]

    def test_unchanged_file_is_not_reread(self, tmp_path: Path) -> None:
        source, store = tmp_path / "claude-usage.json", tmp_path / "merged.json"
        source.write_text(json.dumps(_claude_raw()))
        tracker = usage.ClaudeUsageTracker(source, store)
        tracker.observe(NOW)
        with patch.object(usage, "read_claude_usage") as mock_read:
            tracker.observe(NOW)
        mock_read.assert_not_called()


def test_missing_window_reads_not_reported() -> None:
    report = usage.UsageReport(
        "Claude", NOW, (_win("5h", None, None), _win("7d", 71, 90))
    )
    text = usage.format_usage(report, None, now=NOW)
    assert "• 5h: not reported" in text
    assert "?" not in text
