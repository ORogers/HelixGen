# The USB protocol

How HelixGen talks to an HX device: the transport, the preset document format, the
name-to-ordinal mapping, and how a preset is written. It is a reference for working
on `helixgen/device/`, not a tutorial.

Everything here was measured against a real HX Stomp (fw 3.x) rather than inferred,
and the code it describes is verified end to end: `helixgen push tone.hlx --slot N`
lands a generated preset that survives a power cycle.

**Scope.** macOS only. Windows and Linux are unbuilt, which keeps the
libusb-driver-conflict problem off the critical path. Only the HX Stomp is verified;
other Helix devices share the protocol but nobody has confirmed it.

**This is not MIDI SysEx.** HX Edit does not use the MIDI interface a DAW sees, and
Helix hardware filters SysEx out of MIDI Thru. It is a vendor-specific USB bulk
interface carrying a request/response protocol whose payloads are MessagePack.

---

## 1. Transport

`helixgen/device/usb.py`, `helixgen/device/frames.py`

| Property | Value |
|---|---|
| Vendor ID | `0x0E41` |
| Interface | `MI_00`, vendor-specific, interface number **0** |
| Transfers | USB bulk, IN `0x81` / OUT `0x01` |
| Read buffer | 512 bytes max per transfer |
| Operation timeout | 2000 ms |
| Drain timeout | 50 ms |
| Byte order | little-endian in payloads; **parameter values are big-endian f32** |
| Discipline | strictly one request, one response. Never pipeline. |

Product IDs: HX Stomp `0x4246`, HX Stomp XL `0x4253`, Helix Floor `0x4248`,
Helix LT `0x424a`, Helix Rack `0x4249`, HX Effects `0x4245`, POD Go `0x4247`.

Interface 0 only. Audio is interfaces 1-4 and HID is 5; all are left alone.

### Opening a session

1. Open the device, claim interface 0.
2. **Clear halt** on both `0x01` and `0x81`. A halted endpoint left by an
   interrupted session makes the device ignore everything that follows *silently* -
   no error, no reply, nothing.
3. **Drain** residual bulk data: read repeatedly at 50 ms until it fails. The device
   ignores a new session's init while stale state is pending.
4. Send the five-packet init.
5. Transact.
6. Close politely and release the interface. A dropped handle can leave the pedal's
   front panel locked.

The init runs on the **primary channel alone** (`0x1001` ↔ `0x03EF`) as five packets
sharing one sequence counter. HX Edit instead opens per channel; the device does not
require it.

| # | name | len | seq | cmd | payload |
|---|---|---|---|---|---|
| 1 | HANDSHAKE | 20 | `0x00` | `0x02` | `00 10 00 00`, magic `0x28` |
| 2 | SESSION_OPEN_1 | 28 | `0x02` | `0x04` | inner opcode 2, one selector byte `02` |
| 3 | SESSION_CHUNK_1 | 16 | `0x03` | `0x08` | — |
| 4 | SESSION_OPEN_2 | 36 | `0x04` | `0x04` | `{102: 1000, 100: 254, 101: {}}` |
| 5 | SESSION_CHUNK_2 | 16 | `0x05` | `0x08` | — |

Packet 4 is the `op 254` browse-open, so the init is also the first half of the read
prologue. All five are answered.

### Framing

A 16-byte header. `len` is `8 + significant body`, the body is padded to 4 bytes, and
**the declared length is the authority** when decoding.

Header bytes 12-15 (`arg`) are a running count of *significant* body bytes received,
read like a TCP ACK. Two values must be right or the device stays silent or re-serves
page zero:

- The **handshake** carries a fixed `0x21000100` there, not an offset. Sending `0`
  makes first contact fail with no reply at all.
- A session's counter then starts at **`0x1000`** and advances by each reply's
  declared body length: `0x1000` + 9 → `0x1009`, + 17 → `0x101a`. `Session` tracks it
  centrally rather than letting callers pass one.

### Message envelope

Requests are `{102: txn, 100: op, 101: target}`, where `102` is a u16 counter.
Replies are `{102: txn, 103: status, 104: payload}`; **status `255` means refused**.
Inside a target, block slot is key `98` and a value is key `119`.

**Match every reply by the txn echoed at key 102, never by arrival order.** The
device interleaves keepalives, flow-control credits and leftover stream chunks on the
same channel, and taking one of those for an acknowledgement mis-attributes every
later reply.

A parameter is selected by target key `28` = its index in the model's `Helix.sym`
device order. See [§3](#3-names-to-ordinals).

### Op codes

| Op | Meaning | Notes |
|---|---|---|
| 4 | read the document in a slot | **does not load it** - panel stays put, pending edits survive. 126 presets in ~10.7 s. |
| 5 / 8 | write a document into a slot | **undecoded, deliberately unbuilt.** Writes flash with no edit-buffer step to undo. |
| 16 | empty a slot | **undecoded, deliberately unbuilt.** |
| 20 | select preset | changes device state, loads the preset |
| 21 | whole-preset write | into the **edit buffer**, not flash. ~14 × 496-byte chunks. |
| 28 | delete block at slot | surgical; keeps other footswitch bindings |
| 30 | set value | the parameter edit |
| 33 / 36 | controller reads | `{102, 103: 0, 104: nil}` = nothing assigned |
| 39 | add block | |
| 40 | swap a block's model | error `-306` = model does not fit the DSP budget |
| 41 | bypass | explicit bool at target key 59 - set-state, not a toggle |
| 43 | move block | |
| 71 | **save preset** | commits the edit buffer to a flash slot |
| 76 | open the current edit buffer for a read | leads the non-destructive read sequence |
| 78 | begin a structural edit | sent immediately before a move/add |

An empty answer `{102: txn, 103: 0, 104: nil}` from `op 4` means **flash holds no
document for that slot**. That is distinct from a stored preset whose content is
default, and from a desynced read. Selecting an unpopulated slot makes the firmware
*synthesize* a preset, so a backup that recovers empty slots by selecting them writes
synthesized documents back on restore. `helixgen backup` leaves unpopulated slots out,
as HX Edit does.

---

## 2. The preset document

`helixgen/device/document.py`

A document is three MessagePack values, back to back:

    "l6-helix\0"        magic, a 9-byte fixstr
    <48-byte str>       the header
    {0: ..., 1: ...}    the preset map

**The header is an offset table, not opaque bytes**: twelve little-endian u32s
holding the preset map's start, the byte offset of each of the map's nine top-level
entries, and the total blob length (twice). The device seeks by it, so **any edit to
the map invalidates it**. On the Stomp every one of the twelve classifies cleanly.

### Blocks

Blocks live at `preset[<dsp group>][22]`, a **20-entry slot array** per DSP: index 0
a spacer, 1-8 the first path's blocks, 9 the input, 10 the output, 11-18 the second
path, 19 the routing node. A tone's `@path × 10 + @position + 1` indexes that array
directly.

| what | where |
|---|---|
| model index | block `24 → 25` |
| paired cab | `24 → 26` (`-1` when absent), its values in bank `12` |
| value vector | `11 → 4` |
| enabled | content key `10` |
| type | content key `9` |

### Footswitches

`preset[3][8]`, one entry per physical switch (five on a Stomp). An entry is a
one-element list wrapping a map whose `11` sub-map holds:

| key | meaning |
|---|---|
| `5` | label, NUL-terminated |
| `6` | LED colour |
| `7` | enabled |
| `8` | **the block slot the switch controls** |

**The switch and the block it drives are independent.** The switch is the entry's
*position* in the array; the block is key `8` inside it. They coincide only when
switches happen to be assigned in chain order, which real presets often are not -
HelixGen's own generator assigns drive, modulation and delay first, so a six-block
tone can land as FS1→chorus, FS2→delay, FS3→compressor.

Of 336 footswitch entries read off a pedal, 330 share one shape. The six that differ
carry an extra sub-map at `11 → 9` because they assign a *parameter* rather than a
block's bypass, which HelixGen does not write. A document whose layout is empty - one
rebuilt by writes - has no entry to clone, so that measured shape stands in.

### Snapshots

`preset[10][10]`: `@name` at key `4` (NUL-terminated), `@tempo` at `5`, `@valid` at
`0`, and a block's per-snapshot state at `3[slot][1]`. The snapshot the preset opens
on is the snapshot group's key `6`. The device stores no "custom name" flag: a
snapshot is named or it is not.

### Snapshot-controlled parameters

On/off states alone make every snapshot the same tone with blocks muted. What makes
a snapshot a *sound* is that it recalls its own knob positions, and those are held in
two places at once:

- **The assignment** lives in `preset[4]`, a list indexed by **controller source** -
  1 and 2 are the expression pedals, **9 is Snapshots**. Each entry is
  `{0: id, 1: {0: source, 2: min, 3: max, 5: block slot, 6: {28: sub-model,
  29: parameter ordinal}, 7: sub-model}}`, with the parameter ordinal in the same
  `Helix.sym` index space a set-value uses, and the sub-model selector picking a
  fused cab exactly as it does there. `1: 4` and `13: false` are constant across all
  349 assignments read off a pedal.
- **The values** live in each snapshot's key `2`: 64 entries of
  `[fs_enabled, id, value]`, indexed by the **assignment id**, which is one pool
  shared by every source. An unused entry is `[false, 64, nil]`; stale entries from a
  deleted assignment keep an old value and an id of 13 or 64, and the device treats
  both as unused.

A value has to carry the parameter's own type, as everywhere else on this wire. The
`.hlx` does not say which type that is, and here there is nothing to probe against -
a document is written whole. The **document read back after the edits** settles it:
it holds the block's own value at that ordinal in the device's type, so the range and
every snapshot value are converted to match it.

Assignments the donor held are dropped before the tone's own are written, for the
same reason a footswitch binding is: an assignment names its target by **slot**, so
one left behind drives whatever block the new tone put in that slot.

### Encoding

The device writes **non-canonical** MessagePack - `0` is stored as `d1 00 00` (int16)
where a canonical encoder emits a single `00` - so a re-encode is always a different
length from the original. That does not matter: a semantically identical re-encode,
117 bytes shorter than what came off the pedal, is accepted and stored correctly.

What matters is that **the offset table is recomputed against the bytes actually
emitted**. `dump()` rebuilds it at its fixed length from the classified slots. A
stale table is not cosmetic: the device accepts the transfer, commits it, and stores
an **empty preset**.

---

## 3. Names to ordinals

`helixgen/device/symbols.py`, `helixgen/device/resolve.py`

The device addresses models and parameters by **ordinal**; the catalog names them.
The bridge is `Helix.sym`, which ships inside HX Edit as a JSON array of
`{"symbol": "<device symbol>", "parameters": [...]}`. Array position is the model's
wire identity; the per-symbol list is the value-vector order.

It cannot be folded into `helix_model_information.json`, because the relationship is
one-to-many: the device splits many models into `Mono`/`Stereo` symbols with
**different parameter counts and orders**. Of 153 Mono/Stereo pairs, 110 have Mono as
a strict prefix of Stereo and **43 diverge mid-list** - `Scale` and `Spread` are the
usual culprits, sitting mid-vector and shifting every ordinal after them. Getting the
variant wrong mislabels every parameter past the divergence point.

**`Helix.sym` is Line 6's and is not distributed.** It is read from the user's own HX
Edit install; see [legal.md](legal.md) and `helixgen/device/hxedit.py` for how it is
found. Against a real table: 833 device symbols, 343 catalog models, **343 resolved,
0 unresolved**.

HX Edit's four reference files, for the record:

| File | Holds | In our catalog? |
|---|---|---|
| `Helix.sym` | per-model parameter ordering + model index | **no - this is the gap** |
| `HelixModelDefs.bin` | display names, categories | yes |
| `HelixControls.json` | ranges, value types, enum maps | yes |
| `HX_ModelCatalog.json` | category catalog | yes |

### The mapping

| `.hlx` tone | wire |
|---|---|
| `@model` + `@stereo` | the device symbol's index in `Helix.sym` (block `24 → 25`) |
| named parameters | `11 → 4`, in that symbol's parameter order |
| `@path`, `@position` | slot index = `@path × 10 + @position + 1` |
| `@enabled` | content key `10` |
| `@type` | content key `9` |
| `@mic`, `@trails` | one value appended past the symbol's parameters |
| `footswitch.dspN.blockM` | key `3 → 8`; `@fs_index` − 1 is the array position |
| `@fs_label` / `@fs_ledcolor` / `@fs_enabled` | `11 → 5` (NUL-terminated) / `11 → 6` / `11 → 7` |
| `snapshotN.@name` / `@tempo` / `@valid` | snapshot keys `4` / `5` / `0` |
| `snapshotN.blocks.dspN.<name>` | snapshot key `3[wire slot][1]` |
| `global.@current_snapshot` | snapshot group key `6` |
| `controller.dspN.blockM.<param>` | an assignment in `preset[4][9]` |
| `snapshotN.controllers.….<param>.@value` | snapshot key `2[assignment id][2]` |
| `global.@topologyN` | DSP group key `21` |

Two counter-intuitive points, both load-bearing:

- **`@stereo` is written only when the model has both variants.** An absent
  `@stereo` means *the variant that exists*, not "Mono". Reading it as Mono makes
  stereo-only models - every reverb - unresolvable. Zero of the 343 catalog entries
  carry a variant suffix.
- **An amp+cab block's `@cab` is a sibling reference, not a model.** `@cab: "cab0"`
  names another entry of the same `dspN` object. The pair is one block on the wire.

Parameter-name matching folds out separators and case, because spellings drift
between HX Edit eras (`High Cut` / `HighCut`, `Gate_Range` / `GateRange`). That
normalisation holds across all 343 models.

### How many values a model stores

Not the symbol's parameter count. Getting it wrong makes the device reject the whole
document:

| case | stored | seen |
|---|---|---|
| symbol carries `IrData` | parameters − 1 | 10/10 |
| ordinary model | parameters | 160 |
| model with a trailing extra (`Trails`) | parameters + 1 | 55 |

`IrData` is never stored and is last in **all 92** symbols that carry it, so dropping
it shifts no ordinal. The `+1` group is reverbs, delays and FX loops.

---

## 4. Writing a preset

`helixgen/device/editor.py`, `helixgen/device/writer.py`, `helixgen/device/codec.py`

### The donor

A device preset document is not fully understood: preset key `5` alone holds 86
fields, most undecoded, and the input/output nodes store a "ragged prefix" of the
model's parameter list whose rule nobody has pinned down.

So HelixGen does not synthesise a document. It reads one **the device itself wrote**
from the target slot (`op 4`) and overlays only the fields the `.hlx` determines,
keeping the device's own bytes everywhere else. That base is the **donor**, and it
must come from the target device or at least the same model. Whatever the tone could
not carry is reported per preset rather than guessed at.

This is the same pattern `generate_preset()` uses one level up, deep-copying
`HXTemplate.hlx` and writing blocks into it.

### Why surgical edits, not a whole-document write

Three paths can populate the edit buffer. HelixGen uses **A**:

- **A — surgical edit commands.** `op 20` select → read → per block `op 40` swap,
  `op 30` per value, `op 41` bypass → `op 28` for donor slots the tone does not fill
  → `op 71` commit.
- **B — whole-document write.** `op 21` the document into the edit buffer in
  credit-paced chunks, then `op 71`. Used for the layout phase below and for
  `push --via document`.
- **C — straight to flash.** `op 5` / `op 8` / `op 16`. Undecoded and deliberately
  unbuilt.

A is the path because **`op 21` will not change a block's model.** Isolated on
hardware, one mutation at a time against a known-good donor:

| change | result |
|---|---|
| `@enabled` | accepted |
| value vector | accepted |
| verbatim document (no edit) | accepted |
| semantically identical re-encode | accepted |
| **model index** (with the correct value count) | **rejected - slot left empty** |

Ruled out as the cause: the value count, the block type at key 9 (the same model
legitimately appears with different types, so it is not model-determined), and the
offset table. `op 21` evidently validates more about a model than the block record
carries, and the remaining state is undecoded. `op 40` has no such problem.

**`op 40` resets the block's parameters to the new model's defaults**, which is why
values follow the swap - and why a tone naming only some parameters still lands on
sensible values for the rest.

### The second phase: layout and snapshots

Neither the footswitch layout nor the snapshots is reachable through the edit ops, so
`push` applies them afterwards as a whole-document `op 21` write - which the device
accepts, because by then no model is changing.

It runs in a **fresh session**: the edit run above has spent most of its frame budget
and this is another dozen frames on top of a full read. Snapshot block states and
footswitch bindings are remapped through the slots the blocks actually landed in,
since a binding to `dsp0.block2` has to reach whichever device slot that block
occupies.

### The four guards for `op 21`

Non-negotiable. Each of these was a failure before it was a rule.

1. **Honour the credits.** Each 496-byte data chunk earns an empty `cmd 0x08` frame
   back. Never run more than one chunk ahead; abort if they stop.
2. **Always have a write timeout.** An unbounded bulk write to a device that has
   stopped draining blocks forever - a stalled pedal becomes a hung program.
3. **Open the edit buffer with a read before writing.** Never write straight after a
   `goto`. Every `op 21` write that has succeeded ran on a buffer a read had opened.
4. **Nothing reaches flash until `op 71`.** A bad document write is undone by
   reloading the preset.

The `op 21` and `op 71` bodies:

```
op 21   {102: txn, 100: 21, 101: {110: <blob as msgpack str>}}
op 71   {102: txn, 100: 71, 101: {107: bank, 108: slot, 109: "<name>\0"}}
```

**The document rides inside the op-21 envelope**, at key 110 - it is not a header
announcing a payload sent separately. The credit-paced 496+16 chunking applies to the
whole envelope, blob included. The blob is wrapped as a `str` with **non-minimal
`str16`** framing and is not valid UTF-8, so the envelope is emitted by hand rather
than through a MessagePack library; `writer.encode_write_preset` reproduces the
capture prefix byte-exactly (`83 66 cd 04 c6 64 15 65 81 6e da 0b 9a`). `op 71`
carries the preset name, NUL-terminated.

### Amps carry their own cab

On the pedal an amp and its cab are **one block**: the cab is fused into the amp's
slot as a paired model. That is how a preset with an amp and six effects still fits a
Stomp's eight slots. A tone listing them separately is describing the same thing
twice, so `push` absorbs the cab and closes the chain up behind it.

**Which cab comes from HX Edit's `amp.models`**, not from the tone. Each of its 111
amp entries carries `cablink` (the paired cab) and `ircablink` (the default
standalone IR cab). Checked against 60 amp+cab blocks read off a pedal: 41 use
`cablink` exactly, the other 19 are cabs their owner changed by hand.

Using the amp's default rather than honouring the tone's cab is forced, not a
shortcut:

- **A paired cab is a different model from a standalone one.**
  `HD2_Cab1x12USDeluxe` is `[Distance, LowCut, HighCut, EarlyReflections, Level]`;
  `HD2_CabMicIr_1x12USDeluxe` is `[Mic, Position, Distance, Angle, LowCut, HighCut,
  Level, IrData]`. The amp block owns the mic, so mic, position and angle have
  nowhere to go.
- **Only 20 of the 92 `HD2_CabMicIr_*` symbols have a paired counterpart at all**, so
  honouring the tone's choice would fail for most cabs.

The substitution is audible, so it is reported per preset, along with any cab
parameter the paired model has no home for. `--separate-cabs` keeps the two-slot
layout. A footswitch bound to an absorbed cab is **dropped, not remapped**: a paired
cab cannot be switched on its own, and moving the binding onto the amp would leave
two switches toggling the same block.

`amp.models` is Line 6's, like `Helix.sym`, and is read from the user's own install.

---

## 5. Behaviour that will catch you out

Each of these is measured, and each failed silently before it was understood.

**A stream's first reply carries the document's first 237 bytes.** It looks like a
header worth discarding. It is not:

    00 00 7b 28 86 0a 00 00        stream framing
    83 66 cd 03 eb 67 00 68        {102: txn, 103: status, 104:
    da 0a 7b                       str16, 2683 bytes  <- the declared length
    a9 6c 36 2d 68 65 6c 69 78 00  fixstr(9) "l6-helix\0"  <- the document starts
    da 00 30 3d 00 00 00 ...       str16(48) + the offset table

Dropping it costs the magic *and* the offset table, and what remains still decodes as
plausible MessagePack. A 2683-byte document came back as 2446 bytes that parsed
happily into 34 "records" that were really fragments of one map; a whole 126-slot
backup taken that way looked perfect and was useless. **The declared length at key
104 is the authority.**

**A streamed read must be validated, not trusted.** A desynced stream does not
announce itself - it returns a plausible blob that starts mid-record. Every read is
parsed before it is accepted, the document layer requiring the records to consume the
blob *exactly*, and a failure is drained and retried rather than surfaced. Of one
full sweep, 6 of 126 slots were silently wrong before this check existed.

**A session does not survive the sequence wrap.** The header's sequence byte at
offset 9 rolls over every 256 frames and the device stops reading cleanly across it.
A slot read costs about eleven frames, putting the wrap around every twenty-third
slot - which is exactly where a naive full sweep failed (slots 31, 54, 76, 99, 121,
122). What the device does differently there is **not decoded**; forcing the wrap to
1 instead of 0 moves the symptom without fixing it. `Session.near_sequence_wrap`
reports when the budget is nearly spent, and callers recycle the session. A full
126-slot sweep runs across 8 sessions.

**Applying a tone outlasts a single session.** Two full document reads, a swap plus a
value edit per parameter per block, and a whole-document write runs past the wrap and
kills the device mid-edit. `EditSession` renews before the budget runs out - and
renewing is not just reconnecting, since the edit buffer belongs to the device and is
tied to the loaded preset: the run **commits before letting go**, then re-selects and
re-reads, the read being what reopens the buffer. A four-block tone takes two
sessions.

**Parameter values must be float32, not float64.** The device checks a value's wire
type exactly rather than coercing it, and MessagePack libraries emit Python floats as
`float64` (`cb`) by default where the device wants `float32` (`ca`). With every model
swap succeeding and every value edit refused, one flag - `use_single_float=True` -
took a preset from 0 values applied to 27.

An `.hlx` also does not say which type a parameter *is*: a mic `Angle: 45` is
continuous while `Mic: 3` is an enum, and both are JSON integers. Since a refusal is
cheap and changes nothing, the type is resolved by **trying the plausible wire forms
in order** - float, then int, then bool for 0/1 - rather than guessed from the JSON
type. String enums (`Ratio: "2:1"`) have no wire form of their own and resolve
through the catalog's `reverse_map`. Anything still unresolvable is reported rather
than guessed.

**The assignment count at `preset[10][8]` is load-bearing.** The snapshot group's
key `8` is how many controller assignments the preset holds, across every source; it
matches the count in `preset[4]` on all 126 presets on a real pedal. Leaving it at
`0` produces a preset that looks entirely correct and is not - HX Edit draws the
brackets that mark a parameter as snapshot-controlled, the assignments read back
intact, and the pedal still recalls the same knob positions in every snapshot. The
failure is **visible only in HX Edit**, by switching snapshots and watching a value
that should change and does not. No error, no refusal, and nothing in the read-back
to compare against but another preset.

**`op 41`'s key 59 is *enabled*, not *bypassed*.** The op is called "bypass"
throughout the prior art, and the capture behind it is a bypass press sending
`59: true` then `59: false`, which says nothing about polarity. Measured against a
block's stored `enabled` flag (content key `10`): `59: true` leaves the block **on**,
`59: false` off. Reading the name literally and sending `not @enabled` inverts every
block - the write succeeds, the chain is right, every value is right, and the preset
is silent.

`push` sends the state for **every** block rather than only those whose tone names
`@enabled`, because a model swap leaves the block in whatever state the slot's
previous occupant had.

---

## 6. Module map

| Module | Does |
|---|---|
| `device/frames.py` | The 16-byte framing layer. |
| `device/usb.py` | `Session`: open, clear-halt, drain, init, one synchronous command, the paged stream reader, polite close. Every operation bounded by a timeout. |
| `device/document.py` | The record-stream parser, the offset table, and `dump()`. |
| `device/symbols.py` | Parses `Helix.sym`. Index ↔ symbol, `variants_of()`, and two resolvers: by an explicit `@stereo` flag, and by matching an observed value-vector length (how you pick the variant when reading a preset back). |
| `device/resolve.py` | Joins the catalog onto that table. Wire index, parameter ordinals, and `value_vector()`, which lays named values out in device order and marks unsupplied entries `None` rather than shifting later ones. |
| `device/codec.py` | The donor overlay. |
| `device/editor.py` | `apply_tone`: the surgical edit path, session renewal, and the layout/snapshot phase. |
| `device/writer.py` | The `op 21` transfer with all four guards. |
| `device/amps.py` | `amp.models`: which cab each amp carries. |
| `device/hxedit.py` | Finding the user's HX Edit install. |
| `device/commands.py` | `devices`, `pull`, `backup`, `push` CLI handlers. |

Dependencies: `pyusb` and `msgpack`, plus `libusb` (`brew install libusb`; the `.dmg`
bundles its own).

Tests run against synthetic symbol tables and synthetic documents built to the
documented shape, so the suite needs no proprietary data and no hardware.

---

## 7. Hard rules

- **Firmware, flash and DFU are out of scope and must never be transmitted.** They
  are the only realistic way to brick the unit. Everything else is recoverable.
- **Never probe undecoded ops.** An "accepted" reply from an op the device did not
  act on proves nothing - five ops were observed accepting a body they evidently
  ignored, and one wrong body then wedged a unit. Do not sweep. One op, one body,
  then look at the device.
- **Never write straight after a `goto`.** Read between them.
- **Back up before any write**, and archive the target slot before overwriting it.
- **Do not commit Line 6 data.** `Helix.sym` and its siblings are gitignored and are
  read in place from the user's own HX Edit install. The one distributed file is
  `helixgen/data/helix_model_information.json`, the model catalog: names, ranges,
  defaults and option labels, no code and no algorithms, without which a preset
  cannot be addressed at all. That it ships is a deliberate decision, set out in
  [legal.md](legal.md).
- **Claim interface 0 only.** Leave the audio interface alone.

---

## 8. Not implemented, and not yet confirmed

- **That setting `preset[10][8]` makes the pedal recall snapshot values.** The
  uploaded preset carries the right count (6 of 6 read back), but the pedal was
  disconnected before HX Edit could be reopened on it. Repeat the check the same
  way: push a tone with snapshot values to a scratch slot, quit and reopen HX Edit
  (it holds the USB interface), load the preset from flash, and switch snapshots
  watching a controlled value.
- **Reading a device document back to `.hlx`.** Only the forward direction is built,
  which is all `push` needs.
- **Input/output nodes and routing.** `split`, `join` and `inputA` are left to
  whatever the target slot held; only the chain, the footswitch layout and the
  snapshots are written. Whether the nodes' ragged prefix matters for generated
  presets is untested - it may be safe to leave them to the donor permanently.
- **`op 43` move and `op 39` add-block.** `op 40` already works on an empty slot - a
  6-block tone landed on a 5-block donor - so these matter only past the slot array's
  own length.
- **Byte-identical read-back verification.** The device re-derives the document, so
  it normalises and appends the trailing `Trails` value. The check should be
  *semantic* equality of blocks and values; `push` compares more loosely today.
- **Decoding the sequence rollover.** Stepped around rather than understood. A
  captured HX Edit session running past 256 frames would show what it really does.
- **Captured-transaction fixtures.** The unit tests use synthetic frames, not
  recorded ones.
- **Firmware version recorded in backups**, with a warning on mismatch.
- **Windows and Linux.**
- **Devices other than the HX Stomp.** `op 4`'s behaviour in particular is verified
  there only, and is the first thing to re-check if another device appears.

---

## 9. Prior art

All black-box interoperability work - traffic captured between owned hardware and
licensed software, no Line 6 source, firmware or SDK involved.

- **[fretwire](https://github.com/john-baxter-dev/fretwire)** - the substantive one.
  A ~1,500-line living protocol spec (`docs/protocol.md`), a ~650-line preset-format
  spec with the tone↔wire table (`docs/preset-format.md`), a documented safety model
  (`docs/safety.md`), and working read/write crates. Rust, Apache-2.0/MIT. Much of
  §1-§3 here is distilled from it. It is a living document: check the dated entries,
  since later rounds supersede earlier ones.
- **[openhx](https://github.com/allansomensi/openhx)** - clean transport-level docs
  under `docs/protocol/`. Rust, MIT.
- **[helix_usb](https://github.com/kempline/helix_usb)** - Python on pyusb; useful as
  a shape for the transport layer.

See [application_flow.md](application_flow.md) §5 for where this sits in the
application as a whole.
