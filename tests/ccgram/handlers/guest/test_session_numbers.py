"""Guest session names: <label>-N, one counter per guest for both agents."""

import json

import pytest

from ccgram.handlers.guest import session_numbers


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("CCGRAM_DIR", str(tmp_path))
    return tmp_path / session_numbers.STATE_FILE


def test_first_name_continues_after_existing_names(state) -> None:
    taken = ["xy-2", "xy-3", "xy-c1", "chore"]
    assert session_numbers.next_session_name("XY", taken) == "xy-4"
    assert json.loads(state.read_text()) == {"XY": 4}


def test_numbers_keep_counting(state) -> None:
    assert session_numbers.next_session_name("XY", []) == "xy-1"
    assert session_numbers.next_session_name("XY", []) == "xy-2"


def test_stored_number_beats_a_lower_scan(state) -> None:
    state.write_text(json.dumps({"XY": 7}))
    assert session_numbers.next_session_name("XY", ["xy-3"]) == "xy-8"


def test_a_live_name_above_the_counter_is_skipped(state) -> None:
    state.write_text(json.dumps({"XY": 2}))
    assert session_numbers.next_session_name("XY", ["xy-5"]) == "xy-6"


def test_other_shapes_and_guests_do_not_count(state) -> None:
    taken = ["qfy-task-2", "xy-9", "qfy-x"]
    assert session_numbers.next_session_name("QFY", taken) == "qfy-1"


def test_guests_count_separately(state) -> None:
    session_numbers.next_session_name("XY", [])
    assert session_numbers.next_session_name("QFY", []) == "qfy-1"
    assert json.loads(state.read_text()) == {"XY": 1, "QFY": 1}


def test_unreadable_state_falls_back_to_the_scan(state) -> None:
    state.write_text("{not json")
    assert session_numbers.next_session_name("XY", ["xy-3"]) == "xy-4"
