# Direct USB preset upload — plan, research and TODO

Status: **research complete, offline groundwork landed, no device I/O written yet.**

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
2. **Drain** residual bulk data — read repeatedly at 50 ms until it fails. Stale
   packets from a previous session otherwise desync the request/response state
   machine.
3. **Five-packet handshake**: per-channel `SESSION_OPEN` (`00100000` → `00020000`)
   in the order `ef03` → `ed03` → `f003`, then an identity query per channel
   (opcode 5/6/4 → `"P33Main"` / `"P33"` plus a version word).
4. Transact.
5. Release the interface.

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
find "/Applications/HX Edit.app" -name "Helix.sym"
```

---

## 5. What already exists

Landed on this branch (PR #4), all offline, no hardware needed:

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

Tests run against synthetic symbol tables built to the documented shape, so the
suite needs no proprietary data and no hardware.

---

## 6. TODO

### Phase 0 — confirm the ground truth  ⬜

- [ ] Run `hlxgen device-audit` against a **real** `Helix.sym` from the owner's HX
      Edit install. This is blocking for everything downstream and cannot be done in
      a sandbox (line6.com is unreachable from CI/agent environments, and HX Edit is
      macOS-only).
- [ ] From that output, answer: how many catalog models does the device split into
      Mono/Stereo? Does the separator-and-case name normalisation hold across the
      whole catalog, or does it need more?
- [ ] Take a full HX Edit `.hxb` backup of the pedal **before any transmission**.
- [ ] Confirm the recovery path works (Line 6 Updater / safe-boot on power-up)
      *before* experimenting.
- [ ] Enumerate USB on macOS, dump the descriptors, confirm VID/PID and the
      vendor-specific interface's bulk endpoint addresses against §2.

### Phase 1 — transport and reads  ⬜

- [ ] `hlxgen/device/usb.py`: open, claim interface 0, drain, 5-packet handshake,
      one synchronous `txn()`, release. Write timeout from the start (guard 2).
- [ ] Detect "HX Edit is running" and fail with a clear message rather than a raw
      `LIBUSB_ERROR_BUSY`.
- [ ] `hlxgen devices` — enumerate, identify, report firmware version.
- [ ] `op 4` slot reads. Handle the empty-slot reply (`104: nil`) distinctly from a
      desynced read.
- [ ] `hlxgen pull --slot N -o donor.hlx`
- [ ] `hlxgen backup --all ./backup` — leave unpopulated slots out of the file.
- [ ] Record every USB transaction as a fixture for replay tests.

### Phase 2 — the codec  ⬜

- [ ] `hlxgen/device/codec.py`: decode a device document into our block/parameter
      vocabulary using `resolve.py`, and re-encode with the donor overlay.
- [ ] Implement the §4 mapping table, including the `@stereo`-absence rule and the
      `@cab` sibling reference.
- [ ] **Exit criterion**: read a slot, convert to `.hlx`, run the existing
      `PresetValidator` over it, convert back, assert the MessagePack is
      byte-identical to what came off the device. All offline.
- [ ] Report per preset whatever the tone could not carry.

### Phase 3 — writing  ⬜

- [ ] `op 21` document write with all four guards from §3, and txn-based ACK
      matching.
- [ ] `op 71` commit, only once the edit buffer reads back correct.
- [ ] `hlxgen push tone.hlx --slot N` — `--slot` **required**, no default, no
      "first free slot" guessing.
- [ ] Archive the target slot immediately before overwriting, always.
- [ ] `--dry-run` prints the blob without sending it.
- [ ] **Exit criterion**: a `describe`-generated preset lands in a chosen slot,
      survives a power cycle, and an `op 4` read-back matches what we sent.

### Phase 4 — integration  ⬜

- [ ] New `post_write` implementation behind the existing hook in
      `_generate_from_chain` (`cli.py`). `_upload_via_script` stays as a documented
      fallback rather than being deleted.
- [ ] `--upload-via usb|applescript`, defaulting to USB when a device is found.
- [ ] Replace `--upload-mode auto|manual` with `--slot N`.
- [ ] Add `--upload` to `generate` too — today it exists only on `describe`.
- [ ] Record the verified firmware version in backup files; warn on mismatch.

### Later, not now

- [ ] Path A surgical edits (`op 30` / `39` / `40` / `41` / `43`) for live tweaking.
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

- Which parameters drop out of a mono variant, and whether the drop is ever
  mid-list in *our* catalog's models. Phase 0's audit answers this.
- Whether our parameter-name normalisation is sufficient across all 343 models.
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
