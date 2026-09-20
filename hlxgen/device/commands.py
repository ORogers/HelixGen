"""CLI handlers for the USB device commands.

These live apart from ``hlxgen.cli`` so that importing the CLI does not drag in
pyusb: the USB stack is an optional dependency, and every other command works
without it.
"""

from __future__ import annotations

import argparse
import json
import sys

from hlxgen.dataset import ModelCatalog
from hlxgen.device.codec import overlay
from hlxgen.device.document import dump, parse
from hlxgen.device.hxedit import find_symbol_table
from hlxgen.device.symbols import DeviceSymbols


def _load_amp_defaults():
    """Amp-to-default-cab data, or ``None`` if HX Edit is not installed.

    Absence is not fatal: without it an amp simply keeps its cab in a separate
    slot, which is what the tone already describes.
    """
    from hlxgen.device.amps import AmpDataError, AmpDefaults

    try:
        return AmpDefaults.load()
    except AmpDataError as exc:
        print(f"Note: {exc} Amps will keep a separate cab block.", file=sys.stderr)
        return None


def run_devices(args: argparse.Namespace) -> int:
    """List attached HX hardware."""
    from hlxgen.device.usb import DeviceError, Session, find_devices

    try:
        devices = find_devices()
    except DeviceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not devices:
        print("No Line 6 HX device found on USB.")
        return 0

    for info in devices:
        print(info.describe())
        print(f"  vendor 0x{info.vendor_id:04x}  product 0x{info.product_id:04x}")
        if not info.verified:
            print("  NOTE: this model has not been tested with hlxgen's USB support.")

        if not args.identify:
            continue
        try:
            with Session.open(info) as session:
                print(f"  control session opened on interface 0 (arg 0x{session._state.arg:04x})")
        except DeviceError as exc:
            print(f"  could not open a control session: {exc}")
    return 0


def run_pull(args: argparse.Namespace) -> int:
    """Read one slot off the device without loading it."""
    from hlxgen.device.usb import DeviceError, Session

    try:
        with Session.open() as session:
            document = session.read_slot(bank=args.bank, slot=args.slot)
    except DeviceError as exc:
        print(f"USB error: {exc}", file=sys.stderr)
        return 1

    if document is None:
        print(f"Slot {args.slot} is unpopulated -- flash holds no document for it.")
        return 0

    args.output.write_bytes(document)
    print(f"Read slot {args.slot}: {len(document)} bytes -> {args.output}")
    return 0


def run_backup(args: argparse.Namespace) -> int:
    """Sweep every slot into a directory.

    Unpopulated slots are left out of the backup, as HX Edit does. Recovering
    them by selecting each one makes the firmware *synthesize* a preset, and a
    restore would then write those synthesized documents into flash -- turning
    slots Line 6's own tool reports as empty into populated ones.

    **The session is recycled as it approaches the sequence wrap.** The header's
    sequence byte rolls over every 256 frames and the device does not read
    cleanly across the rollover -- a sweep spends about eleven frames per preset,
    so a single session dies around the twenty-third slot. Closing and reopening
    is cheap (a five-packet init) and sidesteps the problem entirely.
    """
    from hlxgen.device.usb import DeviceError, Session

    args.output.mkdir(parents=True, exist_ok=True)
    stored = 0
    unpopulated: list[int] = []
    failed: list[int] = []
    sessions = 1

    session = Session.open()
    try:
        print(f"Backing up {session.info.describe()} to {args.output}")
        for slot in range(args.count):
            if session.near_sequence_wrap:
                session.close()
                session = Session.open()
                sessions += 1

            try:
                document = session.read_slot(bank=args.bank, slot=slot)
            except DeviceError as exc:
                # One bad slot must not end a sweep that has read correctly up
                # to here. Recycle and give the slot one more chance.
                print(f"  slot {slot:3}: {exc}; retrying on a fresh session")
                session.close()
                session = Session.open()
                sessions += 1
                try:
                    document = session.read_slot(bank=args.bank, slot=slot)
                except DeviceError as retry_exc:
                    print(f"  slot {slot:3}: FAILED -- {retry_exc}", file=sys.stderr)
                    failed.append(slot)
                    continue

            if document is None:
                unpopulated.append(slot)
                continue
            (args.output / f"slot-{slot:03d}.msgpack").write_bytes(document)
            stored += 1
            print(f"  slot {slot:3}: {len(document):5} bytes")
    finally:
        session.close()

    print(
        f"\n{stored} preset(s) stored, {len(unpopulated)} unpopulated, "
        f"{len(failed)} failed, across {sessions} session(s)."
    )
    if unpopulated:
        print(f"  unpopulated: {', '.join(str(s) for s in unpopulated)}")
    if failed:
        print(f"  FAILED: {', '.join(str(s) for s in failed)}", file=sys.stderr)
        return 1
    return 0


def run_push(args: argparse.Namespace) -> int:
    """Put an ``.hlx`` preset into a chosen slot.

    ``--slot`` is required and has no default. There is deliberately no "first
    free slot" guessing: the command overwrites what is there, so the target is
    always the caller's explicit choice.

    Two transports, and the default is the one that works for generated presets:

    * **edits** (default) -- surgical ``op 40``/``op 30``/``op 41``, then the
      footswitch layout and snapshots as a document write. The only path that can
      change a block's **model**.
    * **document** -- a whole-document ``op 21`` write. Touches the chain as one
      transaction, but the device rejects any model change made this way, so it
      suits restoring a document rather than applying a tone.
    """
    from hlxgen.device.editor import EditError, apply_tone
    from hlxgen.device.usb import DeviceError, Session
    from hlxgen.device.writer import WriteAbortedError

    preset = json.loads(args.preset.read_text())
    symbols = DeviceSymbols.load(find_symbol_table(args.symbols))
    name = preset.get("data", {}).get("meta", {}).get("name") or args.preset.stem

    try:
        with Session.open() as session:
            print(f"Device: {session.info.describe()}")

            donor_raw = session.read_slot(bank=args.bank, slot=args.slot)
            if donor_raw is None:
                print(
                    f"Slot {args.slot} is unpopulated, so there is nothing to edit. "
                    "Choose a slot that already holds a preset.",
                    file=sys.stderr,
                )
                return 1

            if args.archive:
                args.archive.write_bytes(donor_raw)
                print(f"Archived slot {args.slot} to {args.archive}")

            if args.dry_run:
                donor = parse(donor_raw)
                report = overlay(donor, preset, symbols)
                print(report.summary())
                if args.via == "document" and args.output:
                    args.output.write_bytes(dump(donor))
                    print(f"Dry run -- wrote the blob to {args.output}, sent nothing.")
                else:
                    print("\nDry run -- nothing sent to the device.")
                return 0

            if args.via == "document":
                donor = parse(donor_raw)
                report = overlay(donor, preset, symbols)
                print(report.summary())
                payload = dump(donor)
                print(f"\nDocument: {len(donor_raw)} -> {len(payload)} bytes")

                result = session.push_document(
                    payload,
                    slot=args.slot,
                    name=name,
                    bank=args.bank,
                    donor=donor_raw,
                    verify=not args.no_verify,
                )
                print(f"\nWrote {result.payload_size} bytes to slot {result.slot}.")
                if not args.no_verify and not result.verified:
                    print(
                        "WARNING: the device does not hold the chain that was sent.",
                        file=sys.stderr,
                    )
                    return 1
                return 0

        # apply_tone opens and renews its own sessions -- a tone costs more frames
        # than one session survives -- so it runs outside the block above.
        edits = apply_tone(
            preset,
            symbols,
            bank=args.bank,
            slot=args.slot,
            name=name,
            catalog=ModelCatalog(args.dataset),
            amps=_load_amp_defaults(),
            fuse_cabs=not args.separate_cabs,
        )
        print(edits.summary())
    except (EditError, WriteAbortedError) as exc:
        print(f"\nUpload failed: {exc}", file=sys.stderr)
        return 1
    except DeviceError as exc:
        print(f"USB error: {exc}", file=sys.stderr)
        return 1

    print(f"\nUploaded {args.preset.name} to slot {args.slot} ({edits.sessions} session(s)).")
    return 0
