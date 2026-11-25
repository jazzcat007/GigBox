# Captures from the bench Pi

Real output, taken read-only on 2026-11-25 from the bench Pi (`FM-Pi`, aarch64) with
`m-flasher 0.12.1` (its loader reports `M-Flasher 1.3`). Nothing here was invented.

| File | What | Edits |
|---|---|---|
| `bcddevice-all-running.raw` | `bcdDevice` (the fingerprint) of every device, running, taken just after the unit-A restart | none |
| `sysfs-all-running.raw` | USB attributes of every device, all four units running | none |
| `sysfs-after-softkey-fm1-a.raw` | the same after `fm1-switch download fm1-a` (soft key to port 1-1.1.2): that port reports `4c4a:8057 WL80UBOOT1.00`, only A moved; no `/dev/sg*` yet at that instant | none |
| `scsi-and-info-fm1-a.raw` | 5 s later: `/dev/fm1-a -> sg0`, the sysfs chain of `sg0`, and `m-flasher info` (with its progress line) | unit id `415035…` → `aaaaaaaa00000001` |

Unit A was restarted afterwards (`m-flasher restart`) and is running again. No unit was written.

## Second pass, 2026-11-25 (B15)

All on the bench Pi, `m-flasher 0.12.1` (its loader reports `M-Flasher 1.3`), read-only except
where a soft key is named. Unit ids are replaced by `aaaaaaaa0000000N`; no other edits unless
stated.

| File | What | Edits |
|---|---|---|
| `softkey-reply-fm1-d.txt` | the output of `fm1-switch download fm1-d` (soft key to port 1-1.3). The Pi wrote the soft key to `/dev/snd/midiC0D0`, and 1-1.3 then reported `4c4a:8057`. A note says the soft key may reach other units; the next files show it did not | header comment and exit-code line added |
| `sysfs-before-softkey-fm1-d.raw` | USB attributes of every device, all four running, just before the soft key | reformatted from one line per device to the `-- port` layout the replay reads; values unchanged |
| `sysfs-after-softkey-fm1-d.raw` | the same about 6 s after the soft key: only 1-1.3 changed (`4c4a:8057 WL80UBOOT1.00`); 1-1.1.2, 1-1.1.3 and 1-1.2 kept their `1209:0001` | reformatted as above |
| `snd-before-softkey-fm1-d.raw` | `ls -la /dev/snd` and `/dev/snd/by-path` before the soft key | none |
| `snd-after-softkey-fm1-d.raw` | the same after it: `/dev/fm1-d -> sg0` appears | none |
| `info-fm1-d.txt` | `m-flasher info` on `/dev/fm1-d`, the **new chip**: `Flash chip 0xB36014 (new)`, firmware `SLOOP 2.8.5`, CRC32 matched | unit id `1111aaaa00000004` → `aaaaaaaa00000004` |
| `hub-catalog-2026-11-25.json` | the Firmware Hub catalogue the Pi cached (`~/fm1-hub/catalog.json`), 322 entries | none |

Afterwards fm1-d was restarted through the Pi's own restart endpoint and reported running
again (`sloop-2.8.5`, `0xB36014`); the other three units were not touched.

The first attempt at the restart was refused with "a flash is running": the web server's own
`m-flasher info` on the download-mode unit was still reading it. Waiting for `busy` to clear
and retrying worked.
