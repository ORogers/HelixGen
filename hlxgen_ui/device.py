"""The UI's view of the pedal, built on ``hlxgen.device`` - the real,
hardware-verified USB implementation that backs ``hlxgen devices/pull/push``.

Nothing here reimplements the protocol. It only:

* collapses "no device / busy / no libusb" into states a panel can render,
* turns a slot sweep into rows a list widget can show, and
* runs the same upload path ``hlxgen push`` uses (:func:`apply_tone`).

Every call in this module is safe to run off the UI thread and none of them
touch Qt, so the workers in :mod:`hlxgen_ui.workers` can call them directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("hlxgen_ui.device")

# Clicking a slot should feel immediate, and three of the four costs in a slot
# read never change between clicks: the symbol table, the model catalog, and
# the bank's name listing. Only the document itself has to come off the wire
# each time. The listing is dropped after an upload, because that is the one
# thing that renames a slot.
_SYMBOLS_CACHE: list[Any] = []
_CATALOG_CACHE: dict[str, Any] = {}
_LISTING_CACHE: dict[int, dict[int, str]] = {}


def invalidate_caches(*, listing: bool = True) -> None:
    """Forget what an upload may have changed."""
    if listing:
        _LISTING_CACHE.clear()


def _cached_catalog(dataset: Path) -> Any:
    from hlxgen.dataset import ModelCatalog

    key = str(dataset)
    if key not in _CATALOG_CACHE:
        _CATALOG_CACHE[key] = ModelCatalog(dataset)
    return _CATALOG_CACHE[key]

#: How many slots the UI sweeps by default. A full HX Stomp bank is 126, but a
#: sweep costs a document read per slot, so the panel offers a useful prefix
#: rather than making the user wait for the whole pedal on every refresh.
DEFAULT_SLOT_COUNT = 16


@dataclass(frozen=True)
class SlotSummary:
    """One row of the slot list."""

    index: int
    populated: bool
    #: The preset's stored name, when the device's listing gives us one. The
    #: pedal only carries a name *outbound* on the op-71 commit; a slot read
    #: returns a document with no decoded name field, so this is ``None``
    #: whenever the listing could not be read or understood.
    name: str | None = None
    #: A short description of what is in the slot ("5 blocks"), derived from
    #: the document itself, which always reads.
    detail: str | None = None

    def label(self) -> str:
        if not self.populated:
            return f"{self.index:>3}   (empty)"
        title = self.name or self.detail or "preset"
        return f"{self.index:>3}   {title}"


@dataclass(frozen=True)
class DeviceSummary:
    """What enumeration alone can say about an attached pedal."""

    description: str
    verified: bool


class DeviceUnavailable(RuntimeError):
    """No usable pedal right now - unplugged, busy, or no libusb.

    Carries a message already phrased for a person, so panels can show
    ``str(exc)`` directly.
    """


def _device_error() -> type[Exception]:
    from hlxgen.device.usb import DeviceError

    return DeviceError


def find_device() -> DeviceSummary | None:
    """The first attached pedal, or ``None`` when there is none.

    Raises :class:`DeviceUnavailable` only for a broken USB stack (no pyusb,
    no libusb) - an absent pedal is a normal state, not an error.
    """
    try:
        from hlxgen.device.usb import find_devices
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise DeviceUnavailable(str(exc)) from exc

    try:
        devices = find_devices()
    except Exception as exc:
        raise DeviceUnavailable(str(exc)) from exc

    if not devices:
        return None
    info = devices[0]
    return DeviceSummary(description=info.describe(), verified=info.verified)


def read_slots(
    count: int = DEFAULT_SLOT_COUNT,
    *,
    bank: int = 0,
    on_progress: Callable[[str], None] | None = None,
) -> list[SlotSummary]:
    """Sweep ``count`` slots and describe what each one holds.

    Uses :meth:`hlxgen.device.usb.Session.read_slot`, which reads a slot's
    document *without loading it* - the panel on the pedal does not move and
    an uncommitted edit survives.
    """
    device_error = _device_error()
    from hlxgen.device.usb import Session

    # Refresh means "go and look again", so it must not answer from a cache:
    # a preset renamed or saved on the pedal itself would otherwise keep its
    # old name in the list until the app was restarted.
    invalidate_caches()

    def _report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    try:
        session = Session.open()
    except Exception as exc:
        raise DeviceUnavailable(str(exc)) from exc

    rows: list[SlotSummary] = []
    try:
        names = _listing_names(session, bank=bank)
        for slot in range(count):
            # A sweep spends about eleven frames per preset and the header's
            # sequence byte wraps every 256, which the device does not read
            # cleanly across - so recycle the session before it gets there,
            # exactly as `hlxgen backup` does.
            if session.near_sequence_wrap:
                session.close()
                session = Session.open()

            _report(f"Reading slot {slot}...")
            try:
                document = session.read_slot(bank=bank, slot=slot)
            except device_error as exc:
                logger.info("Slot %d did not read: %s", slot, exc)
                rows.append(SlotSummary(index=slot, populated=False, detail="unreadable"))
                continue

            if document is None:
                rows.append(SlotSummary(index=slot, populated=False))
                continue
            rows.append(
                SlotSummary(
                    index=slot,
                    populated=True,
                    name=names.get(slot),
                    detail=_describe_document(document),
                )
            )
    finally:
        session.close()
    return rows


def _describe_document(document: bytes) -> str | None:
    """"5 blocks", or ``None`` if the document will not parse."""
    try:
        from hlxgen.device.document import parse

        return f"{len(parse(document).blocks(0))} blocks"
    except Exception:  # noqa: BLE001 - a summary is a nicety, never a failure
        return None


def _listing_names(session: Any, *, bank: int) -> dict[int, str]:
    """Preset names from the bank listing, keyed by slot.

    ``Session.list_presets`` streams the listing the pedal's own browser uses,
    but nothing in this project decodes it yet, so this reads it defensively
    and returns ``{}`` the moment anything is not the shape it expects. The
    slot list stays useful either way - it just falls back to describing each
    document instead of naming it.
    """
    if bank in _LISTING_CACHE:
        return _LISTING_CACHE[bank]

    try:
        raw = session.list_presets(bank=bank)
    except Exception as exc:  # noqa: BLE001 - optional enrichment only
        logger.info("Preset listing unavailable: %s", exc)
        return {}

    names = parse_listing_names(raw)
    if names:
        _LISTING_CACHE[bank] = names
    return names


#: Key 109 carries a preset's name, both on the op-71 commit and in the rows
#: the device streams back for a bank listing.
_KEY_NAME = 109
#: The reply envelope's payload key - the listing's rows live here.
_KEY_PAYLOAD = 104


def parse_listing_names(raw: bytes) -> dict[int, str]:
    """Slot number -> preset name, from a raw bank listing.

    The listing is a run of one-entry maps, ``{<slot>: {109: "<name>", ...}}``,
    concatenated rather than wrapped in an array - so it is decoded with a
    streaming ``Unpacker`` instead of a single ``unpackb``, which would reject
    the whole thing as "extra data". **The key is the absolute slot number**,
    so no positional counting is involved and a dropped row shifts nothing.

    Names come back NUL-padded and truncated to the device's own 16-character
    field. Anything that does not decode is skipped: a missing name costs a
    row its label, never the sweep.
    """
    import msgpack

    rows: Any = None
    try:
        envelope = msgpack.unpackb(raw, raw=False, strict_map_key=False)
    except Exception as exc:  # noqa: BLE001 - fall through to the tolerant walk
        logger.info("Listing did not unpack as one value: %s", exc)
    else:
        rows = envelope.get(_KEY_PAYLOAD) if isinstance(envelope, dict) else envelope

    if rows is None:
        # A truncated or head-clipped stream still yields whole rows to a
        # streaming unpacker, so a partial listing names what it can rather
        # than nothing at all.
        rows = []
        unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
        unpacker.feed(raw)
        while True:
            try:
                rows.append(next(unpacker))
            except StopIteration:
                break
            except Exception:  # noqa: BLE001 - a malformed tail ends the walk
                break

    names: dict[int, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or len(row) != 1:
            continue
        ((slot, entry),) = row.items()
        if not isinstance(slot, int) or not isinstance(entry, dict):
            continue
        name = _as_text(entry.get(_KEY_NAME))
        if name:
            names[slot] = name
    return names


def _as_text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.split(b"\x00", 1)[0].decode("ascii", "replace")
    if not isinstance(value, str):
        return None
    cleaned = value.split("\x00", 1)[0].strip()
    return cleaned or None


def push_preset(
    preset: dict[str, Any],
    *,
    slot: int,
    bank: int = 0,
    dataset: Path,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """Put a generated preset into ``slot`` over USB.

    This is the same path ``hlxgen push`` takes by default: surgical edits via
    :func:`hlxgen.device.editor.apply_tone`, which is the only transport that
    can change a block's model. ``apply_tone`` opens and renews its own
    sessions, so no session is held here.

    Returns the report ``hlxgen push`` prints, for display in the UI.
    """

    def _report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    from hlxgen.device.commands import find_symbol_table
    from hlxgen.device.editor import apply_tone
    from hlxgen.device.symbols import DeviceSymbols

    _report("Locating HX Edit's symbol table...")
    symbols = DeviceSymbols.load(find_symbol_table(None))

    _report("Loading the model catalog...")
    catalog = _cached_catalog(dataset)

    name = preset.get("data", {}).get("meta", {}).get("name") or "hlxgen"
    _report(f"Applying '{name}' to slot {slot}...")
    invalidate_caches()
    report = apply_tone(
        preset,
        symbols,
        bank=bank,
        slot=slot,
        name=name,
        catalog=catalog,
        amps=_amp_defaults(),
    )
    return report.summary()


def _amp_defaults() -> Any:
    """Amp-to-default-cab data, or ``None`` when HX Edit is not installed.

    Absence is not fatal - an amp simply keeps its cab in a separate slot.
    """
    try:
        from hlxgen.device.amps import AmpDataError, AmpDefaults

        try:
            return AmpDefaults.load()
        except AmpDataError as exc:
            logger.info("Amp defaults unavailable: %s", exc)
            return None
    except ImportError:  # pragma: no cover - depends on the install
        return None


@dataclass(frozen=True)
class ChainBlock:
    """One block in a signal chain, as the preview draws it."""

    position: int
    name: str
    category: str | None = None
    enabled: bool = True
    #: True when this is a cab the device fuses into the amp before it, which
    #: is one block on the wire but two things in the chain a player hears.
    fused_cab: bool = False


@dataclass(frozen=True)
class SlotChain:
    """What a slot holds: its name, and the chain in signal order."""

    slot: int
    name: str | None
    blocks: list[ChainBlock]


def chain_from_preset(preset: dict[str, Any], catalog: Any) -> list[ChainBlock]:
    """The chain of a generated ``.hlx`` preset, in signal order."""
    from hlxgen.dataset import ModelCatalogError

    dsp0 = preset.get("data", {}).get("tone", {}).get("dsp0", {})
    blocks: list[ChainBlock] = []
    for key, block in dsp0.items():
        if key in {"inputA", "inputB", "outputA", "outputB", "split", "join"}:
            continue
        if not isinstance(block, dict) or "@model" not in block:
            continue
        internal = block.get("@model", "")
        try:
            model = catalog.get(internal)
            name, category = model.display_name, model.category
        except ModelCatalogError:
            name, category = internal, None
        position = block.get("@position")
        blocks.append(
            ChainBlock(
                position=position if isinstance(position, int) else len(blocks),
                name=name,
                category=category,
                enabled=bool(block.get("@enabled", True)),
            )
        )
    blocks.sort(key=lambda b: b.position)
    return blocks


def read_slot_chain(slot: int, *, bank: int = 0, dataset: Path) -> SlotChain:
    """Read one slot and decode its blocks into names a person recognises.

    Read-only: :meth:`Session.read_slot` does not load the slot, so looking at
    a preset in the UI never moves the pedal's panel.

    The device addresses models by ordinal, so this needs ``Helix.sym`` from
    the user's own HX Edit install to name them. Without it the chain is still
    shown, just with ordinals instead of names - the shape of the preset is
    worth seeing either way.
    """
    from hlxgen.device.document import (
        KEY_CAB_INDEX,
        KEY_ENABLED,
        KEY_MODEL_INDEX,
        KEY_MODEL_REF,
        parse,
    )
    from hlxgen.device.usb import Session

    try:
        session = Session.open()
    except Exception as exc:
        raise DeviceUnavailable(str(exc)) from exc

    try:
        names = _listing_names(session, bank=bank)
        document = session.read_slot(bank=bank, slot=slot)
    except Exception as exc:
        raise DeviceUnavailable(str(exc)) from exc
    finally:
        session.close()

    if document is None:
        return SlotChain(slot=slot, name=names.get(slot), blocks=[])

    resolver = _NameResolver(_cached_catalog(dataset))
    blocks: list[ChainBlock] = []
    for index, content in parse(document).blocks(0):
        ref = content.get(KEY_MODEL_REF) or {}
        name, category = resolver.name_for(ref.get(KEY_MODEL_INDEX))
        blocks.append(
            ChainBlock(
                position=index,
                name=name,
                category=category,
                enabled=bool(content.get(KEY_ENABLED, True)),
            )
        )
        cab_index = ref.get(KEY_CAB_INDEX, -1)
        if cab_index not in (-1, None):
            cab_name, cab_category = resolver.name_for(cab_index)
            blocks.append(
                ChainBlock(
                    position=index,
                    name=cab_name,
                    category=cab_category,
                    enabled=bool(content.get(KEY_ENABLED, True)),
                    fused_cab=True,
                )
            )
    return SlotChain(slot=slot, name=names.get(slot), blocks=blocks)


class _NameResolver:
    """Device ordinal -> catalog display name, via ``Helix.sym``."""

    def __init__(self, catalog: Any) -> None:
        self._catalog = catalog
        self._symbols = self._load_symbols()

    @staticmethod
    def _load_symbols() -> Any:
        if _SYMBOLS_CACHE:
            return _SYMBOLS_CACHE[0]
        try:
            from hlxgen.device.commands import find_symbol_table
            from hlxgen.device.symbols import DeviceSymbols

            symbols = DeviceSymbols.load(find_symbol_table(None))
        except Exception as exc:  # noqa: BLE001 - naming is a nicety
            logger.info("Symbol table unavailable, blocks stay unnamed: %s", exc)
            return None
        _SYMBOLS_CACHE.append(symbols)
        return symbols

    def name_for(self, index: Any) -> tuple[str, str | None]:
        from hlxgen.dataset import ModelCatalogError

        if not isinstance(index, int):
            return "unknown block", None
        if self._symbols is None:
            return f"model #{index}", None
        symbol = self._symbols.by_index(index)
        if symbol is None:
            return f"model #{index}", None

        host = symbol.symbol
        for suffix in ("Mono", "Stereo"):
            if host.endswith(suffix):
                host = host[: -len(suffix)]
                break
        try:
            model = self._catalog.get(host)
        except ModelCatalogError:
            return symbol.symbol, None
        return model.display_name, model.category
