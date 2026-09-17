"""The 16-byte framing layer of the HX vendor-specific bulk protocol.

Every exchange on interface 0 is a framed message. The header is 16 bytes and a
frame that carries a body appends it at offset 16, zero-padded to a 4-byte
boundary. Multi-byte header integers are little-endian.

``len`` deserves a note, because it is the field most easily got wrong: it is
``8 + the significant body length``, where "significant" excludes the trailing
zero padding. A 16-byte bodyless frame therefore carries ``0x08``, not ``0x10``.

Inside a data frame the body is itself structured -- a small header naming an
opcode and an inner length, then MessagePack. :func:`encode_command` builds that
inner layer; :mod:`hlxgen.device.usb` drives the session on top of it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Final

#: Normal frame magic. The very first SESSION_OPEN of a channel uses
#: :data:`MAGIC_HANDSHAKE` instead; every later frame -- the session *close*
#: included, despite sharing the open's ``cmd`` -- uses this one.
MAGIC_NORMAL: Final = 0x18
#: Magic on the opening SESSION_OPEN packet only.
MAGIC_HANDSHAKE: Final = 0x28

# ``cmd`` at header offset 11.
CMD_OPEN: Final = 0x02  # session open (with body) and session close (without)
CMD_DATA: Final = 0x04  # a command / open-resource
CMD_CHUNK: Final = 0x08  # "send me the next page" -- also the device's credit frame
CMD_STREAM: Final = 0x0C  # a command that begins or continues a paged stream
CMD_IDLE: Final = 0x10  # keepalive

HEADER_SIZE: Final = 16
#: Maximum bulk transfer the device will answer with, from the endpoint descriptor.
MAX_TRANSFER: Final = 512
#: Payload carried by a full stream chunk; a 272-byte frame is 16 + 256.
CHUNK_PAYLOAD: Final = 256

#: Body of a SESSION_OPEN request, and the reply that acknowledges it.
SESSION_OPEN_BODY: Final = bytes((0x00, 0x10, 0x00, 0x00))
SESSION_OPEN_ACK: Final = bytes((0x00, 0x02, 0x00, 0x00))


class FrameError(RuntimeError):
    """Raised when bytes off the wire are not a well-formed frame."""


@dataclass(frozen=True)
class Channel:
    """One of the three logical channels multiplexed over the single bulk pair.

    Each channel keeps its own sequence counter and its own ``arg`` offset, which
    is why the mutable half lives in :class:`ChannelState` rather than here.
    """

    name: str
    #: Host-side channel id; the ``src`` of a frame the host sends.
    host: int
    #: Device-side channel id; the ``dst`` of a frame the host sends.
    device: int
    #: Opcode this channel answers an identity query with.
    identity_op: int


#: The primary channel. Opened first, closed last, and the one that answers with
#: the ``"...Main"`` model code.
PRIMARY: Final = Channel("primary", host=0x1001, device=0x03EF, identity_op=0x05)
#: The edit channel. Carries block/parameter edits and the paged preset stream.
EDIT: Final = Channel("edit", host=0x1080, device=0x03ED, identity_op=0x06)
#: The status/meter channel. Mirrors device state back at the host.
STATUS: Final = Channel("status", host=0x1002, device=0x03F0, identity_op=0x04)

#: Open order. The device tolerates the order but this is what HX Edit does.
OPEN_ORDER: Final = (PRIMARY, EDIT, STATUS)
#: Close order -- the reverse role order, primary last. Sending these is what
#: keeps the pedal's front panel from staying locked after we detach.
CLOSE_ORDER: Final = (STATUS, EDIT, PRIMARY)


@dataclass
class ChannelState:
    """The mutable half of a channel: its sequence counter and stream offset."""

    channel: Channel
    seq: int = 0
    #: Running count of *significant* body bytes received on this channel. The
    #: device reads it like a TCP ACK -- "I have consumed this much" -- and pages
    #: a stream from it.
    arg: int = 0
    opened: bool = False

    def next_seq(self) -> int:
        """Return the current sequence byte and advance it, wrapping at 8 bits.

        **A session does not survive the wrap.** A sweep spends roughly eleven
        frames per preset, so the counter rolls over about every twenty-third
        slot, and reads come back desynced or stop answering at exactly that
        period. What the device does differently at the rollover is not decoded
        -- forcing the wrap to 1 instead of 0 moves the symptom without fixing
        it -- so callers that run long open a fresh session instead of trying to
        out-guess it. See ``Session.reads_remaining``.
        """
        value = self.seq
        self.seq = (self.seq + 1) & 0xFF
        return value


@dataclass
class Frame:
    """A decoded frame."""

    src: int
    dst: int
    cmd: int
    seq: int
    arg: int
    body: bytes
    magic: int = MAGIC_NORMAL
    #: Body length the header declared, before padding was stripped.
    declared_body_len: int = field(default=0)

    @property
    def is_keepalive(self) -> bool:
        return self.cmd == CMD_IDLE

    @property
    def is_credit(self) -> bool:
        """An empty ``cmd=0x08`` frame -- flow control, never a reply.

        Consuming one of these as an acknowledgement is precisely the mistake
        that makes every later reply off-by-one, so callers filter on it.
        """
        return self.cmd == CMD_CHUNK and not self.body


def encode_frame(
    *,
    src: int,
    dst: int,
    cmd: int,
    seq: int,
    arg: int = 0,
    body: bytes = b"",
    magic: int = MAGIC_NORMAL,
) -> bytes:
    """Build a frame. ``body`` is padded out to a 4-byte boundary for the wire."""
    header = bytearray(HEADER_SIZE)
    struct.pack_into("<H", header, 0, 8 + len(body))
    header[3] = magic
    struct.pack_into("<H", header, 4, src)
    struct.pack_into("<H", header, 6, dst)
    header[9] = seq & 0xFF
    header[11] = cmd
    struct.pack_into("<I", header, 12, arg & 0xFFFFFFFF)

    if not body:
        return bytes(header)
    padding = (-len(body)) % 4
    return bytes(header) + body + bytes(padding)


def decode_frame(raw: bytes) -> Frame:
    """Parse a frame, trimming the zero padding the wire adds.

    The declared length is the authority on where the body ends. Trusting the
    transfer length instead would admit trailing padding as payload, and in a
    reassembled stream that shifts everything after it.
    """
    if len(raw) < HEADER_SIZE:
        raise FrameError(f"short frame: {len(raw)} bytes, need at least {HEADER_SIZE}")

    declared = struct.unpack_from("<H", raw, 0)[0]
    if declared < 8:
        raise FrameError(f"declared length {declared} is below the 8-byte floor")

    body_len = declared - 8
    available = len(raw) - HEADER_SIZE
    if body_len > available:
        raise FrameError(
            f"declared body of {body_len} bytes exceeds the {available} delivered"
        )

    return Frame(
        magic=raw[3],
        src=struct.unpack_from("<H", raw, 4)[0],
        dst=struct.unpack_from("<H", raw, 6)[0],
        seq=raw[9],
        cmd=raw[11],
        arg=struct.unpack_from("<I", raw, 12)[0],
        body=bytes(raw[HEADER_SIZE : HEADER_SIZE + body_len]),
        declared_body_len=body_len,
    )


def encode_command(opcode: int, payload: bytes) -> bytes:
    """Wrap a MessagePack payload in the inner command header.

    The inner header is ``flag(2) | opcode(2) | ilen(4)``, with ``flag`` 0x0001 on
    a host command and 0x0000 on the device's reply.
    """
    return struct.pack("<HHI", 0x0001, opcode, len(payload)) + payload


def decode_command(body: bytes) -> tuple[int, int, bytes]:
    """Split an inner command body into ``(flag, opcode, payload)``."""
    if len(body) < 8:
        raise FrameError(f"command body of {len(body)} bytes is shorter than its header")
    flag, opcode, inner_len = struct.unpack_from("<HHI", body, 0)
    payload = body[8 : 8 + inner_len]
    if len(payload) < inner_len:
        raise FrameError(
            f"command declared {inner_len} payload bytes but carries {len(payload)}"
        )
    return flag, opcode, payload


def identity_query_body(identity_op: int) -> bytes:
    """Body of the per-channel identity query.

    The inner payload is the single opcode byte rather than MessagePack, which is
    why this does not go through :func:`encode_command`'s caller-side packing.
    """
    return encode_command(identity_op, bytes((identity_op,)))
