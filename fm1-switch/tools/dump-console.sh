#!/bin/sh
# Print exactly what the panel's console is showing, as text.
#
# Why this exists: debugging the panel through photographs wastes a cycle each time.
# /dev/vcsN holds the VT's character buffer, so it can be read and compared to what the
# UI *thinks* it drew. Requires membership of the `tty` group (sudo usermod -aG tty USER).
#
# Usage: ./dump-console.sh [N]   (default tty1)
N=${1:-1}
DEV=/dev/vcs$N
if [ ! -e "$DEV" ]; then
    echo "no $DEV" >&2; exit 1
fi
if [ ! -r "$DEV" ]; then
    echo "cannot read $DEV: add yourself to the tty group" >&2; exit 1
fi
# vcs gives one byte per cell, no attributes, with trailing spaces per line.
cat "$DEV" | tr -d '\000' | sed 's/[[:space:]]*$//' | cat -n | sed 's/^ *//'
