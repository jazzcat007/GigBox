#!/usr/bin/env bash
# bundle-archives.sh — pack the irreplaceable stuff into ONE verified file you can copy.
#
# What goes in:
#   * archives/units/    — the four units' factory flash images (+ sidecars + REGISTRY.md)
#   * archives/V15-FM-1.fwsc            — official V15 (the restore path refuses V16)
#   * archives/fm1-factory-settings-only.bin      — for `repair-settings --factory`
#   * archives/fm1-transporter-dump-*.bin         — full factory image
#   * archives/MANIFEST.txt
#   * verify-units.sh    — so the recipient can re-check without this repo
#
# What does NOT go in: .venv, __pycache__, node_modules. This is data, not environment.
#
# Usage:
#     ./bundle-archives.sh                 # -> archives/fm1-archive-<date>.tar.gz + .sha256
#     OUT=/Volumes/BACKUP ./bundle-archives.sh
#
# The script verifies BEFORE and AFTER packing, and writes a .sha256 next to the tarball.
# Copy BOTH files to the second disk, then run:
#     shasum -a 256 -c fm1-archive-<date>.tar.gz.sha256
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/archives"
DATE="$(date -u +%Y-%m-%d)"
OUT="${OUT:-$here/archives}"
NAME="fm1-archive-$DATE"

say()  { printf '== %s\n' "$*"; }
ok()   { printf '   [ok]   %s\n' "$*"; }
bad()  { printf '   [FAIL] %s\n' "$*" >&2; }
warn() { printf '   [warn] %s\n' "$*" >&2; }

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

main() {
    [[ -d "$src/units" ]] || { printf 'error: %s/units not found\n' "$src" >&2; return 2; }

    # ---- 1. verify the source before we seal it in an archive.
    say "pre-flight: verifying unit backups"
    if ! ARCHIVE_DIR="$src/units" "$here/verify-units.sh" >/tmp/vu.$$ 2>&1; then
        cat /tmp/vu.$$ >&2
        rm -f /tmp/vu.$$
        bad "verification failed — refusing to bundle a bad archive"
        return 1
    fi
    grep -E 'ALL CHECKS PASSED|\[ok\]' /tmp/vu.$$ | tail -6
    rm -f /tmp/vu.$$

    # ---- 2. sanity: the files that must be present.
    say "pre-flight: required files"
    local missing=0
    local required=(
        "V15-FM-1.fwsc"
        "MANIFEST.txt"
        "units/REGISTRY.md"
    )
    for f in "${required[@]}"; do
        if [[ -e "$src/$f" ]]; then ok "$f"; else bad "missing $f"; missing=$((missing + 1)); fi
    done
    local dumps
    dumps=$(find "$src" -maxdepth 1 -name '*factory*' -o -maxdepth 1 -name '*transporter-dump*' | wc -l | tr -d ' ')
    if [[ "$dumps" -ge 1 ]]; then ok "$dumps factory dump(s)"; else warn "no factory dumps found"; fi
    if [[ $missing -gt 0 ]]; then bad "aborting: $missing required file(s) missing"; return 1; fi

    # ---- 3. pack.
    mkdir -p "$OUT"
    local tar="$OUT/$NAME.tar.gz"
    say "packing -> $tar"
    # -C so paths are relative (fm1-archive-.../archives/...), no absolute paths baked in.
    local stage
    stage="$(mktemp -d)"
    mkdir -p "$stage/$NAME"
    cp -R "$src/units"                       "$stage/$NAME/"
    cp -R "$src/V15-FM-1.fwsc"               "$stage/$NAME/" 2>/dev/null
    cp -R "$src/MANIFEST.txt"                "$stage/$NAME/" 2>/dev/null
    find "$src" -maxdepth 1 \( -name '*factory*' -o -name '*transporter-dump*' \) \
        -exec cp {} "$stage/$NAME/" \; 2>/dev/null
    cp "$here/verify-units.sh" "$stage/$NAME/" 2>/dev/null
    chmod +x "$stage/$NAME/verify-units.sh" 2>/dev/null

    cat > "$stage/$NAME/README.txt" <<'EOS'
FM-1 archive bundle
===================

CONTENTS
  units/                      the four FM-1s' factory flash images (+ .json sidecars,
                              + REGISTRY.md listing unit id / chip / firmware / sha256)
  V15-FM-1.fwsc               official V15 package (sha256 db1642b2...). The unbricker's
                              `restore` refuses V16, which is all M-VAVE ships now, so this
                              file is what keeps "back to stock" possible.
  *factory*.bin               factory dumps, for `repair-settings --factory`
  verify-units.sh             re-verify every unit backup

WHY THIS EXISTS
  The four unit images cannot be re-downloaded from anywhere — they are the only copy of
  each unit's factory state (confirmed: 0/143 sectors match the public V15 package). Keep
  TWO copies on separate physical media.

VERIFY
  cd <extracted dir>
  ARCHIVE_DIR=./units ./verify-units.sh
  # expect: ALL CHECKS PASSED

  For the tarball itself, run this FROM THE DIRECTORY CONTAINING BOTH FILES
  (the .sha256 lists a bare filename, so -c resolves it relative to your cwd):
  shasum -a 256 -c fm1-archive-<date>.tar.gz.sha256

RESTORING A UNIT
  Read REGISTRY.md first — units differ! Three have flash chip 0x856014 (old), one has
  0xB36014 (new, which needs firmware that knows it). Restore only the image whose sha256
  and unit id match the target, or you will overwrite another unit's factory data.
EOS

    ( cd "$stage" && tar czf "$tar" "$NAME" ) || { bad "tar failed"; rm -rf "$stage"; return 1; }
    rm -rf "$stage"
    ok "packed $(du -h "$tar" | cut -f1)"

    # ---- 4. verify the tarball itself, then hash it.
    say "post-flight: verifying the tarball"
    if tar tzf "$tar" >/dev/null 2>&1; then ok "gzip integrity ok"; else bad "tarball is corrupt"; return 1; fi
    local members
    members=$(tar tzf "$tar" | grep -c '\.bin$')
    ok "$members .bin member(s)"

    local sum
    sum="$(sha256_of "$tar")"
    printf '%s  %s\n' "$sum" "$(basename "$tar")" > "$tar.sha256"
    ok "sha256 $sum"
    ok "wrote $tar.sha256"

    cat <<EOS

NEXT — copy BOTH files to a second physical disk:
    $tar
    $tar.sha256

Then, on the other machine/disk (from the same directory as both files):
    shasum -a 256 -c $(basename "$tar").sha256
    tar xzf $(basename "$tar") && cd $NAME && ARCHIVE_DIR=./units ./verify-units.sh

An archive that exists in one place is one failure away from not existing.
EOS
    return 0
}

main "$@"
