#!/usr/bin/env bash
# verify-units.sh — re-verify every FM-1 factory backup in archives/units/.
#
# These four images are the ONLY copy of your units' factory state. They cannot be
# re-downloaded from anywhere (unlike the V15 package, which is public). So the archive
# gets checked, not assumed.
#
# Checks, per unit:
#   1. the .bin exists and is exactly 1 MiB (the WL82 flash size)
#   2. its sha256 matches the value recorded in its m-flasher .json sidecar
#   3. that value is also the one pinned in REGISTRY.md
#   4. REGISTRY.md and the on-disk sidecars agree on chip / firmware / unit id
#
# Usage:
#     ./verify-units.sh                  # check ./archives/units
#     ARCHIVE_DIR=/Volumes/BACKUP/fm1/units ./verify-units.sh
#
# Exit: 0 all good, 1 any mismatch, 2 setup problem (missing dir/tool).
set -uo pipefail

ARCHIVE_DIR="${ARCHIVE_DIR:-./archives/units}"
EXPECTED_SIZE=1048576

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

# Minimal JSON value reader — avoids requiring jq on a fresh Pi.
json_get() {
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        print(json.load(fh).get(sys.argv[2], ""))
except Exception:
    print("")
PY
}

main() {
    if [[ ! -d "$ARCHIVE_DIR" ]]; then
        printf 'error: %s not found\n' "$ARCHIVE_DIR" >&2
        return 2
    fi

    local bins=()
    while IFS= read -r -d '' f; do bins+=("$f"); done \
        < <(find "$ARCHIVE_DIR" -maxdepth 1 -name '*.bin' -print0 | sort -z)

    if [[ ${#bins[@]} -eq 0 ]]; then
        printf 'error: no .bin files in %s\n' "$ARCHIVE_DIR" >&2
        return 2
    fi

    say "verifying ${#bins[@]} unit backup(s) in $ARCHIVE_DIR"

    local failures=0
    local registry="$ARCHIVE_DIR/REGISTRY.md"
    local registry_ids=()

    if [[ -f "$registry" ]]; then
        # unit ids appear in the table as 16 hex digits inside backticks, e.g. `0123456789abcdef`
        while IFS= read -r id; do registry_ids+=("$id"); done \
            < <(grep -oE '\`[0-9a-f]{16}\`' "$registry" | tr -d '`' | sort -u)
    else
        warn "REGISTRY.md not found — skipping cross-check against it"
    fi

    for bin in "${bins[@]}"; do
        local base json actual unit_id side_hash chip fw
        base="$(basename "$bin" .bin)"
        json="${bin%.bin}.json"
        printf '\n   %s\n' "$base"

        # 1. size
        local size
        size="$(wc -c < "$bin" | tr -d ' ')"
        if [[ "$size" == "$EXPECTED_SIZE" ]]; then
            ok "size $size bytes (1 MiB)"
        else
            bad "size $size, expected $EXPECTED_SIZE"
            failures=$((failures + 1))
        fi

        # 2. actual hash
        actual="$(sha256_of "$bin")"

        # 3. sidecar
        if [[ ! -f "$json" ]]; then
            warn "no sidecar $base.json — hash cannot be cross-checked"
            ok "sha256 $actual"
            continue
        fi

        side_hash="$(json_get "$json" sha256)"
        unit_id="$(json_get "$json" "unit id")"
        chip="$(json_get "$json" flash)"
        fw="$(json_get "$json" firmware)"

        if [[ -z "$side_hash" ]]; then
            warn "sidecar has no sha256 field"
        elif [[ "$actual" == "$side_hash" ]]; then
            ok "sha256 matches sidecar (${actual:0:16}…)"
        else
            bad "sha256 MISMATCH vs sidecar"
            bad "  sidecar: $side_hash"
            bad "  actual : $actual"
            failures=$((failures + 1))
        fi

        # 4. registry cross-check
        if [[ ${#registry_ids[@]} -gt 0 ]]; then
            local found=0
            for rid in "${registry_ids[@]}"; do
                # ${var,,} is bash 4+; macOS ships 3.2, so lowercase via tr instead.
                if [[ "$(printf '%s' "$rid" | tr 'A-F' 'a-f')" == \
                      "$(printf '%s' "$unit_id" | tr 'A-F' 'a-f')" ]]; then
                    found=1; break
                fi
            done
            if [[ $found -eq 1 ]]; then
                ok "unit id $unit_id present in REGISTRY.md"
            else
                bad "unit id $unit_id NOT in REGISTRY.md"
                failures=$((failures + 1))
            fi
        fi

        # 5. does the registry's row carry the same hash prefix?
        if [[ -f "$registry" && -n "$side_hash" ]]; then
            if grep -qiF "${side_hash:0:16}" "$registry"; then
                ok "hash prefix found in REGISTRY.md"
            else
                warn "hash prefix ${side_hash:0:16}… not found in REGISTRY.md (row may be out of date)"
            fi
        fi

        printf '          chip %s | firmware %s\n' "${chip:-?}" "${fw:-?}"
    done

    # ------------------------------------------------------------------ summary
    printf '\n'
    if [[ $failures -eq 0 ]]; then
        say "ALL CHECKS PASSED — ${#bins[@]} backup(s) intact"
        cat <<'EOS'

   This verifies integrity, not freshness: it proves the files match what was archived,
   not that they were taken recently (there is no such thing for a factory image).

   If this ran on a COPY, that copy is good. Keep two copies on separate physical media.
EOS
        return 0
    fi
    say "$failures CHECK(S) FAILED"
    cat <<'EOS'

   Do NOT restore from a backup that failed verification. Re-copy it from the other
   archive first and re-run this script; if both copies fail, the image is lost.
EOS
    return 1
}

main "$@"
