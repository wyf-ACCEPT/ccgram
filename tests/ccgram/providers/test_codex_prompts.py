"""Codex approval and question overlays must become one button per option.

Fixtures: ``exec-approval`` and ``question`` are live hc captures of Codex
0.167.2; ``mcp-computer-use`` rebuilds the reported Computer Use prompt; the
rest are the codex-rs TUI snapshots (``request_user_input`` and
``mcp_server_elicitation``).
"""

from pathlib import Path

import pytest

from ccgram.providers.base import TerminalControl
from ccgram.providers.codex import CodexProvider
from ccgram.providers.codex_prompts import parse_codex_prompt

FIXTURES = Path(__file__).parent / "fixtures" / "codex_prompts"
ESC = TerminalControl("⎋ Esc", ("Escape",))


def _capture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


@pytest.mark.parametrize(
    ("name", "ui_type", "options"),
    [
        (
            "exec-approval",
            "CodexApproval",
            (
                "1. Yes, proceed (y)",
                "2. Yes, and don't ask again for commands…",
                "3. No, and tell Codex what to do…",
            ),
        ),
        (
            "question",
            "CodexQuestion",
            ("1. Red", "2. Blue", "3. None of the above"),
        ),
        (
            "mcp-computer-use",
            "CodexForm",
            ("1. Allow", "2. Allow for this session", "3. Always allow", "4. Cancel"),
        ),
        (
            "mcp-approval-form-with-session-persist",
            "CodexForm",
            ("1. Allow", "2. Allow for this session", "3. Always allow", "4. Cancel"),
        ),
        ("mcp-boolean-form", "CodexForm", ("1. True", "2. False")),
        (
            "implement-plan",
            "CodexPlan",
            (
                "1. Yes, implement this plan",
                "2. Yes, clear context and implement",
                "3. No, stay in Plan mode",
            ),
        ),
        (
            "unanswered-confirmation",
            "CodexApproval",
            ("1. Proceed", "2. Go back"),
        ),
    ],
)
@pytest.mark.parametrize("padded", [False, True])
def test_prompt_gets_one_button_per_option(
    name: str, ui_type: str, options: tuple[str, ...], padded: bool
) -> None:
    capture = _capture(name)
    if padded:
        capture = "\n".join(line.ljust(200) for line in capture.splitlines())

    status = CodexProvider().parse_terminal_status(capture)

    assert status is not None
    assert status.is_interactive
    assert status.ui_type == ui_type
    assert not status.ui_advisory
    assert status.menu is not None
    assert not status.menu.text_input
    labels = tuple(control.label for control in status.menu.controls)
    assert labels[: len(options)] == options
    for number, control in enumerate(status.menu.controls[: len(options)], 1):
        assert control.keys == (str(number),)
    assert status.menu.controls[-1] == ESC


def test_display_keeps_every_option_and_drops_keyboard_footer() -> None:
    status = parse_codex_prompt(_capture("mcp-computer-use"))

    assert status is not None
    assert status.raw_text.startswith("Field 1/1")
    assert 'Allow Computer Use to use "Safari"?' in status.raw_text
    assert "4. Cancel" in status.raw_text
    assert "esc to cancel" not in status.raw_text
    assert "Using computer" not in status.raw_text


def test_approval_display_starts_at_its_question() -> None:
    status = parse_codex_prompt(_capture("exec-approval"))

    assert status is not None
    assert status.raw_text.startswith("Would you like to run the following command?")
    assert "$ touch probe.txt" in status.raw_text
    assert "3. No, and tell Codex what to do differently (esc)" in status.raw_text
    assert "Running touch probe.txt" not in status.raw_text


def test_wrapped_footer_with_question_navigation() -> None:
    status = parse_codex_prompt(_capture("footer-wrap"))

    assert status is not None
    assert status.ui_type == "CodexQuestion"
    assert status.menu is not None
    labels = [control.label for control in status.menu.controls]
    assert labels[:3] == ["1. Option 1", "2. Option 2", "3. Option 3"]
    assert TerminalControl("← Previous", ("Left",)) in status.menu.controls
    assert TerminalControl("→ Next", ("Right",)) in status.menu.controls
    assert "navigate questions" not in status.raw_text


def test_freeform_question_takes_a_typed_reply() -> None:
    status = parse_codex_prompt(_capture("freeform"))

    assert status is not None
    assert status.ui_type == "CodexQuestion"
    assert status.menu is not None
    assert status.menu.text_input
    assert status.menu.controls == (
        TerminalControl("⏎ Submit", ("Enter",)),
        ESC,
    )


def test_countdown_does_not_change_the_prompt_fingerprint() -> None:
    first = _capture("auto-resolution-countdown")
    later = first.replace("auto-resolves in 1m 00s", "auto-resolves in 0m 59s")

    a = parse_codex_prompt(first)
    b = parse_codex_prompt(later)

    assert a is not None and b is not None
    assert a.raw_text == b.raw_text
    assert "auto-resolves" not in a.raw_text


def test_answered_question_in_scrollback_is_not_a_prompt() -> None:
    capture = (
        "› Plan a page.\n"
        "  Question 1/1 (1 unanswered)\n"
        "  Which color?\n"
        "  › 1. Red   Use red.\n"
        "    2. Blue  Use blue.\n"
        "  tab to add notes | enter to submit answer | esc to interrupt\n"
        "\n"
        "• You chose Red.\n"
        "\n"
        "› Ask Codex to do anything\n"
        "\n"
        "  GPT-6.1-Sol high · probe · Context 3% used\n"
        "  ← for agents · ? for shortcuts\n"
    )

    assert parse_codex_prompt(capture) is None


def test_old_approval_above_composer_is_not_a_prompt() -> None:
    capture = (
        _capture("exec-approval")
        + "\n✗ You canceled the request to run touch probe.txt\n\n"
        "› Ask Codex to do anything\n\n  ? for shortcuts\n"
    )

    assert parse_codex_prompt(capture) is None


def test_footer_quoted_in_prose_is_not_a_prompt() -> None:
    capture = (
        "• The approval dialog ends with this footer:\n"
        "  1. first\n"
        "  2. second\n"
        "  Press enter to confirm or esc to cancel\n"
    )

    assert parse_codex_prompt(capture) is None


def test_plan_footer_without_its_title_stays_unverified() -> None:
    capture = (
        "• Done.\n"
        "  Pick a session\n"
        "› 1. First\n"
        "  2. Second\n"
        "  enter select · esc back\n"
    )

    assert parse_codex_prompt(capture) is None


def test_command_menus_still_win() -> None:
    capture = (
        Path(__file__).parent / "fixtures" / "codex_menus" / "model.txt"
    ).read_text()

    status = CodexProvider().parse_terminal_status(capture)

    assert status is not None
    assert status.ui_type == "CodexModel"
