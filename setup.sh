#!/usr/bin/env bash
# setup.sh — build the FM-1 Pi manager from a fresh Raspberry Pi OS Lite (64-bit) card.
#
# This is the DETERMINISTIC bake only. It does NOT touch calibration or the udev port
# map: those are per-panel / per-hub and are done by hand. See the Install section of
# README.md.
#
# Run on the Pi, from a checked-out copy of this repo:
#     sudo ./setup.sh
#
# Idempotent: safe to re-run. Steps:
#     1. guard: must be 64-bit and Debian-based
#     2. apt packages (usbutils, sg3-utils, evtest, python3-venv/pip)
#     3. sg module (load + persist)
#     4. m-flasher -- either install the .deb, or check the existing binary
#     5. fm1-switch (editable install in a repo-owned venv)
#     6. udev rules (install, but DO NOT fill the port map)
#     7. checks: m-flasher, sg, /dev/fm1-a present
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MFLASHER_DEB="${MFLASHER_DEB:-}"          # set to a .deb path to install it
FMSWITCH_DIR="${FMSWITCH_DIR:-$here/fm1-switch}"
# Default under the INVOKING user's home, not root's. The script runs under sudo, so a
# bare $HOME here would expand to /root and then `sudo -u $SUDO_USER mkdir` would fail
# with "cannot create directory '/root': Permission denied".
TARGET_USER="${SUDO_USER:-root}"
if [[ "$TARGET_USER" == "root" ]]; then
    TARGET_HOME="$HOME"
else
    TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    [[ -n "$TARGET_HOME" ]] || TARGET_HOME="$HOME"
fi

BACKUP_DIR="${BACKUP_DIR:-$TARGET_HOME/fm1-backups}"
LIBRARY_DIR="${LIBRARY_DIR:-$TARGET_HOME/fm1-firmwares}"

say()  { printf '== %s\n' "$*"; }
warn() { printf '!!! %s\n' "$*" >&2; }
done_marker() { printf -- '  -- %s\n' "$*"; }

need_root() {
    [[ $EUID -eq 0 ]] || { echo "error: run with sudo" >&2; exit 1; }
}

verify_arch() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        aarch64|arm64) return 0 ;;
        armv7l|armv6l|i686|x86_64)
            cat >&2 <<EOS
error: this image is NOT 64-bit ARM (uname -m = $arch, want aarch64).

The m-flasher package this setup expects is arm64, and will NOT install on a 32-bit OS.
Re-flash the card with Raspberry Pi OS Lite (64-bit) — Pi Imager, not a raw dd — and
re-run. (The Pi 3B+ is an ARMv8 chip; it runs the 64-bit image fine. 32-bit is the bug,
not the board.)

See the Install section of README.md.
EOS
            exit 1
            ;;
        *)
            echo "error: unknown architecture '$arch' — aborting" >&2
            exit 1
            ;;
    esac
}

installed() { command -v "$1" >/dev/null 2>&1; }

install_apt() {
    say "apt packages"
    if ! command -v apt-get >/dev/null 2>&1; then
        warn "not an apt system; install manually: usbutils sg3-utils evtest python3-venv python3-pip"
        return
    fi
    apt-get update -qq
    # --no-install-recommends keeps a Lite card lean; nothing here needs recommends.
    apt-get install -y --no-install-recommends \
        usbutils sg3-utils evtest python3-venv python3-pip
    done_marker "usbutils, sg3-utils, evtest, python3-venv/pip"
}

ensure_sg() {
    say "SCSI generic (sg) module"
    if ! lsmod | grep -Eq '^sg'; then
        modprobe sg || { warn "modprobe sg failed (sg3-utils installed?)"; return 1; }
    fi
    mkdir -p /etc/modules-load.d
    if ! grep -qx 'sg' /etc/modules-load.d/sg.conf 2>/dev/null; then
        echo 'sg' > /etc/modules-load.d/sg.conf
    fi
    done_marker "sg loaded and persisted in /etc/modules-load.d/sg.conf"
}

# resolve_mflasher — same precedence as fm1_switch.mflasher.binary():
# FM1_FLASHER wins, else PATH. Kept in one place so `setup.sh` and the tool that
# actually shells out can never disagree about what "installed" means.
resolve_mflasher() {
    if [[ -n "${FM1_FLASHER:-}" ]]; then
        if [[ -x "$FM1_FLASHER" ]]; then printf '%s\n' "$FM1_FLASHER"; return 0; fi
        return 1
    fi
    command -v m-flasher 2>/dev/null
}

install_mflasher() {
    say "m-flasher"
    if [[ -n "$MFLASHER_DEB" ]]; then
        if [[ ! -f "$MFLASHER_DEB" ]]; then
            warn "MFLASHER_DEB=$MFLASHER_DEB not found"
            return 1
        fi
        apt-get install -y "./${MFLASHER_DEB#./}"
        done_marker "installed $MFLASHER_DEB"
    elif bin="$(resolve_mflasher)"; then
        done_marker "already present: $bin"
    elif [[ -n "${FM1_FLASHER:-}" ]]; then
        warn "FM1_FLASHER=$FM1_FLASHER is set but not executable"
        return 1
    else
        cat >&2 <<EOS
!!! m-flasher NOT found and no MFLASHER_DEB was set.

Three ways to satisfy this:
  1. install the .deb (pulls the GUI stack, ~50 MB — see note below):
         sudo MFLASHER_DEB=/path/to/m-flasher_0.12.1-1_arm64.deb ./setup.sh
  2. use the tarball build (only needs libc/libm/libgcc — right for a headless
     console Pi), drop the binary somewhere and point at it:
         sudo FM1_FLASHER=/home/\$USER/bin/m-flasher ./setup.sh
  3. put it on PATH:  sudo ln -s /path/to/m-flasher /usr/local/bin/m-flasher

NOTE: the .deb declares hard Depends on libegl1 and libxkbcommon0 (the GUI build).
The linux-aarch64 tarball binary needs only libc/libm/libgcc, so on a headless Pi
the tarball avoids installing a graphics stack you will never use.
EOS
        return 1
    fi
}

install_python() {
    local dir="$1" name="$2"
    say "python package: $name"
    [[ -d "$dir" ]] || { warn "$dir not found (checked out the repo?)"; return 1; }

    local target_user="${SUDO_USER:-root}"
    local venv="$dir/.venv"
    if [[ ! -d "$venv" ]]; then
        sudo -u "$target_user" python3 -m venv "$venv" \
            || { warn "venv creation failed"; return 1; }
    fi
    sudo -u "$target_user" "$venv/bin/pip" install -q --upgrade pip
    sudo -u "$target_user" "$venv/bin/pip" install -q -e "$dir"

    # Verify the CONSOLE SCRIPT exists, not just that pip exited 0. A venv holding only
    # pytest still passes the test suite (tests import from the repo dir via sys.path),
    # so a missing script here is otherwise invisible until the runbook's §3.2 step.
    if [[ ! -x "$venv/bin/$name" ]]; then
        warn "$name installed but $venv/bin/$name is missing — check $dir/pyproject.toml [project.scripts]"
        return 1
    fi
    done_marker "$name at $venv/bin/$name"
}

install_udev() {
    say "udev rules (symlinks + download-mode permissions — port map is a bench step, NOT this script)"
    install -m 0644 "$FMSWITCH_DIR/udev/80-fm1.rules" /etc/udev/rules.d/80-fm1.rules
    udevadm control --reload-rules
    udevadm trigger
    done_marker "installed /etc/udev/rules.d/80-fm1.rules"
    done_marker "LEAVE the example port paths for now; edit them for your own hub (udevadm info -a)"
}

ensure_dirs() {
    say "directories"
    sudo -u "${SUDO_USER:-root}" mkdir -p "$BACKUP_DIR" "$LIBRARY_DIR"
    done_marker "$BACKUP_DIR (backups — copy OFF the Pi after you make them)"
    done_marker "$LIBRARY_DIR (firmware library)"
}

run_checks() {
    say "checks"
    local problems=0

    if bin="$(resolve_mflasher)"; then
        done_marker "m-flasher: $bin"
        # The tool shells out to this exact binary; prove it actually runs.
        if version="$("$bin" --version 2>/dev/null | head -1)"; then
            done_marker "m-flasher version: ${version:-unknown}"
        else
            warn "m-flasher: found at $bin but --version failed"
            problems=$((problems + 1))
        fi
    else
        warn "m-flasher: missing"
        problems=$((problems + 1))
    fi

    if lsmod | grep -Eq '^sg'; then
        done_marker "sg: loaded"
    else
        warn "sg: not loaded"
        problems=$((problems + 1))
    fi

    if [[ -c /dev/fm1-a ]]; then
        done_marker "/dev/fm1-a: present"
    else
        done_marker "/dev/fm1-a: not present yet — put ONE unit in download mode on its mapped port"
    fi

    return "$problems"
}

# ---------------------------------------------------------------------------
main() {
    need_root
    verify_arch

    say "FM-1 Pi manager (deterministic bake) — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    install_apt
    ensure_sg
    install_mflasher
    install_python "$FMSWITCH_DIR" fm1-switch
    install_udev
    ensure_dirs
    run_checks
    local problems=$?

    cat <<EOS

Done (this script only). What it did NOT do — and cannot, by design:
  * touch calibration                -> runbook §2.3  (evtest, per-panel)
  * the unit->port map (fm1-a..d)    -> runbook §4.2  (udevadm info, per-hub)
  * backing up your four units       -> runbook §4.4  (the important part)

Next, in order:
  1. runbook §2 — bring up the panel and calibrate touch
  2. ONE unit at a time: put it in download mode on its mapped port, run
     `m-flasher backup`, and copy the file OFF the Pi
  3. back up all four before installing anything (§4.4)

Verify it all resolves cleanly:
    .venv/bin/fm1-switch diagnose
EOS

    if [[ $problems -ne 0 ]]; then
        echo "exiting with $problems unresolved check(s)." >&2
        return 1
    fi
    return 0
}

main "$@"
