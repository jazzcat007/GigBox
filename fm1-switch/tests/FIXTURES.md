# Test fixture provenance (audit B15)

A fixture is either **captured** (bytes a real tool printed, with date and tool version) or
**synthetic** (invented to exercise a rule). Never present one as the other: an earlier
`m-flasher info` fixture was invented, matched the code's wrong assumption, and stayed green
while every field read as `None` on real hardware.

| Fixture | Where | Status |
|---|---|---|
| `REAL_INFO_V13` | `test_fm1_switch.py` | **Captured** 2026-11-22, `m-flasher info` 1.3, on the bench Pi. Only the unit id replaced by `aaaaaaaa00000002`. |
| `REAL_INFO_LEGACY` | `test_fm1_switch.py` | **Not verified as captured.** The colon form the parser still accepts; despite its name, treat as synthetic until a capture from an older m-flasher is found. |
| `realtest/*.fwsc` | `realtest/` | **Real** published firmware packages; tests skip when absent. |
| Unit ids (`aaaaaaaa0000000N`, `bbbb…`) | all tests | **Synthetic**, obviously fake. |
| `tests/captures/*` | `tests/captures/README.md` | **Captured** 2026-11-25, m-flasher 0.12.1 on the bench Pi: running-state USB tree, post-soft-key tree, `sg0` sysfs chain, `info` with progress line, and (B15 second pass) the soft-key reply, `/dev/snd` before and after, and the new-chip `info`. Unit ids replaced. |
| sysfs / USB trees | `test_captured_trees.py` (via `captured_trees.py`) | **Replayed from the captures** above. `test_registry_relearn.py` still builds small synthetic trees for edge cases. |
| Soft-key replay on the bench | `test_softkey.py` (`bench` fixture) | **Captured** 2026-11-25 on the bench Pi: `/dev/snd` and USB sysfs before and after `fm1-switch download fm1-d` (port 1-1.3, SLOOP 2.8.5). The `sound/cardN` children are derived from the same session's by-path links (`captured_trees.add_sound_cards`), since the sysfs capture does not list interface children. |
| Soft-key edge rules | `test_softkey.py` (`fake_snd` fixture) | **Synthetic**, kept on purpose: a stale by-path link, a port with no MIDI node, a card number reused by another port. No capture shows these, so they are not presented as captured. |
| Hub catalogue, parse and cache | `test_web_hub.py` (`REAL_CATALOG`) | **Captured** 2026-11-25: `hub-catalog-2026-11-25.json`, the 322-entry catalogue the Pi fetched and cached, unedited. |
| Hub catalogue, flow tests | `test_web_hub.py` (`client` fixture) | **Synthetic**: `sloop` and `melodee` with invented checksums. About 40 flow tests check hash drift, checksum refusal and flash routing against those exact values, so they stay synthetic. |
| New chip (0xB36014) `info` | `test_captured_trees.py` | **Captured** 2026-11-25, m-flasher 0.12.1, fm1-d (SLOOP 2.8.5), unit id replaced by `aaaaaaaa00000004`. |
| Hub catalogue JSON | `test_web_hub.py` | Believed synthetic; unverified. |
| Phase D: NEEDS INSTALLER premise | `test_phase_d_findings.py` | **Real package** (`realtest/melodee-1.6.1.fwsc`), the write path stubbed. Skips when realtest is absent. The live result (warn packages written 2026-11-23) is in the Pi's flash log, not a fixture. |
| Phase D: resource magics | `test_phase_d_findings.py` | **Derived from real packages**: Melodee `res.bin` begins `MRS1` (length 53,844, crc 0x2035D335), ChoralRoot `res2.bin` begins `CRE2`. Skips when realtest is absent. JIANT `bt.bin` (`JBT1`) is noted in the roadmap from the Pi; no fixture. |
| Phase D: Melodee install region diff | `test_phase_d_findings.py` | **Captured** bench backups `archives/units/fm1-20261120-171537.bin` (stock V15, unit C) and `…173052.bin` (after Melodee). Only the app region and a tail record differ; boot, VM, BTIF, USR identical. `archives/` is gitignored, so this skips in a clean checkout. |
| Phase D: unit D Melodee pair | not committed | **Captured** on the Pi 2026-11-23 (`~/fm1-backups/auto/fm1-20261123-111209.bin` before, `…111351.bin` after the Melodee write). Facts recorded in the installer roadmap (region diff, `BRASS 1` count 2 → 2). The bytes stay on the Pi. Sidecar `firmware` label of `…111351` reads "Felucca", which is stale. |
| Phase D: Pi flash log and inspect cache | not committed | **Captured** 2026-11-22 to 2026-11-25 from `~/fm1-flash.log` and `~/fm1-hub/inspect.json`. Source of the install history and the warn verdicts (jiant, melodee, choralroot). |
| Phase D: Felucca editor protocol | not committed | **Read from source**: `hugelton/Felucca` tag v1.8.2 (GPL-3.0), `firmware/src/*.c` and `web/EDITOR_PROTOCOL.md`, fetched read-only. Not copied into the repository. The MENU_SET 73 and id 21, 26..29 facts are documented, not hardware-verified. |
| Phase D: Melodee resource writer | not committed | **Read from source**: `keremimo/melodee` `tools/fm1_resources.py`, `docs/WEB_INSTALLER.md`, `firmware/src/resources.c` (GPL-3.0-only), fetched read-only. Command 82 sequence is documented, not hardware-verified. |

## Still to capture

Done 2026-11-25 (B15 second pass): a soft-key reply and `/dev/snd` before and after it, on
`fm1-d` (SLOOP 2.8.5); the hub catalogue; and `m-flasher info` on the new chip (0xB36014),
which is the unit `fm1-d`.

1. A soft-key reply on B (ghoulbox-2.4) and C (jiant V10.7.81). Captured so far: A on stock
   `FM-1 966` (08:36, the earlier session) and D on SLOOP 2.8.5 (09:06).
2. The stock V15 unit (no MIDI interface, `4d4a:4155`): its reply to a power cycle, which
   is the fallback the softkey code describes but no capture yet shows.

Each capture goes in with a header: `captured <date> on <tool> <version>, ids replaced`.
