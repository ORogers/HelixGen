# Security policy

## Reporting a vulnerability

Report privately through GitHub's [security advisories][advisories] rather than
by opening a public issue. Expect an acknowledgement within a week.

[advisories]: https://github.com/ORogers/HelixPy/security/advisories/new

Useful to include: what you did, what happened, and the device and firmware
version if hardware was involved.

## Supported versions

The latest release, and `main`. There are no maintained release branches.

## Scope

In scope:

- Anything that could damage a device or destroy a user's presets.
- Code execution from a preset file, a chain specification, or an LLM response.
- Leaking an API key or the contents of a user's presets.

Out of scope:

- An LLM producing a preset that sounds bad. Every generated preset is
  validated against the schema and the model catalog before it is written, but
  nothing validates taste.
- Needing HX Edit installed to reach the device. That is a deliberate
  consequence of not redistributing Line 6's data files - see
  [docs/legal.md](docs/legal.md).

## Hardware safety

HelixPy writes to real hardware, so a bug here can cost someone their presets.
The project holds itself to the rules in
[docs/usb_upload_plan.md](docs/usb_upload_plan.md) §7, the first of which is
absolute:

> **Firmware, flash and DFU are out of scope and must never be transmitted.**

They are the only realistic way to brick a unit. Everything else is
recoverable. A change that transmits any of them will not be accepted,
whatever it enables.
