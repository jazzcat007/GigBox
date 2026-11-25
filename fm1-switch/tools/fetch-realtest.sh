#!/bin/sh
# Fetch the five real packages we have put on hardware, for the parser tests.
#
# These are the firmwares whose behaviour we actually know: Felucca, SLOOP and OberVave
# installed cleanly from m-flasher; Melodee and ChoralRoot booted and then reported
# missing content. tests/test_web_hub.py asserts the inspector still separates them.
#
# SHRUB and V15 are added separately: SHRUB is not on the hub and publishes no checksum
# (the hash below is our own pin, archives/FM1-firmware-install-model.md), and V15 is on
# the hub as fw/mvave/FM-1_015.fwsc with a different filename.
#
# Hashes come from the hub catalogue at run time, never typed in by hand — the first
# version of this script carried two invented hashes, which is exactly the kind of
# mistake a checksum is supposed to prevent.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
dir="$here/realtest"
cache="$here/realtest/.catalog.json"
mkdir -p "$dir"

UA="fm1-fleet/0.1 (test fixture fetch; personal use)"

if [ ! -f "$cache" ]; then
    curl -fsSL -A "$UA" -o "$cache" https://thefirmwarehub.com/catalog.json
fi

python3 - "$cache" <<'PY' > "$dir/.expected"
import json, sys
cat = json.load(open(sys.argv[1]))
want = {"sloop", "felucca", "obervave", "melodee", "choralroot"}
for f in cat["firmwares"]:
    if f["id"] in want:
        print(f["id"], f["pkg"].rsplit("/", 1)[-1], f["sha256"])
PY

fail=0
while read -r id name sha; do
    if [ -f "$dir/$name" ] && [ "$(sha256sum "$dir/$name" | cut -d' ' -f1)" = "$sha" ]; then
        echo "ok      $name (already present)"
        continue
    fi
    echo "fetch   $name"
    if ! curl -fsSL -A "$UA" -o "$dir/$name.part" "https://thefirmwarehub.com/fw/$name"; then
        echo "FAILED  $name (not downloadable)" >&2
        rm -f "$dir/$name.part"
        continue
    fi
    got=$(sha256sum "$dir/$name.part" | cut -d' ' -f1)
    if [ "$got" != "$sha" ]; then
        rm -f "$dir/$name.part"
        echo "MISMATCH $name: catalogue $sha, got $got" >&2
        fail=1
        continue
    fi
    mv "$dir/$name.part" "$dir/$name"
    echo "ok      $name"
done < "$dir/.expected"

# V15: served by the hub under its own name.
v15_sha=db1642b2b6fa5c2cccb11ffd13878068bb28601678d3644049f99dc40e7edb8a
if [ ! -f "$dir/V15-FM-1.fwsc" ] || [ "$(sha256sum "$dir/V15-FM-1.fwsc" | cut -d' ' -f1)" != "$v15_sha" ]; then
    echo "fetch   V15-FM-1.fwsc"
    curl -fsSL -A "$UA" -o "$dir/V15-FM-1.fwsc.part" "https://thefirmwarehub.com/fw/mvave/FM-1_015.fwsc" \
        && got=$(sha256sum "$dir/V15-FM-1.fwsc.part" | cut -d' ' -f1) \
        && if [ "$got" = "$v15_sha" ]; then mv "$dir/V15-FM-1.fwsc.part" "$dir/V15-FM-1.fwsc"; echo "ok      V15-FM-1.fwsc"; else echo "MISMATCH V15" >&2; rm -f "$dir/V15-FM-1.fwsc.part"; fi
fi

# SHRUB: not on the hub. Copy it from the Pi's library if it is there.
if [ ! -f "$dir/SHRUB.fwsc" ]; then
    echo "note    SHRUB.fwsc is not on the hub; copy it from ~/fm1-firmwares to $dir/"
fi

rm -f "$dir/.expected"
echo
echo "Now: .venv/bin/python -m pytest tests/test_web_hub.py -q"
exit "$fail"
