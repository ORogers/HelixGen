"""Offline tests for the bank-listing parser.

The fixtures are built to the shape a real HX Stomp streams back (captured
live: ``{104: [{<slot>: {109: "<name>\x00", ...}}, ...]}``), so these need no
hardware and no pyusb.
"""

from __future__ import annotations

import msgpack

from helixgen_ui.device import parse_listing_names


def _envelope(rows: list[dict]) -> bytes:
    return msgpack.packb({102: 1003, 103: 0, 104: rows}, use_bin_type=True)


def test_names_are_keyed_by_absolute_slot():
    raw = _envelope(
        [
            {0: {109: "Hendrix Vibe & M\x00", 123: False, 125: 0}},
            {1: {109: "Vox AC30 TS Spac\x00", 123: False, 125: 0}},
            {7: {109: "Light Classic Ro\x00", 123: False, 125: 0}},
        ]
    )
    assert parse_listing_names(raw) == {
        0: "Hendrix Vibe & M",
        1: "Vox AC30 TS Spac",
        7: "Light Classic Ro",
    }


def test_rows_without_a_name_are_skipped_not_guessed():
    raw = _envelope([{0: {123: False}}, {1: {109: "Real Name\x00"}}])
    assert parse_listing_names(raw) == {1: "Real Name"}


def test_a_head_clipped_stream_still_names_what_it_can():
    """A stream that lost its envelope head must not cost every row its name."""
    rows = [{5: {109: "Kept Row\x00"}}, {6: {109: "Also Kept\x00"}}]
    raw = b"".join(msgpack.packb(row, use_bin_type=True) for row in rows)
    assert parse_listing_names(raw) == {5: "Kept Row", 6: "Also Kept"}


def test_garbage_is_not_mistaken_for_names():
    assert parse_listing_names(b"\xc1\xc1\xc1") == {}
