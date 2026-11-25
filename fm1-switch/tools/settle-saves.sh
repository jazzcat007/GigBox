#!/usr/bin/env bash
# settle-saves.sh — does a preset survive a power cycle on this unit?
#
# WHAT THIS SETTLES
# -----------------
# The open question from §4.3.5 of the arrival runbook: a unit with a flash chip the
# firmware does not know writes "saves" that vanish at power-off, because the write never
# reaches flash. The new-chip unit (0xB36014) is the one at real risk; the old-chip ones
# are the control that tells us we are running the test correctly.
#
# The test is a DELTA, not an absolute:
#   1. put the unit in download mode and read its flash          -> before.bin
#   2. power it OFF from the Pi, let it play, save a preset, power cycle, ...
#   3. put it back in download mode and read its flash again     -> after.bin
#   4. a save that reached flash shows as differing bytes; a save that vanished shows none
#
# Step 2 is the part a machine cannot do. YOU perform it, then run step 3.
#
# WHY NOT JUST "LOOK AT THE PRESET"
# ---------------------------------
# On a unit whose firmware cannot write its chip, the preset may still appear to be there
# after a normal power cycle if it never left RAM in the first place -- or "flash error" may
# be shown. Reading the flash bytes is the only answer that is not an interpretation.
#
# USAGE
#   ./settle-saves.sh before <slot>     # requires the unit in download mode
#   ./settle-saves.sh after  <slot>     # same, after your power-cycle + save
#
# Both write into ~/fm1-backups/saves-test/ and exit non-zero on any failed precondition,
# so a half-run cannot be mistaken for a result.

set -uo pipefail

MFLASHER="${FM1_FLASHER:-$HOME/bin/m-flasher}"
PORTMAP="${FM1_PORTMAP:-$HOME/fm1-portmap.txt}"
OUT="$HOME/fm1-backups/saves-test"
PY="${FM1_PYTHON:-$HOME/fm1/fm1-switch/.venv/bin/python}"

say()  { printf '\n== %s\n' "$*"; }
ok()   { printf '   [ok]   %s\n' "$*"; }
bad()  { printf '   [FAIL] %s\n' "$*" >&2; }
note() { printf '          %s\n' "$*"; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

phase="${1:-}"; slot="${2:-}"
[[ "$phase" == "before" || "$phase" == "after" ]] || usage
[[ "$slot" =~ ^fm1-[abcd]$ ]] || usage

port=$(awk -v s="$slot" '$1==s {print $2; exit}' "$PORTMAP" 2>/dev/null)
if [[ -z "$port" ]]; then
    bad "$slot has no entry in $PORTMAP"; exit 1
fi

# --- precondition: this slot must actually be in download mode -------------------------
dev="/dev/$slot"
if [[ ! -e "$dev" ]]; then
    bad "$dev does not exist, so $slot is not in download mode."
    note "Put it into download mode first:  fm1-switch download $slot"
    note "(Reminder: the soft key is a BROADCAST on this hardware -- check every unit"
    note " afterwards rather than assuming only this one moved.)"
    exit 1
fi

# --- precondition: the slot must be the unit we think it is ----------------------------
mkdir -p "$OUT"
say "Identifying $slot (USB port $port -> $dev)"
info=$("$MFLASHER" info --device "$dev" 2>&1) || { bad "m-flasher info failed:"; echo "$info" >&2; exit 1; }
# Field indices matter and were wrong on the first run: the label sits in $1, and the
# value starts at $2 ("unit id" is two words; "flash" is followed by a chip id and a
# parenthesised age; "firmware" by an arbitrary label that may contain spaces).
uid=$(awk '/^  unit id / {print $3}' <<<"$info")
chip=$(awk '/^  flash    / {print $2}' <<<"$info")
fw=$(sed -n 's/^  firmware *//p' <<<"$info")
if [[ -z "$uid" || "$uid" == "-" ]]; then
    bad "no unit id reported -- cross-unit safety cannot be established; stopping."
    exit 1
fi
ok "unit id  $uid"
ok "chip     $chip"
ok "firmware $fw"

if [[ "$chip" != "0x856014" && "$chip" != "0xB36014" ]]; then
    bad "unexpected flash chip $chip -- recording the answer in §7 before trusting a write."
fi

# --- refuse to overwrite an earlier 'before' with a different unit ---------------------
first="$OUT/$slot.before.json"
if [[ "$phase" == "after" && -f "$first" ]]; then
    before_uid=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1])).get('unit id',''))" "$first" 2>/dev/null)
    if [[ -n "$before_uid" && "$before_uid" != "$uid" ]]; then
        bad "this slot held $before_uid when '$phase' ran, but holds $uid now."
        note "The unit in $slot has CHANGED. Resolving the two readings would be meaningless."
        exit 1
    fi
fi

# --- take the reading ------------------------------------------------------------------
dest="$OUT/$slot.$phase.bin"
say "Reading flash from $slot -> $dest"
if ! "$MFLASHER" backup --device "$dev" --backup-dir "$OUT" >/tmp/.fm1-backup.log 2>&1; then
    bad "m-flasher backup failed:"; sed 's/^/   /' /tmp/.fm1-backup.log >&2; exit 1
fi
newest=$(ls -t "$OUT"/fm1-*.bin 2>/dev/null | head -1)
[[ -n "$newest" ]] || { bad "no backup file appeared in $OUT"; exit 1; }
mv -f "$newest" "$dest"
mv -f "${newest%.bin}.json" "$OUT/$slot.$phase.json" 2>/dev/null || true
sha=$(sha256sum "$dest" | cut -c1-16)
ok "$dest"
ok "sha256 ${sha}…"

if [[ "$phase" == "before" ]]; then
    say "Now do this on the unit, BY HAND:"
    note "1. Power $slot OFF (unplug its USB-C, or switch it off)."
    note "2. Power it back on and wait for it to boot its normal firmware."
    note "3. Save a preset you can recognise to a slot whose LED is OFF right now."
    note "     (a SAVE onto an already-lit slot ERASES that preset -- pick a free slot)"
    note "4. Note WHICH slot number you saved to: ______"
    note "5. Power it off and on again."
    note "6. Enter download mode again:  fm1-switch download $slot"
    note "7. Run:  ./settle-saves.sh after $slot"
    say "Nothing has been interpreted yet -- this is only the 'before' reading."
    exit 0
fi

# --- compare ---------------------------------------------------------------------------
before="$OUT/$slot.before.bin"
if [[ ! -f "$before" ]]; then
    bad "no 'before' reading at $before -- run: ./settle-saves.sh before $slot"
    exit 1
fi

say "Comparing before vs after"
"$PY" - "$before" "$dest" <<'PY'
import sys
a = open(sys.argv[1], "rb").read()
b = open(sys.argv[2], "rb").read()
if len(a) != len(b):
    print("   [FAIL] readings differ in size: %d vs %d" % (len(a), len(b)))
    sys.exit(1)
diffs = [i for i in range(len(a)) if a[i] != b[i]]
print("   differing bytes: %d" % len(diffs))
if not diffs:
    print("")
    print("   VERDICT: the flash is byte-identical across the power cycle.")
    print("            Nothing your save did reached flash.")
    print("            -> a preset that looked saved did NOT persist in non-volatile storage.")
    print("            This is the failure mode described in runbook §4.3.5.")
    sys.exit(3)
regs, s, p = [], diffs[0], diffs[0]
for i in diffs[1:]:
    if i - p > 64:
        regs.append((s, p)); s = i
    p = i
regs.append((s, p))
print("   changed regions:")
for x, y in regs[:12]:
    print("     0x%05X-0x%05X  (%d bytes)" % (x, y, y - x + 1))
if len(regs) > 12:
    print("     ... %d more" % (len(regs) - 12))
print("")
print("   VERDICT: the flash CHANGED, so something reached non-volatile storage.")
print("            This is necessary for saves to survive, but check whether the")
print("            changed regions look like a preset store and not just a boot counter")
print("            or settings block before calling the whole question settled.")
PY
rc=$?
say "Raw readings kept in $OUT -- re-run the comparison any time with the python above."
exit $rc
