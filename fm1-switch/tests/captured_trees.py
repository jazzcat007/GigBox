"""Rebuild the bench Pi's real sysfs, from tests/captures/, under a tmp dir (audit B15).

The captures are text; this turns them back into the files and links the registry reads.
"""

import os
from pathlib import Path

CAPTURES = Path(__file__).parent / "captures"


def _devices(text: str) -> dict[str, dict[str, str]]:
    devs: dict[str, dict[str, str]] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("-- ") and "/" not in line and line[3:4].isdigit():
            cur = devs.setdefault(line[3:].strip(), {})
        elif line.startswith("== ") and line[3:4].isdigit():
            cur = devs.setdefault(line[3:].strip(), {})
        elif line.startswith(("==", "--")):
            cur = None if not line[3:4].isdigit() else cur
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
    return devs


def build(root: Path, *, download: bool, usb_capture: str | None = None) -> dict[str, str]:
    """Create ``root/usb`` (the USB device tree) and ``root/scsi_generic``; return paths.

    ``download=False``: every unit running. ``download=True``: unit A's port reports the
    ROM loader and has the sg0 node, exactly as captured after the soft key.
    ``usb_capture`` names another sysfs capture to replay instead (no sg0 is added then)."""
    name = usb_capture or ("sysfs-after-softkey-fm1-a.raw" if download else "sysfs-all-running.raw")
    text = (CAPTURES / name).read_text(encoding="utf-8")
    text = text.split("== scsi_generic")[0]
    devs = _devices(text)
    for line in (CAPTURES / "bcddevice-all-running.raw").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("bcdDevice=") and parts[0] in devs:
            devs[parts[0]]["bcdDevice"] = parts[1].split("=", 1)[1]
    usb = root / "usb"
    for port, attrs in devs.items():
        d = usb / port
        d.mkdir(parents=True)
        for k, v in attrs.items():
            (d / k).write_text(v + "\n", encoding="utf-8")
    sg = root / "scsi_generic"
    sg.mkdir()
    if download and usb_capture is None:
        # The captured chain: .../usb1/1-1/1-1.1/1-1.1.2/1-1.1.2:1.0/host0/target0:0:0/0:0:0:0/scsi_generic/sg0
        chain = (root / "devices/usb1/1-1/1-1.1/1-1.1.2/1-1.1.2:1.0/host0/target0:0:0/0:0:0:0/scsi_generic/sg0")
        chain.mkdir(parents=True)
        (sg / "sg0").symlink_to(chain)
    return {"usb": str(usb), "sg": str(sg)}


def build_snd(root: Path, capture: str) -> str:
    """Rebuild ``root/snd`` (the ALSA node directory) from a captured ``ls -la /dev/snd``
    listing plus its ``by-path`` links; return the directory.

    Device nodes become empty files of the same name (the soft key writes to the one named
    midiC<N>D0, so tests read what landed in it). Each by-path link keeps the captured
    target, e.g. ``platform-...usb-0:1.3:1.0 -> ../controlC0``."""
    snd = root / "snd"
    (snd / "by-path").mkdir(parents=True)
    for line in (CAPTURES / capture).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if line.startswith("crw") and len(parts) >= 10:
            (snd / parts[-1]).touch()
        elif line.startswith("lrwx") and "->" in parts:
            arrow = parts.index("->")
            os.symlink(parts[arrow + 1], snd / "by-path" / parts[arrow - 1])
    return str(snd)


def add_sound_cards(usb: Path, snd: Path) -> None:
    """Give each USB port's interface the ``sound/cardN`` child the kernel makes for it.

    The sysfs captures do not list interface children, but the by-path links in the same
    session do: ``...usb-0:1.1.2:1.0 -> ../controlC3`` says the port ``1-1.1.2`` owns card 3.
    That is the number ``softkey._card_number_of_port`` reads back from sysfs. usbv2 aliases
    describe the same device and are skipped, as the code does."""
    for link in (snd / "by-path").iterdir():
        if "usbv2" in link.name or "usb-usb-0:" not in link.name:
            continue
        chain = link.name.split("usb-usb-0:", 1)[1].rsplit(":1.0", 1)[0]   # "1.1.2"
        card = os.readlink(link).rsplit("controlC", 1)[1]                  # "3"
        port = "1-" + chain
        (usb / port / f"{port}:1.0" / "sound" / f"card{card}").mkdir(parents=True)
