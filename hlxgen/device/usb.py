"""USB bulk transport for the HX vendor-specific control interface.

This is the layer that touches hardware. It opens the device, claims
**interface 0 only** -- the audio and MIDI interfaces are left alone -- runs the
session init, and exposes one synchronous request/response call plus a reader for
the device's paged streams.

Three properties of the device shape the design:

* **It is strictly one request, one response.** Nothing is pipelined.
* **Replies are matched by the transaction id echoed at MessagePack key 102**,
  never by arrival order. The device interleaves keepalives, flow-control credits
  and leftover stream chunks on the same channel, and taking one of those as an
  acknowledgement mis-attributes every later reply.
* **A dropped handle can leave the pedal's front panel locked.** The session is
  closed politely on the way out, which is why the context manager is the
  intended way in.

Every bulk operation carries a timeout. An unbounded write to a device that has
stopped draining its OUT endpoint blocks forever -- the difference between a
stalled pedal and a hung program.

**On the ``arg`` field.** Header bytes 12-15 are a per-channel running count of
the *significant* body bytes received, read by the device like a TCP ACK. It is
not zero-based: the session opens it at :data:`ARG_BASE` and it advances by each
reply's declared body length. Sending a stale ``arg`` makes a paged read stall or
re-serve page zero, so :class:`Session` tracks it centrally rather than letting
callers pass one.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import msgpack

from hlxgen.device.frames import (
    CMD_CHUNK,
    CMD_DATA,
    CMD_OPEN,
    CMD_STREAM,
    HEADER_SIZE,
    MAGIC_HANDSHAKE,
    MAGIC_NORMAL,
    MAX_TRANSFER,
    PRIMARY,
    ChannelState,
    Frame,
    FrameError,
    decode_command,
    decode_frame,
    encode_command,
    encode_frame,
)

logger = logging.getLogger(__name__)

VENDOR_ID: Final = 0x0E41

PRODUCT_IDS: Final[dict[int, str]] = {
    0x4246: "HX Stomp",
    0x4253: "HX Stomp XL",
    0x4248: "Helix Floor",
    0x424A: "Helix LT",
    0x4249: "Helix Rack",
    0x4245: "HX Effects",
    0x4247: "POD Go",
}

#: Product ids this code has actually been run against.
VERIFIED_PRODUCT_IDS: Final = frozenset({0x4246})

CONTROL_INTERFACE: Final = 0
ENDPOINT_OUT: Final = 0x01
ENDPOINT_IN: Final = 0x81

TIMEOUT_MS: Final = 2000
DRAIN_TIMEOUT_MS: Final = 50
DRAIN_MAX_READS: Final = 256

#: Frames a session will send before a caller should recycle it. The sequence
#: byte wraps at 256 and the device stops answering across the rollover, so this
#: leaves room to finish whatever is in flight and close cleanly.
SEQUENCE_BUDGET: Final = 200

#: How many times to re-read a slot whose stream came back desynced.
READ_RETRIES: Final = 3
#: Upper bound on chunk requests for one document.
MAX_STREAM_CHUNKS: Final = 512
#: Pause before a re-read, to let the device finish whatever it was sending.
BACKOFF_SECONDS: Final = 0.05

#: Where a session's ``arg`` counter starts, once the handshake has been
#: acknowledged. Confirmed live: the first request after the handshake carries
#: this, and the next carries it plus the first reply's 9 body bytes.
ARG_BASE: Final = 0x1000

#: The handshake's ``arg`` slot carries a fixed value rather than an offset.
HANDSHAKE_ARG: Final = 0x21000100

#: MessagePack envelope keys.
KEY_TXN: Final = 102
KEY_OP: Final = 100
KEY_TARGET: Final = 101
KEY_STATUS: Final = 103
KEY_PAYLOAD: Final = 104

#: Target keys used when addressing a preset slot.
KEY_BANK: Final = 107
KEY_SLOT: Final = 108

#: ``103: 255`` is the device saying it refused the command outright.
STATUS_REFUSED: Final = 255

#: Operations. Only the read side is exercised here; the write ops that reach
#: flash directly (5, 8, 16) are deliberately absent -- see ``docs/usb_protocol.md``.
OP_PRESETS_OPEN: Final = 0
OP_LIST_PRESETS: Final = 1
OP_READ_SLOT: Final = 4
OP_SAVE_PRESET: Final = 71
OP_BROWSE_OPEN: Final = 254

#: Inner opcode every command rides on.
INNER_OPCODE: Final = 0x0002


class DeviceError(RuntimeError):
    """Raised when the device cannot be opened or does not answer as expected."""


class DeviceBusyError(DeviceError):
    """Raised when another program -- almost always HX Edit -- holds interface 0."""


class DeviceRefusedError(DeviceError):
    """Raised when the device answers with a refusal status."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        op: int | None = None,
        code: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.op = op
        #: The device's own error number, when it gave one. ``-306`` on a model
        #: swap means the model does not fit the DSP budget.
        self.code = code


class StreamError(DeviceError):
    """Raised when a paged stream does not reassemble to its declared length."""


@dataclass(frozen=True)
class DeviceInfo:
    """What enumeration can tell us before a session is opened."""

    vendor_id: int
    product_id: int
    product_name: str
    serial_number: str | None
    bus: int | None
    address: int | None

    @property
    def verified(self) -> bool:
        """Whether this model is one the protocol work has been tested against."""
        return self.product_id in VERIFIED_PRODUCT_IDS

    def describe(self) -> str:
        serial = (self.serial_number or "").strip() or "unknown"
        return f"{self.product_name} (serial {serial})"


@dataclass(frozen=True)
class Reply:
    """A decoded command reply."""

    txn: int
    status: int
    payload: Any
    raw: bytes

    @property
    def refused(self) -> bool:
        return self.status == STATUS_REFUSED

    @property
    def is_empty_answer(self) -> bool:
        """``{102, 103: 0, 104: nil}`` -- the device's general "nothing here".

        For a preset slot this means flash holds no document, which is a
        different state from a stored preset whose content happens to be the
        default. It has to be told apart from a desynced read, because only one
        of the two is worth retrying.
        """
        return self.status == 0 and self.payload is None


def _require_pyusb() -> Any:
    try:
        import usb.core
        import usb.util
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise DeviceError(
            "USB support needs pyusb and libusb. Install them with:\n"
            "    pip install pyusb\n"
            "    brew install libusb"
        ) from exc
    return usb


def find_devices() -> list[DeviceInfo]:
    """Enumerate every Line 6 HX device currently attached."""
    usb = _require_pyusb()
    found: list[DeviceInfo] = []
    for dev in usb.core.find(find_all=True, idVendor=VENDOR_ID) or []:
        name = PRODUCT_IDS.get(dev.idProduct, f"unknown device 0x{dev.idProduct:04x}")
        try:
            serial = dev.serial_number
        except (usb.core.USBError, ValueError, NotImplementedError):
            # String descriptors need to talk to the device, which fails when
            # something else already holds it. The rest of the row is still valid.
            serial = None
        found.append(
            DeviceInfo(
                vendor_id=dev.idVendor,
                product_id=dev.idProduct,
                product_name=name,
                serial_number=serial,
                bus=getattr(dev, "bus", None),
                address=getattr(dev, "address", None),
            )
        )
    return found


def encode_envelope(txn: int, op: int, target: Any) -> bytes:
    """Build the MessagePack command envelope ``{102: txn, 100: op, 101: target}``.

    Floats are emitted as **float32**. Parameter values on the wire are 32-bit,
    and the device checks a value's wire type exactly rather than coercing it --
    a float64 is refused outright, which is how every value edit in a preset can
    fail while the model swaps around them all succeed.
    """
    return msgpack.packb(
        {KEY_TXN: txn, KEY_OP: op, KEY_TARGET: target},
        use_bin_type=False,
        use_single_float=True,
    )


def decode_reply(payload: bytes) -> Reply | None:
    """Decode ``{102: txn, 103: status, 104: payload}``, or ``None`` if it is not one."""
    if not payload:
        return None
    try:
        decoded = msgpack.unpackb(payload, strict_map_key=False, raw=False)
    except Exception:  # noqa: BLE001 - a body that is not an envelope is normal
        return None
    if not isinstance(decoded, dict) or KEY_TXN not in decoded:
        return None
    return Reply(
        txn=decoded[KEY_TXN],
        status=decoded.get(KEY_STATUS, 0),
        payload=decoded.get(KEY_PAYLOAD),
        raw=payload,
    )


#: Bytes of stream framing that lead the reply envelope on a paged read.
STREAM_PREFIX: Final = 8


def parse_stream_preamble(preamble: bytes) -> tuple[int, bytes]:
    """Split a stream reply into ``(declared blob length, first blob bytes)``.

    A paged read's first reply is **not** a discardable header. It carries the
    stream framing, the reply envelope, *and the start of the document*::

        00 00 7b 28 86 0a 00 00        stream prefix
        83 66 cd 03 eb 67 00 68        {102: txn, 103: status, 104:
        da 0a 7b                       str16, 2683 bytes
        a9 6c 36 2d 68 65 6c 69 78 00  fixstr(9) "l6-helix\0"  <- the document
        ...

    Dropping it costs the document's magic and its offset table, and the
    remainder still parses as plausible MessagePack -- so the loss is silent, and
    a blob reassembled without it is rejected by the device as not a preset.

    The declared length is the authority on reassembly, in both directions.
    """
    body = preamble[STREAM_PREFIX:]
    if not body or body[0] not in (0x83, 0x84, 0x85):
        raise StreamError(
            f"stream reply does not start with an envelope map: {body[:8].hex()}"
        )

    # Walk the envelope by hand: key 104's value is a str whose payload is mostly
    # still on the wire, so it cannot go through a normal unpacker.
    position = 1
    for _ in range(body[0] & 0x0F):
        key = body[position]
        position += 1
        if key == KEY_PAYLOAD:
            return _read_str_header(body, position)
        position = _skip_value(body, position)

    raise StreamError("stream reply envelope carries no key 104")


def _read_str_header(body: bytes, position: int) -> tuple[int, bytes]:
    marker = body[position]
    if 0xA0 <= marker <= 0xBF:
        length, start = marker & 0x1F, position + 1
    elif marker == 0xD9:
        length, start = body[position + 1], position + 2
    elif marker == 0xDA:
        length, start = int.from_bytes(body[position + 1 : position + 3], "big"), position + 3
    elif marker == 0xDB:
        length, start = int.from_bytes(body[position + 1 : position + 5], "big"), position + 5
    else:
        raise StreamError(f"key 104 is not a str: marker 0x{marker:02x}")
    return length, body[start:]


def _skip_value(body: bytes, position: int) -> int:
    """Step over one small scalar in the envelope header."""
    marker = body[position]
    if marker <= 0x7F or marker >= 0xE0 or marker in (0xC0, 0xC2, 0xC3):
        return position + 1
    if marker == 0xCC or marker == 0xD0:
        return position + 2
    if marker == 0xCD or marker == 0xD1:
        return position + 3
    if marker == 0xCE or marker == 0xD2:
        return position + 5
    raise StreamError(f"unexpected envelope value marker 0x{marker:02x}")


class Session:
    """An open control session over interface 0.

    Use it as a context manager so the session is closed politely::

        with Session.open() as session:
            document = session.read_slot(bank=0, slot=0)
    """

    def __init__(self, device: Any, info: DeviceInfo):
        self._usb = _require_pyusb()
        self._device = device
        self.info = info
        self._state = ChannelState(PRIMARY)
        self._claimed = False
        self._closed = False
        self._opened = False
        self._presets_open = False
        self._frames_sent = 0
        self._txn = 1000

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, info: DeviceInfo | None = None) -> Session:
        """Open the first attached device, or the one ``info`` names."""
        usb = _require_pyusb()
        if info is None:
            devices = find_devices()
            if not devices:
                raise DeviceError(
                    "No Line 6 HX device found on USB. Check that the pedal is "
                    "connected and powered on."
                )
            info = devices[0]

        device = usb.core.find(idVendor=info.vendor_id, idProduct=info.product_id)
        if device is None:
            raise DeviceError(f"{info.describe()} disappeared between enumeration and open")

        session = cls(device, info)
        session._claim()
        try:
            session.drain()
            session._init_session()
        except Exception:
            session.close()
            raise
        return session

    def _claim(self) -> None:
        usb = self._usb
        try:
            if self._device.is_kernel_driver_active(CONTROL_INTERFACE):
                self._device.detach_kernel_driver(CONTROL_INTERFACE)
        except (NotImplementedError, usb.core.USBError):
            # macOS reports no kernel driver on a vendor-specific interface.
            pass

        try:
            usb.util.claim_interface(self._device, CONTROL_INTERFACE)
        except usb.core.USBError as exc:
            if _is_busy(exc):
                raise DeviceBusyError(
                    f"{self.info.describe()} is in use by another program.\n"
                    "HX Edit holds the control interface whenever it is running -- "
                    "quit it and try again."
                ) from exc
            raise DeviceError(f"Could not claim interface 0: {exc}") from exc
        self._claimed = True

        # A halted endpoint from an interrupted session makes the device ignore
        # everything that follows, silently.
        for endpoint in (ENDPOINT_OUT, ENDPOINT_IN):
            try:
                self._device.clear_halt(endpoint)
            except usb.core.USBError as exc:
                logger.debug("clear_halt(0x%02x) failed: %s", endpoint, exc)

    def close(self) -> None:
        """Close the session, then release the interface. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._claimed and self._opened:
                self._send_close()
        finally:
            if self._claimed:
                try:
                    self._usb.util.release_interface(self._device, CONTROL_INTERFACE)
                except Exception as exc:  # noqa: BLE001 - teardown is best effort
                    logger.debug("Releasing interface 0 failed: %s", exc)
                self._claimed = False
            try:
                self._usb.util.dispose_resources(self._device)
            except Exception as exc:  # noqa: BLE001 - teardown is best effort
                logger.debug("Disposing USB resources failed: %s", exc)

    def _send_close(self) -> None:
        """Send the bare ``cmd=0x02`` close.

        Failures are logged and swallowed: we are on the way out, and a
        half-closed session beats no close at all.
        """
        try:
            self._send(cmd=CMD_OPEN, body=b"")
            self._read_frame(timeout_ms=DRAIN_TIMEOUT_MS * 8)
        except Exception as exc:  # noqa: BLE001 - we are on the way out
            logger.debug("Session close failed: %s", exc)
        self._opened = False

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- raw transport -----------------------------------------------------

    def _write(self, data: bytes, *, timeout_ms: int = TIMEOUT_MS) -> None:
        """Write one frame. Always bounded -- see the module docstring."""
        try:
            written = self._device.write(ENDPOINT_OUT, data, timeout_ms)
        except self._usb.core.USBError as exc:
            raise DeviceError(f"Bulk write failed: {exc}") from exc
        if written != len(data):
            raise DeviceError(f"Short bulk write: {written} of {len(data)} bytes")

    def _read_raw(self, *, timeout_ms: int = TIMEOUT_MS) -> bytes:
        try:
            data = self._device.read(ENDPOINT_IN, MAX_TRANSFER, timeout_ms)
        except self._usb.core.USBError as exc:
            raise DeviceError(f"Bulk read failed: {exc}") from exc
        return bytes(data)

    def _read_frame(self, *, timeout_ms: int = TIMEOUT_MS) -> Frame:
        return decode_frame(self._read_raw(timeout_ms=timeout_ms))

    def drain(self) -> int:
        """Discard bulk data left over from a previous session.

        The device ignores a new session's init packets while stale state is
        pending, so this runs before anything else. It ends when a read times
        out, which is the normal way out.
        """
        discarded = 0
        for _ in range(DRAIN_MAX_READS):
            try:
                data = self._read_raw(timeout_ms=DRAIN_TIMEOUT_MS)
            except DeviceError:
                break
            if not data:
                break
            discarded += 1
        else:
            raise DeviceError(
                "The device kept sending after 256 drain reads. Something else is "
                "probably talking to it."
            )
        if discarded:
            logger.debug("Drained %d stale frame(s) before init", discarded)
        return discarded

    # -- framing -----------------------------------------------------------

    def _send(
        self,
        *,
        cmd: int,
        body: bytes,
        magic: int = MAGIC_NORMAL,
        arg: int | None = None,
    ) -> None:
        self._frames_sent += 1
        frame = encode_frame(
            src=PRIMARY.host,
            dst=PRIMARY.device,
            cmd=cmd,
            seq=self._state.next_seq(),
            arg=self._state.arg if arg is None else arg,
            body=body,
            magic=magic,
        )
        self._write(frame)

    def _receive(self, *, timeout_ms: int = TIMEOUT_MS) -> Frame:
        """Read one frame and credit its body against the channel's ``arg``."""
        frame = self._read_frame(timeout_ms=timeout_ms)
        if frame.declared_body_len:
            self._state.arg = (self._state.arg + frame.declared_body_len) & 0xFFFFFFFF
        return frame

    def _receive_matching(
        self,
        predicate: Callable[[Frame], bool],
        *,
        timeout_ms: int = TIMEOUT_MS,
        max_frames: int = 64,
    ) -> Frame:
        """Read frames until ``predicate`` accepts one.

        Keepalives and empty credit frames are skipped unconditionally: they are
        never a reply, and consuming one as an acknowledgement is the off-by-one
        that mis-attributes every later response.
        """
        for _ in range(max_frames):
            frame = self._receive(timeout_ms=timeout_ms)
            if frame.is_keepalive or frame.is_credit:
                continue
            if predicate(frame):
                return frame
            logger.debug(
                "Skipping unmatched frame: cmd=0x%02x len=%d",
                frame.cmd,
                frame.declared_body_len,
            )
        raise DeviceError(f"No matching reply after {max_frames} frames")

    # -- session init ------------------------------------------------------

    def _init_session(self) -> None:
        """Run the proven five-packet init.

        Packet 1 resynchronises the device's session state; 2 and 4 open the two
        session resources (4 is the ``op 254`` browse-open) and 3 and 5 are the
        chunk requests that acknowledge them.
        """
        self._state.seq = 0
        self._state.arg = ARG_BASE

        self._send(
            cmd=CMD_OPEN,
            body=b"\x00\x10\x00\x00",
            magic=MAGIC_HANDSHAKE,
            arg=HANDSHAKE_ARG,
        )
        frame = self._read_frame()
        if frame.body[:4] != b"\x00\x02\x00\x00":
            raise DeviceError(
                f"Handshake was answered with {frame.body[:4].hex()} rather than "
                "the expected 00020000"
            )
        self._opened = True

        # The handshake reply does not count toward the offset; the session
        # starts its arithmetic from the base.
        self._state.seq = 0x02
        self._state.arg = ARG_BASE

        # Open the first session resource. Its inner payload is a bare selector
        # byte rather than a MessagePack envelope, so it does not go through
        # _command().
        self._send(cmd=CMD_DATA, body=encode_command(INNER_OPCODE, b"\x02"))
        self._receive()
        self._chunk_request()

        self._command(op=OP_BROWSE_OPEN, target={})
        self._chunk_request()
        logger.debug("Session init complete; arg now 0x%04x", self._state.arg)

    def _chunk_request(self) -> Frame:
        """Send a bare ``cmd=0x08`` page request and read its answer."""
        self._send(cmd=CMD_CHUNK, body=b"")
        return self._receive()

    def _command(
        self,
        *,
        op: int,
        target: Any,
        cmd: int = CMD_DATA,
    ) -> Reply | None:
        """Send one command and return its reply, matched by transaction id."""
        txn = self.next_txn()
        self._send(cmd=cmd, body=encode_command(INNER_OPCODE, encode_envelope(txn, op, target)))

        frame = self._receive_matching(lambda f: bool(f.body))
        reply = _reply_from_frame(frame)
        if reply is None:
            return None
        if reply.txn != txn:
            logger.debug(
                "Reply txn %s does not match the %s sent for op %s", reply.txn, txn, op
            )
        if reply.refused:
            code = _refusal_code(reply)
            detail = f" (code {code})" if code is not None else ""
            raise DeviceRefusedError(
                f"The device refused op {op}{detail}",
                status=reply.status,
                op=op,
                code=code,
            )
        return reply

    def _stream_command(
        self, *, op: int, target: Any
    ) -> tuple[Reply | None, bytes]:
        """Start a paged stream, returning ``(status reply, first payload)``.

        **The reply to a stream command may itself be chunk #0.** The device
        either answers with a small ``{102, 103, 104}`` status envelope -- a
        refusal, or the "nothing here" that marks an unpopulated slot -- or with
        a stream preamble that the chunk requests then re-serve from page zero.
        Only the envelope carries meaning to the caller; the preamble is returned
        so the distinction stays visible rather than being swallowed here.

        Telling them apart is by decoding, not by size: an envelope decodes as a
        map carrying key 102, and a preamble does not.
        """
        txn = self.next_txn()
        self._send(
            cmd=CMD_STREAM, body=encode_command(INNER_OPCODE, encode_envelope(txn, op, target))
        )
        frame = self._receive_matching(lambda f: bool(f.body))

        reply = _reply_from_frame(frame)
        if reply is not None:
            if reply.refused:
                raise DeviceRefusedError(
                    f"The device refused op {op} (status {reply.status})",
                    status=reply.status,
                    op=op,
                )
            return reply, b""

        # Not an envelope, so it is the stream's first page.
        return None, frame.body

    @property
    def frames_sent(self) -> int:
        """How many frames this session has put on the wire.

        The sequence byte wraps at 256 and the device does not survive the
        rollover, so a caller that reads many slots watches this and opens a
        fresh session before it gets there.
        """
        return self._frames_sent

    @property
    def near_sequence_wrap(self) -> bool:
        """Whether this session is close enough to the wrap to be worth recycling."""
        return self._frames_sent >= SEQUENCE_BUDGET

    def command(self, *, op: int, target: Any) -> Reply | None:
        """Send one command and return its reply. Raises on a refusal."""
        return self._command(op=op, target=target)

    def next_txn(self) -> int:
        """Next transaction id. A whole u16 counter, wrapping."""
        self._txn = (self._txn + 1) & 0xFFFF
        return self._txn

    # -- reads -------------------------------------------------------------

    def read_stream(self, *, max_chunks: int = 512) -> bytes:
        """Reassemble a paged stream that a command has just started.

        The stream ends when a chunk frame comes back shorter than a full page.
        Each request carries the running ``arg``, which is how the device knows
        which page to serve next.
        """
        chunks: list[bytes] = []
        for _ in range(max_chunks):
            frame = self._chunk_request()
            if frame.body:
                chunks.append(frame.body)
            if len(frame.body) + HEADER_SIZE < 272:
                break
        else:
            raise StreamError(f"Stream did not end within {max_chunks} chunks")
        return b"".join(chunks)

    def _open_presets(self) -> None:
        """Run the ``op 0`` presets-open prologue that a slot read rides on.

        Idempotent per session: the device keeps the resource open once asked,
        and re-opening it mid-sweep would restart the paging.
        """
        if self._presets_open:
            return
        self._command(op=OP_PRESETS_OPEN, target=None)
        self._presets_open = True

    def _reassemble(self, declared: int, first: bytes) -> bytes:
        """Collect chunks until the declared length is met, and no further.

        Under-length means the device stopped answering mid-stream. Over-length
        means a frame that was not stream payload got spliced in -- and a spliced
        blob parses just as readily as a real one, into the wrong preset. Both
        are refused rather than returned.
        """
        collected = bytearray(first)
        for _ in range(MAX_STREAM_CHUNKS):
            if len(collected) >= declared:
                break
            frame = self._chunk_request()
            if not frame.body:
                break
            collected += frame.body
        else:
            raise StreamError(f"stream did not end within {MAX_STREAM_CHUNKS} chunks")

        if len(collected) != declared:
            raise StreamError(
                f"stream reassembled to {len(collected)} bytes, not the declared {declared}"
            )
        return bytes(collected)

    def read_slot(self, *, bank: int = 0, slot: int) -> bytes | None:
        """Read the document stored in a slot **without loading it**.

        The panel stays on whatever preset it was showing and the edit buffer
        keeps any uncommitted change, which is what makes this safe to sweep.

        Returns ``None`` when flash holds no document for the slot -- a distinct
        state from a stored preset whose content happens to be the default.
        """
        self._open_presets()

        for attempt in range(READ_RETRIES + 1):
            reply, preamble = self._stream_command(
                op=OP_READ_SLOT, target={KEY_BANK: bank, KEY_SLOT: slot, KEY_TARGET: 2}
            )
            if reply is not None and reply.is_empty_answer:
                logger.debug("Slot %d holds no document", slot)
                return None

            try:
                declared, first = parse_stream_preamble(preamble)
                raw = self._reassemble(declared, first)
            except StreamError as exc:
                logger.warning(
                    "Slot %d stream was malformed (attempt %d/%d): %s",
                    slot,
                    attempt + 1,
                    READ_RETRIES + 1,
                    exc,
                )
            else:
                return raw

            # Drain only. Re-opening the presets resource here would restart the
            # device's paging and desync the rest of the sweep.
            time.sleep(BACKOFF_SECONDS)
            self.drain()

        raise StreamError(
            f"Slot {slot} did not read cleanly after {READ_RETRIES + 1} attempts. "
            "The stream stayed desynced."
        )

    def list_presets(self, *, bank: int = 0) -> bytes:
        """Stream the raw preset listing for a bank.

        Goes through :meth:`_stream_command` rather than :meth:`_command`
        because **the reply to a stream command is itself the first page**.
        Sending it through ``_command`` returned that page as a ``Reply`` and
        then began collecting from page *one*, silently losing the head of the
        listing - on an HX Stomp, the first eight rows, which is every name up
        to slot 8.
        """
        self._open_presets()
        reply, preamble = self._stream_command(
            op=OP_LIST_PRESETS, target={KEY_BANK: bank, KEY_TARGET: 2}
        )
        if reply is not None and reply.is_empty_answer:
            return b""

        # Only the 8-byte stream prefix is dropped, not the envelope behind it:
        # unlike a slot read, this stream's key 104 is an *array* of rows that
        # arrives complete across the pages, so it is returned whole for the
        # caller to unpack. (`parse_stream_preamble` is document-specific - it
        # insists key 104 is a str, which a listing's never is.)
        return preamble[STREAM_PREFIX:] + self.read_stream()

    # -- writes ------------------------------------------------------------

    def push_document(
        self,
        payload: bytes,
        *,
        slot: int,
        name: str,
        bank: int = 0,
        donor: bytes | None = None,
        dry_run: bool = False,
        verify: bool = True,
    ) -> PushResult:
        """Put a document in ``slot``, archiving what was there first.

        The sequence is deliberate and each step earns its place:

        1. **Archive** the target slot. Always, and before anything else -- it is
           the only copy of what is about to be replaced.
        2. **Read**, which is what opens the edit buffer. Guard 3: every ``op 21``
           that has ever succeeded ran on a buffer a read had opened.
        3. **Write** into the edit buffer. Still nothing in flash.
        4. **Commit** with ``op 71``.
        5. **Read back** and compare against what was sent.

        With ``dry_run`` the archive and the read still happen -- they are how the
        donor is obtained -- but nothing is sent and nothing is committed.
        """
        from hlxgen.device.writer import DocumentWriter, encode_save_preset

        # A caller that has already read the slot passes it in: the read is what
        # opens the edit buffer (guard 3), and doing it twice just spends frames
        # against the sequence budget.
        archive = donor if donor is not None else self.read_slot(bank=bank, slot=slot)

        if dry_run:
            return PushResult(
                slot=slot,
                bank=bank,
                archived=archive,
                written=False,
                committed=False,
                verified=False,
                payload_size=len(payload),
                stats=None,
            )

        # Guard 3. The archive read above already opened the edit buffer; this
        # is the explicit statement of the dependency rather than a second read.
        if archive is None:
            raise DeviceError(
                f"Slot {slot} holds no document, so there is no donor and no edit "
                "buffer opened by reading it. Choose a populated slot."
            )

        writer = DocumentWriter(self)
        stats = writer.write_document(payload, slot=slot, bank=bank)

        # Guard 4: only now does anything reach flash. The name rides the commit
        # and is stored NUL-terminated, as HX Edit sends it.
        txn = self.next_txn()
        self._send(
            cmd=CMD_DATA,
            body=encode_command(INNER_OPCODE, encode_save_preset(bank, slot, name, txn)),
        )
        self._receive_matching(lambda f: bool(f.body))

        verified = False
        if verify:
            readback = self.read_slot(bank=bank, slot=slot)
            verified = _same_chain(payload, readback)
            if not verified:
                logger.warning(
                    "Slot %d does not hold the chain that was sent "
                    "(%s bytes back vs %s sent)",
                    slot,
                    len(readback) if readback else 0,
                    len(payload),
                )

        return PushResult(
            slot=slot,
            bank=bank,
            archived=archive,
            written=True,
            committed=True,
            verified=verified,
            payload_size=len(payload),
            stats=stats,
        )


@dataclass(frozen=True)
class PushResult:
    """Outcome of a :meth:`Session.push_document`."""

    slot: int
    bank: int
    #: The document that was in the slot beforehand, or ``None`` if it was empty.
    archived: bytes | None
    written: bool
    committed: bool
    verified: bool
    payload_size: int
    stats: Any


def _is_wellformed(raw: bytes) -> bool:
    """Whether a streamed document parses as a complete record stream.

    This is the guard against a desynced read. The document layer requires the
    records to consume the blob exactly, so a stream that began mid-document --
    or ran long because a non-payload frame got spliced in -- fails here rather
    than decoding into a preset the pedal does not have.
    """
    from hlxgen.device.document import parse

    if not raw:
        return False
    try:
        parse(raw)
    except Exception:  # noqa: BLE001 - any failure to parse means re-read
        return False
    return True


def _same_chain(sent: bytes, readback: bytes | None) -> bool:
    """Whether the device stored the chain we sent.

    **Not a byte comparison.** ``op 21`` makes the device re-derive the document:
    it re-encodes, rebuilds the footswitch layout, and appends the trailing extra
    value a ``Trails`` switch lives in. A stored preset is therefore never
    byte-identical to what was sent, and comparing bytes reports every successful
    write as a failure.

    What must match is the chain: the same models in the same slots, each with a
    value vector the device did not have to invent.
    """
    from hlxgen.device.document import DocumentError, parse

    if not readback:
        return False
    try:
        before, after = parse(sent), parse(readback)
    except DocumentError:
        return False

    def chain(document: Any) -> list[tuple[int | None, int]]:
        return [
            (document.model_index(index), len(document.values(index) or []))
            for index in document.block_slots()
        ]

    sent_chain, stored_chain = chain(before), chain(after)
    if len(sent_chain) != len(stored_chain):
        return False
    # The device may append one trailing value it owns; it may not drop any.
    return all(
        model == stored_model and 0 <= stored_count - count <= 1
        for (model, count), (stored_model, stored_count) in zip(
            sent_chain, stored_chain, strict=True
        )
    )


def _refusal_code(reply: Reply) -> int | None:
    """The device's error number from a refusal payload, ``{111: code}``."""
    payload = reply.payload
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, int):
                return value
    return None


def _reply_from_frame(frame: Frame) -> Reply | None:
    """Pull a decoded reply out of a frame's inner command body."""
    payload = frame.body
    with contextlib.suppress(FrameError):
        # Not every reply body carries the inner command header; the raw body is
        # the right thing to decode when it does not.
        _flag, _opcode, payload = decode_command(frame.body)
    return decode_reply(payload)


def _is_busy(exc: Any) -> bool:
    """Whether a USBError is the "someone else holds it" flavour."""
    errno = getattr(exc, "errno", None)
    if errno in (16, 6):  # EBUSY, and macOS' access-denied
        return True
    text = str(exc).lower()
    return "busy" in text or "access denied" in text or "not permitted" in text
