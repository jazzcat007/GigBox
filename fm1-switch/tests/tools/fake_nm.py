#!/usr/bin/env python3
"""A stand-in for ``nmcli`` and ``iw`` for the synthetic WiFi tests. It talks to no network.

Usage (the test makes tiny wrappers): ``fake_nm.py nmcli ARGS...`` or ``fake_nm.py iw ARGS...``.
State lives in the JSON file named by ``FAKE_NM_STATE`` so the test can change the world while the
real ``fm1-switch net`` code runs: ``{"state": "connected", "conn": "Home", "in_range": ["Home"],
"saved": ["Home", "Home-netplan"], "phones": 0, "has_hotspot": false, "fail_hotspot": false}``.
Every call is appended to ``FAKE_NM_LOG``.
"""
import fcntl
import json
import os
import sys

HOTSPOT = "fm1-hotspot"
PATH = os.environ["FAKE_NM_STATE"]
LOG = os.environ["FAKE_NM_LOG"]


def load():
    with open(PATH) as f:
        return json.load(f)


def save(st):
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, PATH)


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    with open(LOG, "a") as lf:
        lf.write(tool + " " + " ".join(args) + "\n")
    lock = open(PATH + ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    st = load()
    if tool == "iw":
        if st["state"] == "connected" and st["conn"] == HOTSPOT:
            for i in range(st.get("phones", 0)):
                print(f"Station aa:bb:cc:dd:ee:{i:02x} (on wlan0)")
        return 0
    a = [x for x in args if x != "-t"]
    if a[:3] == ["-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
        print(f"wlan0:wifi:{st['state']}:{st['conn'] if st['state'] == 'connected' else ''}")
        print("eth0:ethernet:unavailable:")
        return 0
    if a[:3] == ["-f", "NAME,TYPE,AUTOCONNECT", "connection"]:
        for n in st["saved"]:
            print(f"{n}:802-11-wireless:yes")
        if st.get("has_hotspot"):
            print(f"{HOTSPOT}:802-11-wireless:no")
        return 0
    if a[:2] == ["-f", "NAME"]:
        for n in st["saved"] + ([HOTSPOT] if st.get("has_hotspot") else []):
            print(n)
        return 0
    if a[:1] == ["-g"] and "802-11-wireless.mode" in a:
        print("infrastructure")
        return 0
    if a[:1] == ["-g"] and "connection.autoconnect-priority" in a:
        print("0")
        return 0
    if a[:1] == ["-g"] and "IP4.ADDRESS" in a:
        print("10.42.0.1/24" if st["conn"] == HOTSPOT else "192.168.1.20/24")
        return 0
    if a[:2] == ["-f", "PERMISSION,VALUE"] or a[:3] == ["-f", "PERMISSION,VALUE", "general"]:
        for perm in ("network-control", "settings.modify.system", "wifi.share.protected"):
            print(f"org.freedesktop.NetworkManager.{perm}:yes")
        return 0
    if a[:2] == ["device", "disconnect"]:
        st["state"], st["conn"] = "disconnected", ""
        save(st)
        return 0
    if a[:2] == ["connection", "add"]:
        st["has_hotspot"] = True
        save(st)
        return 0
    if a[:2] == ["connection", "modify"]:
        return 0
    if "up" in a:
        name = a[-1]
        if name == HOTSPOT:
            if st.get("fail_hotspot"):
                return 4
            st["state"], st["conn"] = "connected", name
        elif name in st["in_range"]:
            st["state"], st["conn"] = "connected", name
        else:
            if st["conn"] == HOTSPOT:       # joining drops the hotspot even when it fails
                st["state"], st["conn"] = "disconnected", ""
            save(st)
            return 4
        save(st)
        return 0
    if "down" in a:
        st["state"], st["conn"] = "disconnected", ""
        save(st)
        return 0
    return 0


sys.exit(main())
