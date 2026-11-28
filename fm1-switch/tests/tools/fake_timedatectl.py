#!/usr/bin/env python3
"""A stand-in for ``timedatectl`` for the clock tests (FM1_TIMEDATECTL). It touches no clock.
State is the JSON file FAKE_TD_STATE: {"ntp": true, "synced": false, "time": "", "deny": false};
every call is appended to FAKE_TD_LOG. Like the real one it refuses set-time while network time is on."""
import json
import os
import sys

state_path, log_path = os.environ["FAKE_TD_STATE"], os.environ["FAKE_TD_LOG"]
args = sys.argv[1:]
with open(log_path, "a") as f:
    f.write(" ".join(args) + "\n")
st = json.load(open(state_path))


def save():
    json.dump(st, open(state_path, "w"))


if args[:1] == ["show"]:
    print(f"NTP={'yes' if st['ntp'] else 'no'}")
    print(f"NTPSynchronized={'yes' if st['synced'] else 'no'}")
elif args[:1] == ["set-ntp"]:
    if st.get("deny"):
        print("Failed to set ntp: Access denied", file=sys.stderr)
        sys.exit(1)
    st["ntp"] = args[1] == "true"
    save()
elif args[:1] == ["set-time"]:
    if st.get("deny"):
        print("Failed to set time: Access denied", file=sys.stderr)
        sys.exit(1)
    if st["ntp"]:
        print("Failed to set time: Automatic time synchronization is enabled", file=sys.stderr)
        sys.exit(1)
    if st.get("fail_set"):
        print("Failed to set time: Invalid time", file=sys.stderr)
        sys.exit(1)
    st["time"] = args[1]
    save()
else:
    sys.exit(2)
