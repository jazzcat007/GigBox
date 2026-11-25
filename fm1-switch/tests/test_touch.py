"""Tests for reading the touch panel directly from /dev/input/event*.

The properties that matter, in order:

1. A tap maps to the correct screen cell, including the edges.
2. Holding the stylus down produces ONE tap, not a stream (a resistive panel repeats).
3. A missing or unreadable device degrades to "no taps" rather than an exception, because
   the screen must still run on a keyboard-only machine.

The byte source is injected, so none of this needs a real device or blocks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm1_switch import touch  # noqa: E402


def _ev(etype, code, value):
    return touch.EVENT.pack(0, 0, etype, code, value)


def _press(x, y):
    return (_ev(touch.EV_ABS, touch.ABS_X, x)
            + _ev(touch.EV_ABS, touch.ABS_Y, y)
            + _ev(touch.EV_KEY, touch.BTN_TOUCH, 1)
            + _ev(touch.EV_SYN, touch.EV_SYN, 0))


def _release():
    return (_ev(touch.EV_KEY, touch.BTN_TOUCH, 0)
            + _ev(touch.EV_SYN, touch.EV_SYN, 0))


class Stream:
    """A scripted byte source: returns each chunk once, then acts drained."""

    def __init__(self, *chunks):
        self.chunks = list(chunks)

    def __call__(self, fd, n):
        if not self.chunks:
            raise BlockingIOError()
        return self.chunks.pop(0)


def _reader(*chunks):
    r = touch.TouchReader("/nonexistent", reader=Stream(*chunks))
    r._fd = 999  # pretend open; the injected reader never uses it
    return r


def test_a_press_in_the_middle_maps_to_the_middle_cell():
    r = touch.TouchReader("/nonexistent")
    # 2047 is the exact centre of 0..4095. Scaled onto 0..79 that is 39.49, and the
    # nearest cell is 39 -- one left of the exact middle column, which is correct for an
    # even/odd difference and not an off-by-one bug.
    r._x, r._y = 2047, 2047
    assert r.to_cell(80, 24) == (39, 11)
    # The true middle lands where expected when the grid has a centre cell.
    r._x, r._y = 2048, 2048
    assert r.to_cell(81, 25) == (40, 12)


def test_the_corners_map_to_the_corner_cells():
    r = touch.TouchReader("/nonexistent")
    for (x, y), expected in [((0, 0), (0, 0)), ((4095, 0), (79, 0)),
                             ((0, 4095), (0, 23)), ((4095, 4095), (79, 23))]:
        r._x, r._y = x, y
        assert r.to_cell(80, 24) == expected


def test_coordinates_outside_the_range_are_clamped():
    r = touch.TouchReader("/nonexistent")
    r._x, r._y = -500, 99999
    col, row = r.to_cell(80, 24)
    assert 0 <= col < 80 and 0 <= row < 24


def test_holding_the_stylus_produces_one_tap():
    """A resistive panel repeats while pressed; that must not fire a button repeatedly."""
    stream = (_press(2000, 2000)
              + _ev(touch.EV_ABS, touch.ABS_X, 2100)
              + _ev(touch.EV_ABS, touch.ABS_Y, 2100))
    taps = _reader(stream).read_taps(80, 24)
    assert len(taps) == 1


def test_a_second_press_after_release_is_a_second_tap():
    r = _reader(_press(1000, 1000))
    first = r.read_taps(80, 24)
    r._read = Stream(_release())
    r.read_taps(80, 24)
    r._read = Stream(_press(3000, 3000))
    second = r.read_taps(80, 24)
    assert len(first) == 1 and len(second) == 1
    assert (second[0].col, second[0].row) != (first[0].col, first[0].row)


def test_two_taps_in_one_chunk_are_both_reported():
    taps = _reader(_press(500, 500) + _release() + _press(3500, 3500)).read_taps(80, 24)
    assert len(taps) == 2


def test_a_partial_event_is_not_misparsed():
    """A short read must not shift every later event by a few bytes."""
    taps = _reader(b"\x00" * 10).read_taps(80, 24)
    assert taps == []


def test_open_returns_false_for_a_missing_device():
    r = touch.TouchReader("/nonexistent/event99")
    assert r.open() is False
    assert r.read_taps(80, 24) == []
    assert r.readable(0) is False


def test_close_is_safe_on_an_unopened_reader():
    touch.TouchReader("/nonexistent").close()


def test_event_struct_size_matches_the_kernel_abi():
    """struct input_event is 24 bytes on 64-bit Linux; a wrong size misparses everything."""
    assert touch.EVENT_SIZE == 24
