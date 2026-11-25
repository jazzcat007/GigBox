#!/usr/bin/env bash
# prepare-archives.sh — put the load-bearing FM-1 recovery files somewhere safe.
#
# These two things are the ones whose loss breaks recovery permanently:
#
#   1. Official V15 package (FM-1.fwsc)  — the unbricker's `restore` writes only
#      0x4000..0x92FFF and REFUSES V16, which is all M-VAVE ships now. If V15
#      disappears from the internet, `restore` stops working entirely.
#
#   2. Factory dump (DUMP.bin) — needed by `repair-settings --factory`, the only fix
#      for the known post-custom-firmware presets corruption.
#
# Usage (on the Mac, or anywhere with curl):
#     ./prepare-archives.sh                 # use ./archives as the vault
#     ARCHIVE_DIR=/somewhere ./prepare-archives.sh
#     ./prepare-archives.sh --check         # verify what is already there, download nothing
#
# Design note on VERIFICATION: where we know a hash we check it and FAIL on mismatch.
# Where we do not know a hash, we RECORD it into MANIFEST.txt and label it UNPINNED
# rather than claiming a check we did not perform. A false "verified" is the one
# outcome worse than no verification at all.
set -euo pipefail

ARCHIVE_DIR="${ARCHIVE_DIR:-./archives}"
REPO_RAW="https://raw.githubusercontent.com/Quixotic7/FM1Backups/main"
CHECK_ONLY=0

# Pinned values, verified against the live upstream on 2026-11-21.
#
# V15: full sha256 computed from the downloaded file and confirmed to match the
# db1642b2... prefix recorded in the unbricker research. Size 699956 bytes.
V15_SHA256="db1642b2b6fa5c2cccb11ffd13878068bb28601678d3644049f99dc40e7edb8a"
V15_SIZE=699956

# NOTE ON PATHS: the upstream layout is FLAT at the repo root. V15 is
# `V15-FM-1.fwsc`, NOT `official/FM-1.fwsc` (that path 404s). Verified 2026-11-21.
V15_NAME="V15-FM-1.fwsc"

# Factory dumps inside new-unit-factory-20261017/. Both are 1 MiB (full flash images).
# `fm1-factory-settings-only.bin` is the settings-region dump that `repair-settings
# --factory` wants; the transporter dump is a full image. We fetch both and do not
# pretend to know which a given unbricker subcommand wants.
FACTORY_DIR="new-unit-factory-20261017"
FACTORY_FILES=(
    "fm1-factory-settings-only.bin"
    "fm1-transporter-dump-20261017-001153.bin"
)

say()  { printf '== %s\n' "$*"; }
ok()   { printf '   [ok]   %s\n' "$*"; }
warn() { printf '   [warn] %s\n' "$*" >&2; }
bad()  { printf '   [FAIL] %s\n' "$*" >&2; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: need $1" >&2; exit 1; }; }
need curl
need sha256sum 2>/dev/null || need shasum

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# fetch <url> <dest> — download unless present; never overwrite a good file with a bad one.
fetch() {
    local url="$1" dest="$2"
    if [[ -f "$dest" && -s "$dest" ]]; then
        ok "already present: $(basename "$dest")"
        return 0
    fi
    if [[ $CHECK_ONLY -eq 1 ]]; then
        bad "missing: $(basename "$dest")"
        return 1
    fi
    printf '   ...    downloading %s\n' "$(basename "$dest")"
    if ! curl -fsSL --retry 3 --connect-timeout 20 -o "$dest.part" "$url"; then
        rm -f "$dest.part"
        bad "download failed: $url"
        return 1
    fi
    mv "$dest.part" "$dest"
    ok "downloaded $(basename "$dest")"
}

manifest_line() {
    # name  sha256  status  source
    printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$ARCHIVE_DIR/MANIFEST.txt"
}

verify_pinned() {
    local file="$1" want="$2" label="$3"
    local sum
    sum="$(sha256_of "$file")"
    if [[ "$sum" == "$want" ]]; then
        ok "$label sha256 verified"
        manifest_line "$(basename "$file")" "$sum" "VERIFIED" "$label"
        return 0
    fi
    bad "$label sha256 MISMATCH"
    bad "  expected: $want"
    bad "  actual:   $sum"
    manifest_line "$(basename "$file")" "$sum" "MISMATCH" "$label"
    return 1
}

verify_unpinned() {
    local file="$1" label="$2"
    local sum
    sum="$(sha256_of "$file")"
    warn "$label: hash recorded but NOT verified (no pinned value)"
    manifest_line "$(basename "$file")" "$sum" "UNPINNED" "$label"
    return 0
}

# ---------------------------------------------------------------------------
main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check) CHECK_ONLY=1 ;;
            -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
            *) echo "unknown arg: $1" >&2; exit 2 ;;
        esac
        shift
    done

    mkdir -p "$ARCHIVE_DIR"
    (
        cd "$ARCHIVE_DIR" || exit 1
        : > MANIFEST.txt
        printf '# FM-1 archive manifest — generated %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> MANIFEST.txt
        printf '# name\tsha256\tstatus\tsource\n' >> MANIFEST.txt
    )

    local failures=0

    say "1/2 official V15 package ($V15_NAME)"
    if fetch "$REPO_RAW/$V15_NAME" "$ARCHIVE_DIR/$V15_NAME"; then
        verify_pinned "$ARCHIVE_DIR/$V15_NAME" "$V15_SHA256" "official V15" \
            || failures=$((failures + 1))
        local got_size
        got_size="$(wc -c < "$ARCHIVE_DIR/$V15_NAME" | tr -d ' ')"
        if [[ "$got_size" == "$V15_SIZE" ]]; then
            ok "size verified ($V15_SIZE bytes)"
        else
            bad "size mismatch: expected $V15_SIZE, got $got_size"
            failures=$((failures + 1))
        fi
    else
        failures=$((failures + 1))
        warn "if the upstream layout changed, check: https://github.com/Quixotic7/FM1Backups"
    fi

    say "2/2 factory dumps ($FACTORY_DIR/)"
    # Hashes are NOT pinned for these: we record what we got and label it UNPINNED rather
    # than claim a verification we did not perform. A false "verified" is worse than none.
    local dumps_found=0
    for f in "${FACTORY_FILES[@]}"; do
        if fetch "$REPO_RAW/$FACTORY_DIR/$f" "$ARCHIVE_DIR/$f"; then
            verify_unpinned "$ARCHIVE_DIR/$f" "factory dump"
            dumps_found=$((dumps_found + 1))
        else
            warn "could not fetch $FACTORY_DIR/$f"
        fi
    done
    if [[ $dumps_found -eq 0 ]]; then
        failures=$((failures + 1))
        warn "no factory dump obtained — fetch manually from:"
        warn "  https://github.com/Quixotic7/FM1Backups/tree/main/$FACTORY_DIR"
        warn "then drop the file(s) into $ARCHIVE_DIR/ and re-run --check"
    fi

    say "summary"
    if command -v column >/dev/null 2>&1; then
        column -t -s $'\t' "$ARCHIVE_DIR/MANIFEST.txt" 2>/dev/null || tail -n +4 "$ARCHIVE_DIR/MANIFEST.txt"
    else
        tail -n +4 "$ARCHIVE_DIR/MANIFEST.txt"
    fi

    cat <<EOS

Archive dir: $ARCHIVE_DIR

NEXT — this is the part that actually matters:
  Copy this directory to a SECOND physical place (another disk, not the same drive).
  An archive on one disk is a single point of failure, which is the exact thing these
  two files exist to prevent.

Verified vs unpinned: VERIFIED means the hash matched a pinned value — V15 is pinned to its
full sha256 + size, so a mismatch there is a hard failure. UNPINNED means the hash was
RECORDED but could not be checked: treat the factory dumps as "we know what we have", not
"we know it is right". Pin them once you have a second, independent source to compare to.

To check an existing vault without downloading anything:
    ./prepare-archives.sh --check
EOS

    return "$failures"
}

main "$@"
