# Direct USB preset upload — plan, research and TODO

Status: **working end to end on an HX Stomp.** `hlxgen generate --upload --slot N`
and `hlxgen push tone.hlx --slot N` put a generated preset on the pedal; it
survives a power cycle and reads back byte-identical.

Phases 0-4 are done. The upload uses **path A** (surgical `op 40`/`op 30`/`op 41`
edits), not path B — §3 chose B, and the device turned out to reject any model
change made that way. The corrections that mattered are recorded in §5; what is
left is in §6.

This document is a handoff. It carries everything learned so far about talking to an
HX Stomp over USB, the approach we settled on and why, what is already in the
repository, and what remains. It should be enough to resume without redoing the
research.

Scope decision from the repo owner: **macOS only for now.** Windows and Linux
support are explicitly deferred, which removes the whole libusb-driver-conflict
problem from the critical path.

---

## 1. Why

`hlxgen describe --upload` currently drives HX Edit's GUI with AppleScript
(`scripts/import_helix_preset.applescript`): keystrokes, fixed `delay` statements,
roughly twelve seconds, no error signal, and in `auto` mode it silently overwrites
preset slot 1. It is macOS-only, it requires HX Edit to be installed and frontmost,
and it cannot read anything back.

Replacing it with a USB client makes uploads verifiable, lets us choose the target
slot, and — the part that is new capability rather than a like-for-like replacement
— lets us **read** presets off the pedal.

---

## 2. What the protocol actually is

The single most important correction to any prior assumption: **this is not MIDI
SysEx.** HX Edit does not use the MIDI interface a DAW sees. Helix hardware also
filters SysEx out of MIDI Thru, so that was never the transfer path.

It is a **vendor-specific USB bulk interface** on the same physical device, carrying
an application-level request/response protocol whose payloads are **MessagePack**.

### Transport facts

| Property | Value |
|---|---|
| Vendor ID | `0x0E41` |
| Interface | `MI_00`, vendor-specific, interface number **0** |
| Transfers | USB bulk, IN and OUT |
| Read buffer | 512 bytes max per transfer |
| Operation timeout | 2000 ms |
| Drain timeout | 50 ms |
| Byte order | little-endian (multi-byte integers in payloads) |
| Discipline | **strictly one request, one response.** Never pipeline. |

Product IDs: HX Stomp `0x4246`, HX Stomp XL `0x4253`, Helix Floor `0x4248`,
Helix LT `0x424a`, Helix Rack `0x4249`, HX Effects `0x4245`, POD Go `0x4247`.

### Session shape

1. Open the device, claim interface 0. Leave the audio and MIDI interfaces alone.
2. **Clear halt** on both `0x01` and `0x81`. A halted endpoint from an interrupted
   session makes the device ignore everything that follows, *silently* — no error,
   no reply, just nothing.
3. **Drain** residual bulk data — read repeatedly at 50 ms until it fails. The
   device ignores a new session's init packets while stale state is pending.
4. **Five-packet init** (see the correction below).
5. Transact.
6. Release the interface.

### Correction: the handshake is linear, not per-channel [verified live, 2026-09-17]

An earlier draft of this section described the handshake as a per-channel
`SESSION_OPEN` in the order `ef03` → `ed03` → `f003` with an identity query on
each. **That is what HX Edit does, and it is not what the device requires.** The
whole init runs on the **primary channel alone** (`0x1001` ↔ `0x03EF`) as five
packets with one sequence counter:

| # | name | len | seq | cmd | payload |
|---|---|---|---|---|---|
| 1 | HANDSHAKE | 20 | `0x00` | `0x02` | `00 10 00 00`, magic `0x28` |
| 2 | SESSION_OPEN_1 | 28 | `0x02` | `0x04` | inner opcode 2, one selector byte `02` |
| 3 | SESSION_CHUNK_1 | 16 | `0x03` | `0x08` | — |
| 4 | SESSION_OPEN_2 | 36 | `0x04` | `0x04` | `{102: 1000, 100: 254, 101: {}}` |
| 5 | SESSION_CHUNK_2 | 16 | `0x05` | `0x08` | — |

Packet 4 is the `op 254` browse-open, so the init *is* the first half of the read
prologue. All five are answered.

### The `arg` field is not zero-based [verified live]

Header bytes 12–15 are the running count of *significant* body bytes received.
Two values have to be right or the device stays silent or re-serves page zero:

* The **handshake** carries a fixed `0x21000100` in that slot, not an offset.
  Sending `0` there is what made first contact fail with no reply at all.
* A session's counter then starts at **`0x1000`** and advances by each reply's
  declared body length: `0x1000` + 9 → `0x1009`, + 17 → `0x101a`. Those are
  exactly the constants the prior art hardcodes, derived rather than copied.

### Message envelope

Requests are `{102: txn, 100: op, 101: target}` where `102` is a u16 counter.
Replies are `{102: txn, 103: status, 104: payload}`; **status `255` means refused**.
Inside a target, block slot is key `98` and a value is key `119`.

Parameter values on the wire are **big-endian f32**.

A parameter is selected by target key `28` = **its index in the model's `Helix.sym`
device order** — which is why §4 exists.

### Op codes worth knowing

| Op | Meaning | Notes |
|---|---|---|
| 4 | read the document stored in a slot | **does not load it** — panel stays put, pending edits survive. 126 presets in ~10.7 s. |
| 5 / 8 | write a document into a slot | **undecoded.** Persistent. Out of scope. |
| 16 | empty a slot | **undecoded.** Out of scope. |
| 20 | select preset | changes device state, loads the preset |
| 21 | whole-preset write | into the **edit buffer**, not flash. ~14 × 496-byte chunks. |
| 28 | delete block at slot | surgical; keeps other footswitch bindings |
| 30 | set value | the parameter edit |
| 33 / 36 | controller reads | `{102, 103: 0, 104: nil}` = nothing assigned |
| 39 | add block | |
| 40 | swap a block's model | error `-306` = model does not fit the DSP budget |
| 41 | bypass | **explicit bool at target key 59** — set-state, not a toggle |
| 43 | move block | |
| 71 | **save preset** | commits the edit buffer to a flash slot |
| 76 | open the current edit buffer for a read | leads the non-destructive read sequence |
| 78 | begin a structural edit | sent immediately before a move/add |

An empty answer `{102: txn, 103: 0, 104: nil}` from op 4 means **flash holds no
document for that slot** — distinct from a stored preset whose content is default,
and distinct from a desynced read. Selecting an unpopulated slot makes the firmware
*synthesize* a preset, so a backup that recovers empty slots by selecting them will
write synthesized documents back on restore. Leave unpopulated slots out of a backup
file, as HX Edit does.

---

## 3. Approach: which write path

Three paths can put a preset on the pedal. All of them are reached through the same
envelope; they differ in how the edit buffer gets populated.

**A — surgical edit commands.** `op 78` begin → `op 39` add → `op 40` swap →
`op 30` set-value ×N → `op 41` bypass → `op 71` save. Verified live; byte-exact
command builders exist in the prior art. Needs no donor, because the device already
holds a valid document and you mutate it in place.

**B — whole-document write.** Build the document, `op 21` it into the edit buffer in
credit-paced chunks, `op 71` to commit. This is what HX Edit sends for structural
changes and what a restore uses.

**C — straight to slot.** `op 5` / `op 8` / `op 16`. Writes flash directly with no
edit-buffer step to undo. **Undecoded and deliberately unbuilt. Do not attempt.**

### We chose B, and this reversed an earlier decision

An earlier draft of this plan chose A on safety grounds, citing a July 2026 report
that `op 21` "reproducibly wedges the pedal, cause unknown". **That reading was
wrong** — it was one entry in a running log, and later rounds of the same
investigation found the mechanism. It was the *client's* bug, not the pedal's:

- The transport treated the device's flow-control credits as noise and had **no
  write timeout at all**, so once the device stopped draining its OUT endpoint the
  host blocked forever.
- A separate, deterministic stall at 512 of 2230 bytes turned out to be a write
  issued straight after a `goto`. Every `op 21` write that has ever succeeded ran on
  an edit buffer that a **read** had opened first.

Both are fixed upstream; `op 21` is implemented and verified live, and a full preset
restore reads back byte-identical from flash.

B suits this project better. We generate a complete preset in one go — we are not
nudging one knob on something already loaded. B puts exactly that preset on the
pedal in one transaction. A would mean decomposing a finished preset back into a
command sequence, and some of it does not decompose: the input and output nodes, and
some switch/transport parameters, have no decoded edit form.

Path A remains worth building **later**, for live tweaking while the pedal is
plugged in and playing — something the file-based workflow cannot do at all.

### The four guards for `op 21` — non-negotiable

1. **Honour the credits.** Each 496-byte data chunk earns an empty `cmd 0x08` frame
   back. Never run more than one chunk ahead; abort if they stop.
2. **Always have a write timeout.** An unbounded bulk write to a device that has
   stopped draining blocks forever. This is what turned a stalled pedal into a hung
   program.
3. **Open the edit buffer with a read before writing.** Never write straight after a
   `goto`.
4. **Nothing reaches flash until `op 71`.** A bad document write is undone by
   reloading the preset.

Match every ACK **by the txn echoed at key 102**, never by arrival order — credit
frames otherwise get consumed as acknowledgements, which silently corrupts the
correlation. (Upstream measured `op 71` at 0 of 21 correctly correlated before they
fixed this.)

---

## 4. The donor, and the name↔ordinal problem

### Why a donor is needed

A device preset document is not fully understood. Preset key `5` alone holds **86
fields, most undecoded**; there is also key `2`, a device-info stamp at key `7`, and
the input/output nodes, whose stored parameter vectors are a "ragged prefix" of the
model's list that nobody has pinned down the rule for.

So we do not synthesise a document. We take one **the device itself wrote** — read
from the target with `op 4` — and overlay only the fields our `.hlx` determines,
keeping the device's own bytes everywhere else. That base is the **donor**. It must
come from the target device or at least the same model, because slot geometry and
those undecoded fields are device-specific. Report per preset whatever the tone could
not carry rather than guessing.

`generate_preset()` already does exactly this one level up: it deep-copies
`HXTemplate.hlx` and writes blocks into it. Same pattern, same reason.

### The host ↔ wire mapping

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
  stereo-only models (every reverb) unresolvable.
- **An amp+cab block's `@cab` is a sibling reference, not a model.** `@cab: "cab0"`
  names another entry of the same `dspN` object. The pair is one block on the wire,
  cab at `24 → 26`, its parameters in bank `12`.

### `Helix.sym` — the file we do not have

The device addresses models and parameters by **ordinal**. Our catalog names them.
The bridge is `Helix.sym`, which ships inside HX Edit as a JSON array of
`{"symbol": "<device symbol>", "parameters": [...]}`. Array position is the model's
wire identity; the per-symbol list is the value-vector order.

It cannot be folded into `helix_model_information.json`, because the relationship is
one-to-many: the device splits many models into `Mono`/`Stereo` symbols with
**different parameter counts and orders**, and a stereo-only parameter sitting
mid-list shifts every ordinal after it. (Checked: zero of our 343 catalog entries
carry a variant suffix.)

**We do not ship this file.** The pattern the ecosystem settled on is Line 6 → user →
tool: import it from the user's own HX Edit install and cache it locally.
`.gitignore` already covers `Helix.sym` and its siblings.

HX Edit's four reference files, for the record:

| File | Holds | In our catalog? |
|---|---|---|
| `Helix.sym` | per-model parameter ordering + model index | **no — this is the gap** |
| `HelixModelDefs.bin` | display names, categories | yes |
| `HelixControls.json` | ranges, value types, enum maps | yes |
| `HX_ModelCatalog.json` | category catalog | yes |

Find it with:

```bash
find /Applications -name Helix.sym
```

On the machine this was built against it is at
`/Applications/Line6/HX Edit.app/Contents/Resources/Helix.sym` — note the `Line6`
folder, which the installer has not always used. `hlxgen push` checks both known
locations before asking for `--symbols`.

---

## 5. What already exists

Landed in PR #4, all offline, no hardware needed:

- **`hlxgen/device/symbols.py`** — parses `Helix.sym`. Index ↔ symbol lookup,
  `variants_of()`, and two resolvers: by an explicit `@stereo` flag
  (`resolve()`), and by matching an observed value-vector length
  (`resolve_by_value_count()`, which is how you pick the right variant when reading
  a preset back). Parameter-name matching folds out separators and case, because
  spellings drift between HX Edit eras (`High Cut` / `HighCut`,
  `Gate_Range` / `GateRange`).
- **`hlxgen/device/resolve.py`** — joins `helix_model_information.json` onto that
  table via `internal_model_name` + variant suffix. Gives the wire index, parameter
  ordinals, and `value_vector()`, which lays named values out in device order and
  marks unsupplied entries `None` rather than shifting later ones.
- **`hlxgen device-audit --symbols <path>`** — reconciles the whole catalog against
  a symbol table: coverage, the Mono/Stereo split, and parameters that do not
  reconcile in either direction. `--report` writes JSON. Diagnostic; always exits 0,
  because a real table legitimately carries host-only models with no device symbol.

Added since, and verified against a real HX Stomp (serial 3264140, fw 3.x):

- **`hlxgen/device/frames.py`** — the 16-byte framing layer. `len` is
  `8 + significant body`, the body is padded to 4 bytes, and the declared length
  is the authority when decoding.
- **`hlxgen/device/usb.py`** — `Session`: open, clear-halt, drain, the five-packet
  init, one synchronous command, the paged stream reader, and a polite close.
  Every bulk operation is bounded by a timeout.
- **`hlxgen/device/document.py`** — the record-stream parser. See the correction
  below; this is where the non-canonical-encoding problem is solved.
- **`hlxgen/device/codec.py`** — the donor overlay.
- **`hlxgen/device/writer.py`** — the `op 21` transfer with all four guards.
- **`hlxgen/device/commands.py`** — `devices`, `pull`, `backup`, `push`.

New dependencies: `pyusb` and `msgpack`, plus `libusb` (`brew install libusb`).

### The stream's first reply carries the document's first 237 bytes [verified live]

**This was the single most costly mistake of the session, and it was silent.**

A paged read's first reply looks like a header worth discarding. It is not::

    00 00 7b 28 86 0a 00 00        stream framing
    83 66 cd 03 eb 67 00 68        {102: txn, 103: status, 104:
    da 0a 7b                       str16, 2683 bytes  <- the declared length
    a9 6c 36 2d 68 65 6c 69 78 00  fixstr(9) "l6-helix\0"  <- the document starts
    da 00 30 3d 00 00 00 ...       str16(48) + the offset table

Dropping it costs the magic **and** the offset table. What remains still decodes
as plausible MessagePack, so nothing complains: a 2683-byte document came back as
2446 bytes that parsed happily into 34 "records" which were really fragments of
one map. A whole 126-slot backup taken that way looked perfect and was useless.

**The declared length at key 104 is the authority.** Reassembly now starts from
the preamble's tail and stops exactly on it, refusing anything under or over.

### A preset document is three MessagePack values [verified live]

    "l6-helix\0"        magic, a 9-byte fixstr
    <48-byte str>       the header
    {0: ..., 1: ...}    the preset map

**The header is an offset table, not opaque bytes** — twelve little-endian u32s:
the preset map's start, the byte offset of each of the map's nine top-level
entries, and the total blob length (twice). The device seeks by it, so **any edit
to the map invalidates it**. On the Stomp every one of the twelve classifies
cleanly; none is an unexplained constant.

Blocks live at `preset[<dsp group>][22]`, a **20-entry slot array** per DSP:
index 0 a spacer, 1–8 the first path's blocks, 9 the input, 10 the output, 11–18
the second path, 19 the routing node. So **§4's `@path × 10 + @position + 1` is
exactly right** — it indexes that array. The earlier "contiguous runs" reading in
this document was an artefact of parsing a truncated blob and has been removed.

The §4 mapping table is otherwise confirmed against real data: model index at
block `24 → 25`, paired cab at `24 → 26` (`-1` when absent), value vector at
`11 → 4`, enable at `10`, type at `9`.

### The `op 21` and `op 71` bodies [verified live]

Both were **guessed** at first, and the guess is what wedged the pedal. The real
shapes, from `fretwire-protocol`'s builders:

```
op 21   {102: txn, 100: 21, 101: {110: <blob as msgpack str>}}
op 71   {102: txn, 100: 71, 101: {107: bank, 108: slot, 109: "<name>\0"}}
```

**The document rides inside the op-21 envelope**, at key 110. It is not a header
announcing a payload sent separately — which is what was sent first, and the
device took the frame and stopped draining its OUT endpoint, needing a power
cycle. The credit-paced 496+16 chunking applies to the whole envelope, blob
included.

The blob is wrapped as a `str` with **non-minimal `str16`** framing, and it is
not valid UTF-8, so the envelope is emitted by hand rather than through a
MessagePack library. `hlxgen.device.writer.encode_write_preset` reproduces the
capture prefix byte-exactly (`83 66 cd 04 c6 64 15 65 81 6e da 0b 9a`).

`op 71` carries the preset **name**, NUL-terminated. Omitting key 109 was the
second guess.

### How many values a model stores [verified live, from 126 real presets]

It is **not** the symbol's parameter count, and getting it wrong makes the device
reject the whole document:

| case | stored | seen |
|---|---|---|
| symbol carries `IrData` | parameters − 1 | 10/10 |
| ordinary model | parameters | 160 |
| model with a trailing extra (`Trails`) | parameters + 1 | 55 |

`IrData` is never stored and is last in **all 92** symbols that carry it, so
dropping it shifts no ordinal. The `+1` group — reverbs, delays, FX loops — is
the trailing extra §4 already predicted.

### A whole-document write will not change a block's model [verified live]

This is why the upload path ended up being **A rather than B**, reversing §3.

Isolated on hardware, one mutation at a time, against a known-good donor:

| change | result |
|---|---|
| `@enabled` | **accepted** |
| value vector | **accepted** |
| verbatim document (no edit) | **accepted** |
| semantically identical re-encode | **accepted** |
| **model index** (with the correct value count) | **rejected — slot left empty** |

Ruled out as the cause: the value count (matched against the observed table), the
block type at key 9 (the same model legitimately appears with different types —
model 15 as both 1 and 18, so it is not model-determined), and the offset table
(rebuilt and verified).

So `op 21` evidently validates more about a model than the block record carries,
and the remaining state is undecoded. **`op 40` — swap a block's model — has no
such problem**, and path A is now what `hlxgen push` uses by default.

### Path A works, and it is the upload path [verified live]

`hlxgen/device/editor.py`. The sequence, per §3's path A:

1. `op 20` select the slot, so the edit buffer holds it.
2. **Read**, which opens the buffer. Never edit straight after a select.
3. Per block: `op 40` swap the model, then `op 30` per parameter, then `op 41`
   for bypass. `op 28` empties any donor slot the tone does not fill.
4. `op 71` commit.

**`op 40` resets the block's parameters to the new model's defaults**, which is
why values follow the swap — and why a tone naming only some parameters still
lands on sensible values for the rest.

### Parameter values must be float32, not float64 [verified live]

The single most confusing failure of the write work: every model swap in a
preset succeeded and **every value edit was refused**. The device checks a
value's wire type exactly rather than coercing it, and a MessagePack library
emits Python floats as `float64` (`cb`) by default where the device wants
`float32` (`ca`). One flag — `use_single_float=True` — took a preset from 0
values applied to 27.

An `.hlx` also does not say which type a parameter *is*: a mic `Angle: 45` is
continuous while a `Mic: 3` is an enum, and both are JSON integers. Since a
refusal is cheap and changes nothing, the type is resolved by **trying the
plausible wire forms in order** (float, then int, then bool for 0/1) rather than
by guessing from the JSON type. That took 27 values to 32 of 33.

The last gap was the string enum (`Ratio: "2:1"`), which has no wire form on its
own — the catalog's `reverse_map` turns the label into its index, and with that
wired in both sample presets transfer every value they carry. Anything still
unresolvable is reported per preset rather than guessed at.

### An amp carries its own cab, in one slot [verified live]

On the pedal an amp and its cab are **one block**, not two: the cab is fused into
the amp's slot as a paired model (`24 -> 26`, its values in bank `12`). That is
how a preset with an amp and six effects still fits a Stomp's eight slots. A tone
that lists them as separate blocks is describing the same thing twice, so `push`
absorbs the cab and closes the chain up behind it -- a six-block jazz tone lands
as five blocks with three slots free instead of two.

**Which cab comes from HX Edit's `amp.models`**, not from the tone. Each of its
111 amp entries carries `cablink` (the paired cab) and `ircablink` (the default
standalone IR cab). Checked against 60 amp+cab blocks read off the pedal: 41 use
`cablink` exactly, and the other 19 are cabs their owner changed by hand.

Using the amp's default rather than honouring the tone's cab is not a shortcut:

* **A paired cab is a different model from a standalone one.**
  `HD2_Cab1x12USDeluxe` is `[Distance, LowCut, HighCut, EarlyReflections, Level]`
  while `HD2_CabMicIr_1x12USDeluxe` is `[Mic, Position, Distance, Angle, LowCut,
  HighCut, Level, IrData]`. The amp block owns the mic, so mic/position/angle
  have nowhere to go.
* **Only 20 of the 92 `HD2_CabMicIr_*` symbols have a paired counterpart at
  all**, so honouring the tone's choice would fail for most cabs.

The substitution is audible, so it is reported per preset
(`tone asked for HD2_CabMicIr_1x12USDeluxe`), along with any cab parameter the
paired model has no home for. `--separate-cabs` keeps the old two-slot layout.

A footswitch the tone bound to an absorbed cab is **dropped, not remapped**: a
paired cab cannot be switched on its own, and moving the binding onto the amp
would leave two switches toggling the same block.

`amp.models` is Line 6's, like `Helix.sym`, and is read from the user's own HX
Edit install. `.gitignore` now covers `*.models`.

### `op 41`'s key 59 is *enabled*, not *bypassed* [verified live]

The op is called "bypass" throughout the prior art, and the capture behind it is
a **bypass press** sending `59: true` then `59: false` — which says nothing about
which way round the bool goes. Reading the name literally and sending
`not @enabled` inverts every block in the preset.

Measured directly, against a block's stored `enabled` flag (content key `10`):

| sent | block ends up |
| --- | --- |
| `59: true` | **on** |
| `59: false` | off |

So key 59 carries *enabled*. The failure this caused is worth noting because of
how quiet it is: the write succeeds, the chain is right, every value is right,
the footswitches are right — and the whole preset is silent, because all six
blocks are switched off. Nothing in the transfer reports a problem.

`push` now sends the state for **every** block rather than only those whose tone
names `@enabled`: a model swap leaves the block in whatever state the slot's
previous occupant had, so a generated preset would otherwise inherit the old
preset's on/off pattern.

### The footswitch layout and snapshots live in the document [verified live]

Neither is reachable through the edit ops, so `push` applies them as a second
phase: once the chain is correct, the document goes back with `op 21` — which the
device accepts, because by then **no model is changing**.

**Footswitches** are `preset[3][8]`, one entry per physical switch (five on a
Stomp). An entry is a one-element list wrapping a map whose `11` sub-map holds:

| key | meaning |
| --- | --- |
| `5` | label, NUL-terminated |
| `6` | LED colour |
| `7` | enabled |
| `8` | **the block slot the switch controls** |

**The switch and the block it drives are independent.** The switch is the entry's
*position* in the array (§4's "`@fs_index` − 1 is the array position"); the block
is key `8` inside it. §4 does not mention that field, and it is easy to conflate
the two because they coincide whenever switches happen to be assigned in chain
order. On a real preset they often are not: `hlxgen`'s own generator assigns
distortion/modulation/delay blocks first and everything else after, so a six-block
tone lands as FS1→chorus, FS2→delay, FS3→compressor — verified end to end on the
pedal.

Of 336 footswitch entries read off the pedal, 330 share one shape. The six that
differ carry an extra sub-map at `11 → 9` because they assign a *parameter*
rather than a block's bypass, which is not what this writes. A document whose
layout is empty — one rebuilt by writes — has no entry to clone, so that measured
shape stands in.

**Snapshots** are `preset[10][10]`, and §4's mapping holds exactly: `@name` at
key `4` (NUL-terminated), `@tempo` at `5`, `@valid` at `0`, and a block's
per-snapshot state at `3[slot][1]`. The snapshot the preset opens on is the
group's key `6`. The device stores no "custom name" flag: a snapshot is named or
it is not.

### Snapshot-controlled parameters [decoded live; the fix awaits a re-check]

On/off states alone make every snapshot the same tone with blocks muted. What
makes a snapshot a *sound* is that it recalls its own knob positions, and those
are held in two places at once:

- **The assignment** lives in `preset[4]`, a list indexed by **controller
  source** — 1 and 2 are the expression pedals, **9 is Snapshots**. Each entry is
  `{0: id, 1: {0: source, 2: min, 3: max, 5: block slot, 6: {28: sub-model,
  29: parameter ordinal}, 7: sub-model}}`, with the parameter ordinal in the same
  `Helix.sym` index space a set-value uses, and the sub-model selector picking a
  fused cab exactly as it does there. `1: 4` and `13: false` are constant across
  all 349 assignments read off the pedal.
- **The values** live in each snapshot's key `2`: 64 entries of
  `[fs_enabled, id, value]`, indexed by the **assignment id**, which is one pool
  shared by every source. An unused entry is `[false, 64, nil]`; stale entries
  from a deleted assignment keep an old value and an id of 13 or 64, and the
  device treats both as unused.

A value has to carry the parameter's own type, as everywhere else on this wire.
The `.hlx` does not say which type that is, and here there is nothing to probe
against — a document is written whole. The **document read back after the edits**
settles it: it holds the block's own value at that ordinal in the device's type,
so the range and every snapshot value are converted to match it.

#### The count at `preset[10][8]` is load-bearing [verified live]

The snapshot group's key `8` is **how many controller assignments the preset
holds**, across every source. It matches the count in `preset[4]` on all 126
presets on the owner's pedal.

Leaving it at `0` produces a preset that looks entirely correct and is not:
HX Edit draws the brackets that mark a parameter as snapshot-controlled, the
assignments read back intact, and the pedal still recalls the same knob positions
in every snapshot. That failure was observed live, and it was **visible only in
HX Edit** — by switching snapshots and watching a value that should have changed
and did not. No error, no refusal, and nothing in the read-back to compare
against but another preset.

**Still to confirm on hardware:** that setting the count makes the pedal recall
the values. The uploaded preset carries the right count now (6 of 6 read back),
but the pedal was disconnected before HX Edit could be reopened on it. Repeat the
check the same way: push a tone with snapshot values to a scratch slot, quit and
reopen HX Edit (it holds the USB interface), load the preset from flash, and
switch snapshots watching a controlled value.

Assignments the donor held are dropped before the tone's own are written, for the
same reason a footswitch binding is: an assignment names its target by **slot**,
so one left behind drives whatever block the new tone put in that slot.

### Applying a tone outlasts a single session [verified live]

A tone costs far more frames than one session survives: two full document reads,
a swap plus a value edit per parameter per block, and a whole-document write. That
runs past the 256-frame sequence wrap and kills the device mid-edit — it did,
twice, needing a power cycle each time.

`EditSession` renews before the budget runs out. Renewing is not just
reconnecting: the edit buffer belongs to the device and is tied to the loaded
preset, so the run **commits before letting go** and re-selects and re-reads
afterwards, the read being what reopens the buffer. A four-block tone takes two
sessions.

### A session does not survive the sequence wrap [verified live, 2026-09-17]

The header's sequence byte at offset 9 rolls over every 256 frames, and **the
device stops reading cleanly across the rollover**. A slot read costs about
eleven frames, which puts the wrap around every twenty-third slot — and that is
exactly the period at which a naive full sweep failed: slots 31, 54, 76, 99, 121
and 122 came back started-mid-document, roughly 22–23 apart.

What the device does differently at the rollover is **not decoded**. Forcing the
wrap to 1 instead of 0 moves the symptom (reads start timing out instead of
coming back short) without fixing it, so it is not simply that sequence 0 is
reserved. Rather than out-guess it, long-running callers **recycle the session**:
`Session.near_sequence_wrap` reports when the budget is nearly spent and a fresh
five-packet init costs almost nothing. A full 126-slot sweep runs across 8
sessions and reads every slot cleanly.

This is worth chasing properly at some point — a captured HX Edit session that
runs past 256 frames would show what it really does.

### A streamed read must be validated, not trusted [verified live]

A desynced stream does not announce itself. It comes back as a plausible-looking
blob that simply starts in the middle of a record, and a backup full of those
looks like a successful backup. Every read is therefore parsed before it is
accepted — the document layer requires the records to consume the blob *exactly*,
so a short or spliced stream fails — and a failure is retried after a drain
rather than surfaced, which is what the prior art's logs also concluded. Only
after the retries are exhausted does a read raise.

Of the first full sweep, 6 of 126 slots were silently wrong before this check
existed.

### The offset table must be rebuilt, and canonical re-encoding is fine

The device writes **non-canonical** MessagePack — `0` is stored as `d1 00 00`
(int16) where a canonical encoder emits a single `00` — so a re-encode is always
a different length from the original. That turns out not to matter: a
semantically identical re-encode, 117 bytes shorter than what came off the pedal,
was **accepted and stored correctly**. What matters is that the offset table is
recomputed against the bytes actually emitted. `dump()` does that, rebuilding the
table at its fixed length from the classified slots.

A stale table is not a cosmetic fault. The device accepts the transfer, commits
it, and stores an **empty preset** — the first `.hlx` push wiped its target slot
exactly this way.

Tests run against synthetic symbol tables and synthetic documents built to the
documented shape, so the suite needs no proprietary data and no hardware.

---

## 6. TODO

### Phase 0 — confirm the ground truth  ◐

- [x] Run `hlxgen device-audit` against a **real** `Helix.sym`. Done:
      833 device symbols, 343 catalog models, **343 resolved, 0 unresolved**.
- [x] From that output: the device splits **146** catalog models into Mono/Stereo,
      and the separator-and-case normalisation **holds across all 343** — nothing
      needed adding. 115 models mismatch on parameters, and the breakdown matters:
      **47 are a trailing `IrData`** the device lists but stores no value for
      (checked: it is last in **all 92** symbols that carry it, so it never shifts
      an ordinal), and most of the rest are stereo-only parameters legitimately
      absent from a Mono variant.
- [x] **A full, verified backup of the pedal exists.** `hlxgen backup` swept all
      **126 slots (0 unpopulated, 0 failed)** and every file parses and re-encodes
      **byte-identically**. Taken with the same protocol that writes them back, so
      it is restorable by the same code path.
      An HX Edit `.hxb` is still worth having as an *independent* recovery route —
      it does not depend on this project's write path being correct — and it can be
      taken normally: HX Edit and the USB client only conflict while both are
      running, not over time.
- [ ] Confirm the recovery path works (Line 6 Updater / safe-boot on power-up)
      *before* experimenting. Also a human step.
- [x] Enumerate USB on macOS and dump the descriptors. Confirmed: VID `0x0e41`,
      PID `0x4246`, interface 0 class 255 (vendor), bulk **IN `0x81` / OUT `0x01`**,
      `wMaxPacketSize` 512. Audio is interfaces 1–4 and HID is 5; all left alone.

### Phase 1 — transport and reads  ✅

- [x] `hlxgen/device/usb.py`: open, claim interface 0, clear halt, drain, the
      five-packet init, one synchronous command, polite close. Write timeout from
      the start (guard 2).
- [x] Detect "HX Edit is running" and fail with a clear message
      (`DeviceBusyError`) rather than a raw `LIBUSB_ERROR_BUSY`.
- [x] `hlxgen devices` — enumerate and identify. `--identify` opens a real control
      session per device.
- [x] `op 4` slot reads, with the empty-slot reply (`104: nil`) told apart from a
      desynced read.
- [x] `hlxgen pull --slot N -o donor.bin`
- [x] `hlxgen backup -o ./backup` — leaves unpopulated slots out, as HX Edit does.
- [x] Recycle the session before the sequence wrap; validate and retry a
      desynced stream. Both were found by verifying a real sweep.
- [ ] Record every USB transaction as a fixture for replay tests. The unit tests
      currently use synthetic frames rather than captured ones.
- [ ] Decode what the device actually does at the sequence rollover, instead of
      stepping around it.

### Phase 2 — the codec  ✅

- [x] `hlxgen/device/document.py` + `codec.py`: parse a device document and
      overlay an `.hlx` onto it using `resolve.py`.
- [x] The §4 mapping table, including the `@stereo`-absence rule. A model change
      **resizes** the value vector to the new symbol rather than inheriting the
      donor's length — carrying it over would land values on the wrong controls,
      since 43 of 153 Mono/Stereo pairs diverge mid-list.
- [x] **Exit criterion met**: a slot read off the pedal (2446 bytes) re-encodes
      **byte-identically**. Verified live, and pinned by offline tests.
- [x] Report per preset whatever the tone could not carry (`OverlayReport`).
- [x] Amp+cab as one block: an amp is paired with its own `cablink` cab, a
      separate cab block in the tone is absorbed, and the chain closes up. The
      paired cab's own parameters are written through the sub-model selector
      (`26: 1`).
- [ ] Round-trip a device document **back** to `.hlx`. Only the forward direction
      (`.hlx` → device) is built, which is all `push` needs.

### Phase 3 — writing  ✅

- [x] `op 21` document write with all four guards from §3. Units are split
      **496 + 16**, credits are matched on source *and* opcode, strays are held and
      returned, and a slow non-final credit aborts rather than backs off.
- [x] `op 71` commit, sent by the session and never by the writer.
- [x] `hlxgen push tone.hlx --slot N` — `--slot` **required**, no default, no
      "first free slot" guessing.
- [x] Archive the target slot before overwriting (`--archive`).
- [x] `--dry-run` builds the document and reports, sending nothing.
- [x] **The write path works on hardware.** A complete document written to a
      chosen slot, committed, and read back with every block intact — verified
      repeatedly on an HX Stomp. Credits pace cleanly (worst non-final 2.1–2.9 ms
      against a 20 ms abort threshold).
- [x] `hlxgen push tone.hlx --slot N` **lands a generated preset**, verified on
      an HX Stomp with both a 4-block and a 6-block `.hlx`: every model, every
      value, every bypass state, **the footswitch layout and the snapshots** match
      the source, and donor blocks the tone does not define are cleared.
- [x] Footswitch bindings, labels and LED colours, and snapshot names, tempos and
      per-block states — written as a second phase, since the edit ops do not
      carry them.
- [x] **Snapshot-controlled parameters**: the assignments in `preset[4][9]`, a
      value per snapshot in each snapshot's key `2`, and the assignment count at
      `preset[10][8]` without which the device recalls nothing. Written and read
      back intact from a scratch slot.
- [ ] Confirm on the pedal that the values are actually recalled when the
      snapshot changes — see the note at the end of §5.
- [x] Per-block on/off state, with `op 41`'s polarity established by measurement
      rather than by the op's name.
- [x] Path A (`op 20`/`40`/`30`/`41`/`28`/`71`) built as `device/editor.py` and
      made `push`'s default. `--via document` keeps the op-21 path for restoring
      a document.
- [x] **Survives a power cycle**, confirmed on the owner's HX Stomp: the slot
      read back **byte-identical** (2368 bytes) after the pedal was unplugged and
      brought back up.

**Phase 3's exit criterion is met in full**: a generated preset lands in a chosen
slot, survives a power cycle, and reads back correct.
- [ ] Read-back is not byte-identical and probably never will be: the device
      re-derives the document (it normalises, and appends the trailing `Trails`
      value). The criterion should be *semantic* equality of blocks and values,
      which is what `push` should check instead of comparing bytes.

### Phase 4 — integration  ✅

- [x] New `post_write` implementation behind the existing hook in
      `_generate_from_chain` (`cli.py`). `_upload_via_script` stays as a documented
      fallback rather than being deleted.
- [x] `--upload-via usb|applescript`, defaulting to USB when a device is found.
- [x] `--slot N` added. `--upload-mode auto|manual` is **kept** rather than
      replaced, because it still drives the AppleScript fallback; it is simply
      ignored on the USB path.
- [x] `--upload` added to `generate` as well. Both commands share one hook, so
      the same flags mean the same thing from either.
- [ ] Record the verified firmware version in backup files; warn on mismatch.
- [ ] The tone's input/output nodes and routing (`split`/`join`/`inputA`) are
      still left to whatever the target slot held. Only the chain, the footswitch
      layout and the snapshots are written.
- [ ] Only the snapshot controller (source 9) is written. A tone that assigns an
      expression pedal or a footswitch to a parameter has those assignments
      reported as unwritten rather than transferred; `--via document` does not
      carry controllers at all.

### Later, not now

- [x] Path A surgical edits — **built and shipping**, because `op 40` is the only
      known way to change a block's model. Live tweaking, its original motivation,
      now comes almost free.
- [ ] `op 43` move, and `op 39` add-block for a tone that needs more slots than
      the donor's array has. Note that **`op 40` already works on an empty slot** —
      a 6-block tone landed on a 5-block donor without needing `op 39` — so this
      matters only past the array's own length.
- [x] Enum parameters named by string (`Ratio: "2:1"`) resolve through the
      catalog's `reverse_map`. With that wired in, both sample presets transfer
      **every** value: 33 of 33 and 46 of 46, nothing reported unsupported.
- [ ] Windows and Linux support.

---

## 7. Hard rules

- **Firmware, flash and DFU are out of scope and must never be transmitted.** They
  are the only realistic way to brick the unit. Everything else is recoverable.
- **Never probe undecoded ops.** An "accepted" reply from an op the device did not
  act on proves nothing — five ops were observed accepting a body they evidently
  ignored, and one wrong body then wedged a unit. Do not sweep. One op, one body,
  then look at the device.
- **Never write straight after a `goto`.** Read between them.
- **Back up before any write**, and archive the target slot before overwriting it.
- **Do not commit Line 6 data.** `Helix.sym` and its siblings are gitignored. Note
  that `helix_model_information.json` is committed and plainly derives from the same
  source — a pre-existing question this work did not create, but one worth resolving.
- Claim **interface 0 only**. Leave the audio interface alone.

---

## 8. Open questions

- ~~Which parameters drop out of a mono variant, and whether the drop is ever
  mid-list.~~ **Answered, and the answer is yes.** Of 153 Mono/Stereo pairs in
  `Helix.sym`, **110 have Mono as a strict prefix of Stereo and 43 diverge
  mid-list** — `Scale` and `Spread` are the usual culprits, sitting in the middle
  of the stereo variant and shifting every ordinal after them. Getting `@stereo`
  wrong therefore mislabels every parameter past the divergence point, so the
  variant rule is load-bearing rather than cosmetic.
- ~~Whether our parameter-name normalisation is sufficient across all 343
  models.~~ **Answered: it is.** All 343 resolve, none unresolved.
- Whether the input/output nodes' "ragged prefix" matters for presets we generate —
  we may be able to leave them entirely to the donor.
- `op 4`'s behaviour is verified on the HX Stomp only. Irrelevant while we are
  Stomp-only, but it is the first thing to re-check if another device appears.

---

## 9. Prior art

All black-box interoperability work — traffic captured between owned hardware and
licensed software, no Line 6 source, firmware or SDK involved.

- **[fretwire](https://github.com/john-baxter-dev/fretwire)** — the substantive one.
  A ~1,500-line living protocol spec (`docs/protocol.md`), a ~650-line preset-format
  spec with the tone↔wire table (`docs/preset-format.md`), a documented safety model
  (`docs/safety.md`), and working read/write crates. Rust, Apache-2.0/MIT. Most of
  §2–§4 above is distilled from it. **Note it is a living document: check the dated
  entries, since later rounds supersede earlier ones.**
- **[openhx](https://github.com/allansomensi/openhx)** — clean transport-level docs
  under `docs/protocol/`. Rust, MIT.
- **[helix_usb](https://github.com/kempline/helix_usb)** — Python on pyusb; useful
  as a shape for the transport layer.

Related in-repo reading: [`application_flow.md`](application_flow.md) for how
`hlxgen` works today, in particular the `post_write` hook that phase 4 targets.
