# docs/

| File | What it is |
|---|---|
| [../CHANGELOG.md](../CHANGELOG.md) | What phase B changed, grouped by audit item, with the commit that did each. |
| [GUIDE-registering-units.md](GUIDE-registering-units.md) | How a unit gets its letter, why moving cables or a hub makes letters show NOT CONNECTED, and what RE-REGISTER FLEET does about it, message by message. |
| [GUIDE-midi-hub.md](GUIDE-midi-hub.md) | The Pi as a MIDI hub: patches and routes, a USB controller such as an Oxi One, channel layouts (packed, blocks, custom), the master clock, panic. |
| [AUDIT-fleet-manager.md](AUDIT-fleet-manager.md) | Full audit (2026-11-23): code, safety model, tests, UX, docs, security, open-source readiness, and the prioritised roadmap. Start here for the current state. |
| [REPORT-phase-b-gate.md](REPORT-phase-b-gate.md) | Phase B gate report (2026-11-23): UX-01, the SEC request gates/token/bind/static/umask, SAFE-01 + fleetctl retirement — changes per commit, tests, the 11 invariants' enforcement sites, and the owner hardware checklist. |
| [REPORT-phase-b-p1.md](REPORT-phase-b-p1.md) | Phase B/p1 report (2026-11-23): registering a unit from the panel (B4, FEAT-01) — the scan/bind endpoints, the panel flow, the refusals — plus the pre-publication scrub (fake unit ids, placeholder identity), per-commit suites, the sweep outputs, and the owner's on-panel registration walk. |
| [RELEASE-CHECKLIST.md](RELEASE-CHECKLIST.md) | The owner's bench checklist before a release: fresh Pi install, headless install, register four units, flash one unit, backup and restore, phone use, power loss mid-write. Hardware only. |
| [FM1-firmware-install-model.md](FM1-firmware-install-model.md) | Evidence: what `m-flasher firmware` writes, which packages it cannot complete, and what was verified per unit. |
| [ROADMAP-installer-firmwares.md](ROADMAP-installer-firmwares.md) | Planned work: firmwares whose resource region only the maker's installer writes (Melodee, ChoralRoot). |
| [ROADMAP-device-settings.md](ROADMAP-device-settings.md) | Planned work: setting a unit's MIDI channel and similar from the fleet manager, and fleet profiles. Starts with a read-only investigation. |
| [ROADMAP-dawless-hub.md](ROADMAP-dawless-hub.md) | Planned work: the Pi as a USB MIDI host and dawless hub (routing, clock, channel remap, network MIDI, recording). Starts with a read-only survey. |
| [FM1-firmware-landscape.md](FM1-firmware-landscape.md) | Research snapshot (2026-11-19) of the community firmware projects. |
| [FM1-fleet-web-ui.md](FM1-fleet-web-ui.md) | The expert web UI (`/`). Partly out of date; banner lists the corrections. |
| [FM1-fleet-manager-screen.md](FM1-fleet-manager-screen.md) | **Retired** console UI. Its identity and registration concepts still apply. |
| [FM1-fleet-controller-design.md](FM1-fleet-controller-design.md) | Historical design note that led to `fleetctl` (superseded; the tool was retired and removed — its udev rule lives on at `fm1-switch/udev/`). |
| [FM1-pi-appliance-assessment.md](FM1-pi-appliance-assessment.md) | Historical hardware assessment of the Pi 3B+ and panel. |
| [FM1-h4midi-wc-integration.md](FM1-h4midi-wc-integration.md) | Background: how the fleet is played over MIDI (out of this software's scope). |

`docs/private/` (gitignored) holds the bench-specific handoff and arrival runbook: they
contain LAN details, usernames and unit ids, and are not part of the public repository.
