# Legal

HelixPy is an independent project. This page states what it is, what it is
built from, and what it deliberately does not contain.

## Trademarks

Helix, HX, HX Stomp, HX Edit, Line 6 and POD are trademarks of Yamaha Guitar
Group, Inc. / Line 6, Inc.

This project is **not affiliated with, authorised by, endorsed by or sponsored
by** Line 6 or Yamaha Guitar Group. Those names appear here only to identify
the hardware and software HelixPy interoperates with, which is the only way to
say what the project does.

## How the device support was built

All of it is black-box interoperability work: USB traffic observed between
hardware and software the author owns and is licensed to use, then reasoned
about and written up in [`usb_upload_plan.md`](usb_upload_plan.md).

It contains **no Line 6 source code, firmware, or SDK**, and none was consulted.
Firmware, flash and DFU operations are out of scope and are never transmitted -
see §7 of that document for the safety rules this work holds itself to.

## Line 6 data files: not distributed

HX Edit ships several data files that HelixPy reads. None of them is in this
repository, none is in a release, and all of them are listed in `.gitignore` so
they cannot be committed by accident:

| File | What it is | Needed for |
| --- | --- | --- |
| `Helix.sym` | The device's model and parameter ordering | Every device operation. Nothing can be addressed without it. |
| `amp.models` | Each amp's default cabinet | Optional. Without it an amp keeps a separate cab block. |

They are read at run time, in place, from the HX Edit installation on the
user's own machine - which is why HelixPy needs HX Edit installed to talk to a
pedal at all, and why it will never be able to ship without that requirement.
See [`hlxgen/device/hxedit.py`](../hlxgen/device/hxedit.py) for how it is found.

Anything pulled off a pedal (`hlxgen pull`, `hlxgen backup`) is someone's own
presets and is likewise gitignored.

## The model catalog

`hlxgen/data/helix_model_information.json` **is** distributed, and it is the one
file in this project where that deserves an explanation.

It is a table of facts about what the hardware can do: each model's display
name, its internal identifier, its category, the real-world amp or pedal it is
based on, and for each control its type, range, default and the labels of its
options. It exists because a preset file is unintelligible without it - a
generated preset has to name a model the device recognises and give every
parameter a value the device will accept, and there is no other way to know
what those are.

It carries no code, no algorithms, no audio processing, and nothing about *how*
any model works. It is used solely to produce files the hardware can read.

If you are a rights holder and consider any part of this file to be yours, open
an issue or write to oliver.rogers101@gmail.com. It will be taken seriously and
addressed promptly.

## Your own presets

`HXTemplate.hlx` is a preset exported from the author's own HX Stomp. Generated
presets are built by copying its structure - snapshots, global parameters,
device identifiers - so the result imports cleanly. It contains no Line 6 data
beyond the model identifiers already discussed.

## Warranty

None, as the Apache-2.0 licence states. HelixPy writes to a device you paid
for. Back up before you write - `hlxgen backup` exists for that - and read the
safety notes in the README first.
