"""Codex approval and question overlays as verified per-option controls.

Codex 0.16x draws three kinds of blocking prompt in its bottom pane:

* approvals (``Would you like to run the following command?``) and confirm
  lists, footer ``Press enter to confirm or esc to cancel`` (or ``go back``);
* ``request_user_input`` questions, header ``Question 1/2 (…)``, footer tips
  such as ``tab to add notes | enter to submit answer | esc to interrupt``;
* MCP elicitations, including MCP tool approvals such as Computer Use,
  header ``Field 1/1``, footer ``enter to submit | esc to cancel``.

Plan mode's ``Implement this plan?`` list shares its footer
(``enter select · esc back``) with the command menus, so it is recognised by
that exact title only; other selectors with that footer stay unverified.

The footer is the last thing on screen while one of them is open, so it is
the anchor: an old prompt in scrollback has the composer below it. In each a
digit key picks that option and confirms it (the list's number shortcut),
which is what each option button sends.
"""

import re

from .base import StatusUpdate, TerminalControl, TerminalMenu

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_CONFIRM_FOOTER_RE = re.compile(
    r"^press enter to confirm or esc to (?:cancel|go back)$", re.IGNORECASE
)
_PLAN_FOOTER = "enter select · esc back"
_PLAN_TITLE_RE = re.compile(r"^\s*Implement this plan\?$")
_TIP_SEPARATOR_RE = re.compile(r"\s+\|\s+")
_SUBMIT_TIP_RE = re.compile(r"\bto submit(?: answer| all)?$", re.IGNORECASE)
_LEAVE_TIP_RE = re.compile(r"\bto (?:cancel|interrupt)$", re.IGNORECASE)
_NAVIGATE_TIP_RE = re.compile(r"^←/→ to navigate (?:questions|fields)$", re.IGNORECASE)
_FORM_HEADER_RE = re.compile(r"^\s*(Question|Field) \d+/\d+\b")
_COUNTDOWN_RE = re.compile(r"\s+·\s+auto-resolves in .*$")
_APPROVAL_TITLE_RE = re.compile(
    r"^\s*(?:Would you like to |Do you want to |Submit with unanswered )"
)
# Transcript rows above the bottom pane: bullets, and the user's own messages
# (the selected option row "› 1. …" is not one).
_TRANSCRIPT_ROW_RE = re.compile(r"^\s{0,2}(?:[•✗■⚠]\s|›\s(?!\s*\d+\.\s))")
_OPTION_RE = re.compile(r"^(\s*)(›\s+)?(\d+)\.\s+(\S.*?)\s*$")
_COLUMN_GAP_RE = re.compile(r"\s{2,}")

_MAX_PROMPT_LINES = 60
_MAX_FOOTER_LINES = 2
_MAX_OPTIONS = 9  # digit shortcuts stop at 9
_MAX_LABEL = 40

_ESC = TerminalControl("⎋ Esc", ("Escape",))
_SUBMIT = TerminalControl("⏎ Submit", ("Enter",))
_NAVIGATION = (
    TerminalControl("← Previous", ("Left",)),
    TerminalControl("→ Next", ("Right",)),
)


def parse_codex_prompt(pane_text: str) -> StatusUpdate | None:
    """Return the open Codex approval/question as a menu, else None."""
    lines = [line.rstrip() for line in _ANSI_RE.sub("", pane_text).splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    footer = _footer(lines)
    if footer is None:
        return None
    kind, footer_start, tips = footer

    found = _prompt_start(lines, footer_start, kind)
    if found is None:
        return None
    start, kind = found
    body = lines[start:footer_start]
    while body and not body[-1]:
        body.pop()
    if not body:
        return None
    body[0] = _COUNTDOWN_RE.sub("", body[0])

    options = _options(body)
    if kind in {"CodexApproval", "CodexPlan"} and options is None:
        return None
    controls: list[TerminalControl] = []
    if options:
        controls.extend(
            TerminalControl(f"{number}. {_short_label(label)}", (str(number),))
            for number, label in options
        )
    else:
        controls.append(_SUBMIT)
    if any(_NAVIGATE_TIP_RE.match(tip) for tip in tips):
        controls.extend(_NAVIGATION)
    controls.append(_ESC)

    raw_text = "\n".join(_dedent(body)).strip()
    return StatusUpdate(
        raw_text=raw_text,
        display_label=kind,
        is_interactive=True,
        ui_type=kind,
        menu=TerminalMenu(tuple(controls), text_input=not options),
    )


def _footer(lines: list[str]) -> tuple[str, int, list[str]] | None:
    """Classify the footer that ends the screen (one row, or two when wrapped)."""
    for count in range(1, _MAX_FOOTER_LINES + 1):
        if len(lines) < count:
            return None
        start = len(lines) - count
        rows = [row.strip() for row in lines[start:]]
        if not all(rows):
            return None
        tips = [tip for row in rows for tip in _TIP_SEPARATOR_RE.split(row)]
        if count == 1 and _CONFIRM_FOOTER_RE.match(rows[0]):
            return "CodexApproval", start, tips
        if count == 1 and rows[0].lower() == _PLAN_FOOTER:
            return "CodexPlan", start, tips
        if any(_SUBMIT_TIP_RE.search(tip) for tip in tips) and any(
            _LEAVE_TIP_RE.search(tip) for tip in tips
        ):
            return "CodexForm", start, tips
    return None


def _prompt_start(
    lines: list[str], footer_start: int, kind: str
) -> tuple[int, str] | None:
    """First row of the prompt and its kind (a form header names a question)."""
    rows = range(footer_start - 1, max(0, footer_start - _MAX_PROMPT_LINES) - 1, -1)
    if kind == "CodexForm":
        return _form_start(lines, rows)
    if kind == "CodexPlan":
        return _titled_start(lines, rows, kind)
    return _approval_start(lines, rows, kind)


def _form_start(lines: list[str], rows: range) -> tuple[int, str] | None:
    for index in rows:
        header = _FORM_HEADER_RE.match(lines[index])
        if header:
            kind = "CodexQuestion" if header.group(1) == "Question" else "CodexForm"
            return index, kind
    return None


def _titled_start(lines: list[str], rows: range, kind: str) -> tuple[int, str] | None:
    """Only the exact plan title, met before any transcript row, counts."""
    for index in rows:
        if _PLAN_TITLE_RE.match(lines[index]):
            return index, kind
        if _TRANSCRIPT_ROW_RE.match(lines[index]):
            return None
    return None


def _approval_start(lines: list[str], rows: range, kind: str) -> tuple[int, str] | None:
    """The approval's question, else the row after the transcript above it."""
    for index in rows:
        if _APPROVAL_TITLE_RE.match(lines[index]):
            return index, kind
        if _TRANSCRIPT_ROW_RE.match(lines[index]):
            return index + 1, kind
    return None


def _options(body: list[str]) -> list[tuple[int, str]] | None:
    """The last run of options numbered 1..N that holds the cursor."""
    best: list[tuple[int, str]] | None = None
    run: list[tuple[int, str]] = []
    has_cursor = False
    option_indent = 0
    for line in body:
        option = _OPTION_RE.match(line)
        if option is not None:
            number = int(option.group(3))
            if number == 1:
                run, has_cursor = [], False
                option_indent = len(option.group(1))
            if number == len(run) + 1:
                run.append((number, option.group(4)))
                has_cursor = has_cursor or option.group(2) is not None
                if has_cursor and len(run) >= 2:  # noqa: PLR2004 - a choice needs two
                    best = list(run)
                continue
        # A wrapped description continues the option above it.
        if run and line.strip() and len(line) - len(line.lstrip()) > option_indent:
            continue
        run, has_cursor = [], False
    if best is None or len(best) > _MAX_OPTIONS:
        return None
    return best


def _short_label(text: str) -> str:
    """Option name without its description column, cut to a button's width."""
    label = _COLUMN_GAP_RE.split(text, maxsplit=1)[0]
    if len(label) <= _MAX_LABEL:
        return label
    cut = label[:_MAX_LABEL]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:") + "…"


def _dedent(rows: list[str]) -> list[str]:
    indents = [len(row) - len(row.lstrip()) for row in rows if row.strip()]
    margin = min(indents, default=0)
    return [row[margin:] for row in rows]
