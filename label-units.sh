#!/usr/bin/env bash
# label-units.sh — find out which physical port each FM-1 is on, and print the udev
# lines that would bind them to fm1-a..fm1-d.
#
# Run on the Pi, ONE UNIT AT A TIME, in download mode:
#
#     1. plug in a unit on the port you want to be fm1-a, put it in download mode
#     2. ./label-units.sh fm1-a
#     3. repeat for fm1-b, fm1-c, fm1-d
#     4. ./label-units.sh --apply      # writes the collected rules and reloads udev
#
# Why one at a time: in download mode every unit is "WL82 UBOOT1.00" with no USB serial,
# so the ONLY thing distinguishing them is the physical port path. Nothing else is
# available to match on, which is exactly why the port map is the right identity here.
#
# The collected paths are stored in ~/fm1-portmap.txt so --apply can build the rule file.
set -uo pipefail

MAP="${FM1_PORTMAP:-$HOME/fm1-portmap.txt}"
RULES=/etc/udev/rules.d/80-fm1.rules
SLOTS=(fm1-a fm1-b fm1-c fm1-d)

say()  { printf '== %s\n' "$*"; }
ok()   { printf '   [ok]   %s\n' "$*"; }
bad()  { printf '   [FAIL] %s\n' "$*" >&2; }
warn() { printf '   [warn] %s\n' "$*" >&2; }

# find_sg_path — the port path of the single download-mode unit, or fail loudly.
find_sg_path() {
    local sgs=()
    while IFS= read -r -d '' f; do sgs+=("$f"); done < <(find /dev -maxdepth 1 -name 'sg[0-9]*' -print0 2>/dev/null | sort -z)

    if [[ ${#sgs[@]} -eq 0 ]]; then
        bad "no /dev/sg* — is the unit in download mode? (m-flasher download-mode)"
        return 1
    fi
    if [[ ${#sgs[@]} -gt 1 ]]; then
        bad "${#sgs[@]} units in download mode (${sgs[*]})."
        bad "Connect exactly ONE, because download-mode units have no unique serial."
        return 1
    fi

    local sg="${sgs[0]}"
    local path
    path=$(udevadm info -q path -n "$sg" 2>/dev/null) || { bad "udevadm failed for $sg"; return 1; }
    # The raw path looks like:
    #   /devices/platform/soc/3f980000.usb/usb1/1-1/1-1.1/1-1.1.3/1-1.1.3:1.0/host0/.../sg0
    #
    # The USB port is the component BEFORE the ":1.0" interface suffix. Taking the last
    # component yields "sg0" — a DEVICE NAME, not a port — and KERNELS=="sg0" in a udev
    # rule silently never matches. (Caught on hardware 2026-11-21: the first version of
    # this script recorded "sg0" and looked plausible.)
    local tail=""
    local -a comps
    IFS='/' read -ra comps <<< "$path"
    local i comp
    for ((i = ${#comps[@]} - 1; i >= 0; i--)); do
        comp="${comps[$i]}"
        if [[ "$comp" =~ ^[0-9]+-[0-9]+(\.[0-9]+)*: ]]; then
            tail="${comp%%:*}"      # strip the ":1.0" interface suffix
            break
        fi
    done
    if [[ -z "$tail" ]]; then
        bad "could not find a USB port component in: $path"
        return 1
    fi
    printf '%s\t%s\t%s\n' "$tail" "$sg" "$path"
}

record() {
    local slot="$1" tail="$2"
    touch "$MAP"
    # replace any existing line for this slot (re-running is safe)
    if grep -q "^$slot	" "$MAP" 2>/dev/null; then
        tmp=$(mktemp); grep -v "^$slot	" "$MAP" > "$tmp"; mv "$tmp" "$MAP"
    fi
    printf '%s\t%s\n' "$slot" "$tail" >> "$MAP"
    sort -o "$MAP" "$MAP"
}

cmd_record() {
    local slot="${1:-}"
    [[ -n "$slot" ]] || { echo "usage: $0 <fm1-a|fm1-b|fm1-c|fm1-d> | --show | --apply" >&2; return 2; }
    local found
    found=$(find_sg_path) || return 1
    local tail sg path
    IFS=$'\t' read -r tail sg path <<< "$found"
    say "$slot  ->  port $tail   ($sg)"
    record "$slot" "$tail"
    ok "recorded in $MAP"
    printf '\n'
    cmd_show
}

cmd_show() {
    say "port map so far ($MAP)"
    if [[ ! -s "$MAP" ]]; then
        warn "empty — record a slot first: $0 fm1-a"
        return 0
    fi
    printf '   %-8s %-12s\n' SLOT PORT
    while IFS=$'\t' read -r slot tail; do
        printf '   %-8s %-12s\n' "$slot" "$tail"
    done < "$MAP"

    # warn about ports that are assigned twice
    local dups
    dups=$(cut -f2 "$MAP" | sort | uniq -d)
    if [[ -n "$dups" ]]; then
        bad "port(s) assigned to more than one slot: $dups"
    fi

    # A slot bound to a port that does not exist silently produces an absent unit, which
    # is safe but confusing. Probe the real ports and say which of the recorded ones are
    # real -- cheap check, and it catches the classic "stale example path" mistake.
    local missing=()
    while IFS=$'\t' read -r slot tail; do
        [[ -e /sys/bus/usb/devices/$tail ]] || missing+=("$tail ($slot)")
    done < "$MAP"
    if [[ ${#missing[@]} -gt 0 ]]; then
        warn "recorded port(s) that do not exist on this Pi right now:"
        printf '     %s\n' "${missing[@]}"
        warn "  (a hub port with nothing plugged in also shows here -- check 'lsusb -t')"
    fi
}

cmd_apply() {
    [[ -s "$MAP" ]] || { bad "$MAP is empty; record slots first"; return 1; }
    [[ $EUID -eq 0 ]] || { bad "--apply needs sudo"; return 1; }

    local tmp
    tmp=$(mktemp)
    {
        cat <<'HDR'
# FM-1 fleet: bind download-mode units to stable names by PHYSICAL USB PORT.
#
# Generated by label-units.sh — edit that script's output, not this file by hand.
# In download mode every unit announces "WL82 UBOOT1.00" with NO unique serial, so the
# port path is the only thing that can identify which unit is which. A unit is only
# matched here while it is IN DOWNLOAD MODE; a unit running firmware has no symlink,
# and that absence is expected rather than a fault.
#
# Re-apply after moving any cable:
#     sudo ./label-units.sh --apply
HDR
        echo
        # The vendor filter is not decoration: /dev/sg* is not exclusively an FM-1.
        # Any SCSI generic node could in principle collide with a KERNELS path, so the
        # rule also requires the FM-1 vendor id. Both ids are accepted because a
        # Felucca-family unit need not present the stock `4c4a`.
        while IFS=$'\t' read -r slot tail; do
            printf 'SUBSYSTEM=="scsi_generic", KERNELS=="%s", ATTRS{idVendor}=="4c4a", SYMLINK+="%s"\n' "$tail" "$slot"
            printf 'SUBSYSTEM=="scsi_generic", KERNELS=="%s", ATTRS{idVendor}=="1209", SYMLINK+="%s"\n' "$tail" "$slot"
        done < "$MAP"
        echo
        while IFS=$'\t' read -r slot tail; do
            printf 'SUBSYSTEM=="block", KERNELS=="%s", ATTRS{idVendor}=="4c4a", SYMLINK+="%s-disk"\n' "$tail" "$slot"
        done < "$MAP"
        cat <<'FTR'

# Permissions: reach the unit without root (m-flasher's own rule does this too;
# this is belt-and-braces for the symlinks above).
SUBSYSTEM=="scsi_generic", ATTRS{idVendor}=="4c4a", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="4c4a", MODE="0660", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", MODE="0660", GROUP="plugdev"
FTR
    } > "$tmp"

    if [[ -f "$RULES" ]]; then
        cp "$RULES" "$RULES.bak.$(date +%Y%m%d%H%M%S)"
        ok "backed up existing $RULES"
    fi
    install -m 0644 "$tmp" "$RULES"
    rm -f "$tmp"
    ok "wrote $RULES"

    udevadm control --reload-rules
    udevadm trigger --action=add --subsystem-match=scsi_generic
    udevadm trigger --action=add --subsystem-match=block
    ok "udev reloaded"

    echo
    say "verify: replug the unit (or run 'm-flasher download-mode'), then"
    for s in "${SLOTS[@]}"; do printf '   ls -l /dev/%s\n' "$s"; done
}

case "${1:-}" in
    --show)  cmd_show ;;
    --apply) cmd_apply ;;
    ""|-h|--help) sed -n '2,20p' "$0" ;;
    *) cmd_record "$1" ;;
esac
