"""Frame-layer tests, anchored on the byte values documented from real captures.

These need no hardware: every expectation here is a number the protocol notes
state outright, so the suite catches a drift in the framing long before it
reaches a pedal.
"""

from __future__ import annotations

import pytest

from helixgen.device.frames import (
    CLOSE_ORDER,
    CMD_CHUNK,
    CMD_IDLE,
    CMD_OPEN,
    EDIT,
    HEADER_SIZE,
    MAGIC_HANDSHAKE,
    MAGIC_NORMAL,
    OPEN_ORDER,
    PRIMARY,
    SESSION_OPEN_ACK,
    SESSION_OPEN_BODY,
    STATUS,
    ChannelState,
    FrameError,
    decode_command,
    decode_frame,
    encode_command,
    encode_frame,
    identity_query_body,
)


class TestDeclaredLength:
    """``len`` is ``8 + significant body``, which the captures pin to exact values."""

    @pytest.mark.parametrize(
        "body_len, frame_len, declared",
        [
            (0, 16, 0x08),
            (17, 36, 0x19),
            (21, 40, 0x1D),
        ],
    )
    def test_documented_frame_sizes(self, body_len, frame_len, declared):
        raw = encode_frame(
            src=EDIT.host, dst=EDIT.device, cmd=CMD_OPEN, seq=0, body=b"\x00" * body_len
        )
        assert len(raw) == frame_len
        assert raw[0] | (raw[1] << 8) == declared

    def test_bodyless_frame_is_exactly_the_header(self):
        raw = encode_frame(src=PRIMARY.host, dst=PRIMARY.device, cmd=CMD_IDLE, seq=0)
        assert len(raw) == HEADER_SIZE
        assert raw[0] == 0x08

    def test_stream_chunk_length_uses_the_high_byte(self):
        """A 272-byte chunk frame declares 0x0108, per the capture notes."""
        raw = encode_frame(
            src=EDIT.host, dst=EDIT.device, cmd=CMD_CHUNK, seq=0, body=b"\x00" * 256
        )
        assert len(raw) == 272
        assert raw[0] | (raw[1] << 8) == 0x0108


class TestHeaderFields:
    def test_field_offsets_match_the_documented_layout(self):
        raw = encode_frame(
            src=0x1080,
            dst=0x03ED,
            cmd=CMD_OPEN,
            seq=0x07,
            arg=0x1193,
            body=SESSION_OPEN_BODY,
            magic=MAGIC_HANDSHAKE,
        )
        assert raw[2] == 0x00
        assert raw[3] == MAGIC_HANDSHAKE
        assert raw[4:6] == b"\x80\x10"  # src, little-endian
        assert raw[6:8] == b"\xed\x03"  # dst, little-endian
        assert raw[8] == 0x00
        assert raw[9] == 0x07
        assert raw[10] == 0x00
        assert raw[11] == CMD_OPEN
        assert raw[12:16] == b"\x93\x11\x00\x00"  # arg, little-endian

    def test_body_is_padded_to_four_bytes_but_length_is_significant(self):
        raw = encode_frame(src=1, dst=2, cmd=CMD_OPEN, seq=0, body=b"\x01")
        assert len(raw) == HEADER_SIZE + 4  # padded
        assert raw[0] == 9  # 8 + 1 significant byte, not 8 + 4
        assert raw[HEADER_SIZE:] == b"\x01\x00\x00\x00"


class TestRoundTrip:
    def test_decode_strips_padding_using_the_declared_length(self):
        raw = encode_frame(src=0x1001, dst=0x03EF, cmd=CMD_OPEN, seq=3, body=b"\xaa")
        frame = decode_frame(raw)
        assert frame.body == b"\xaa"  # not b"\xaa\x00\x00\x00"
        assert frame.src == 0x1001
        assert frame.dst == 0x03EF
        assert frame.seq == 3

    def test_session_open_exchange_bytes(self):
        assert SESSION_OPEN_BODY == b"\x00\x10\x00\x00"
        assert SESSION_OPEN_ACK == b"\x00\x02\x00\x00"

    def test_trailing_transfer_bytes_do_not_become_payload(self):
        """A bulk read may deliver more than the frame; the header decides."""
        raw = encode_frame(src=1, dst=2, cmd=CMD_OPEN, seq=0, body=b"\xde\xad")
        frame = decode_frame(raw + b"\x00" * 64)
        assert frame.body == b"\xde\xad"


class TestDecodeRejects:
    def test_short_frame(self):
        with pytest.raises(FrameError, match="short frame"):
            decode_frame(b"\x00" * 8)

    def test_length_below_floor(self):
        raw = bytearray(encode_frame(src=1, dst=2, cmd=CMD_IDLE, seq=0))
        raw[0] = 0x02
        with pytest.raises(FrameError, match="below the 8-byte floor"):
            decode_frame(bytes(raw))

    def test_body_longer_than_delivered(self):
        raw = bytearray(encode_frame(src=1, dst=2, cmd=CMD_IDLE, seq=0))
        raw[0] = 0xFF
        with pytest.raises(FrameError, match="exceeds"):
            decode_frame(bytes(raw))


class TestInnerCommand:
    def test_round_trip(self):
        payload = b"\x83\x66\xcd\x03\xe8"
        body = encode_command(0x0006, payload)
        flag, opcode, out = decode_command(body)
        assert (flag, opcode, out) == (0x0001, 0x0006, payload)

    def test_host_flag_is_one(self):
        assert encode_command(0x0006, b"")[:2] == b"\x01\x00"

    def test_identity_query_matches_the_documented_bytes(self):
        """``01 00 0X 00 01 00 00 00 0X`` for opcode X."""
        assert identity_query_body(0x05) == b"\x01\x00\x05\x00\x01\x00\x00\x00\x05"
        assert identity_query_body(0x06) == b"\x01\x00\x06\x00\x01\x00\x00\x00\x06"
        assert identity_query_body(0x04) == b"\x01\x00\x04\x00\x01\x00\x00\x00\x04"

    def test_truncated_command_body_is_rejected(self):
        with pytest.raises(FrameError, match="shorter than its header"):
            decode_command(b"\x01\x00\x06")


class TestChannels:
    def test_ids_match_the_capture_notation(self):
        # Device ids read off the wire as ef03 / ed03 / f003 little-endian.
        assert (PRIMARY.device, EDIT.device, STATUS.device) == (0x03EF, 0x03ED, 0x03F0)
        assert (PRIMARY.host, EDIT.host, STATUS.host) == (0x1001, 0x1080, 0x1002)

    def test_identity_opcodes(self):
        assert PRIMARY.identity_op == 0x05
        assert EDIT.identity_op == 0x06
        assert STATUS.identity_op == 0x04

    def test_close_order_is_not_merely_the_reverse_of_open(self):
        """Primary leads on open and trails on close; edit stays in the middle."""
        assert OPEN_ORDER == (PRIMARY, EDIT, STATUS)
        assert CLOSE_ORDER == (STATUS, EDIT, PRIMARY)
        assert CLOSE_ORDER[-1] is PRIMARY

    def test_magic_differs_only_for_the_opening_packet(self):
        assert MAGIC_HANDSHAKE == 0x28
        assert MAGIC_NORMAL == 0x18


class TestChannelState:
    def test_sequence_advances(self):
        state = ChannelState(EDIT)
        assert [state.next_seq() for _ in range(3)] == [0, 1, 2]

    def test_sequence_wraps_at_eight_bits(self):
        state = ChannelState(EDIT)
        state.seq = 0xFF
        assert state.next_seq() == 0xFF
        assert state.seq == 0

    def test_credit_frame_is_recognised(self):
        credit = decode_frame(
            encode_frame(src=EDIT.device, dst=EDIT.host, cmd=CMD_CHUNK, seq=0)
        )
        assert credit.is_credit

    def test_a_chunk_carrying_payload_is_not_a_credit(self):
        chunk = decode_frame(
            encode_frame(
                src=EDIT.device, dst=EDIT.host, cmd=CMD_CHUNK, seq=0, body=b"\x01" * 256
            )
        )
        assert not chunk.is_credit
