"""Read-only probe of one running unit over USB-MIDI. Run it with the maker-tools venv's python
(it needs ``mido`` and ``python-rtmidi``, which this package does not), never imported:

    python maker_probe.py <alsa-client-number>

It sends only the editor INFO request (``F0 7D 46 4C 01 F7``) and prints one JSON line:
``{"firmware": "JIANT V10.7.81", "bt": 1}`` where ``bt`` is the Bluetooth-part tag (1 present,
2 missing, 0 no tag). Nothing is written to the unit. Exit 3 when the unit does not answer.
"""

import json
import sys
import time


def parse_info(data: list[int]) -> dict:
    """The unit's INFO reply (the bytes after F0 and before F7, header included)."""
    body = data[4:]
    text = bytes(body[: body.index(0)] if 0 in body else body).decode("ascii", "replace")
    n = len(body)
    ok = (n >= 10 and body[n - 10] == 0x58 and body[n - 9] == 1 and body[n - 7] == 0x43
          and body[n - 6] == 2 and body[n - 3] == 0x59)
    return {"firmware": text, "bt": body[n - 8] if ok else 0}


def main(argv: list[str]) -> int:
    import mido
    client = f" {int(argv[1])}:0"
    ins = [n for n in mido.get_input_names() if n.endswith(client)]
    outs = [n for n in mido.get_output_names() if n.endswith(client)]
    if len(ins) != 1 or len(outs) != 1:
        print(json.dumps({"error": "port not found"}))
        return 3
    with mido.open_input(ins[0]) as inp, mido.open_output(outs[0]) as out:
        for _ in inp.iter_pending():
            pass
        out.send(mido.Message("sysex", data=[0x7D, 0x46, 0x4C, 1]))
        end = time.time() + 3
        while time.time() < end:
            msg = inp.poll()
            if msg is None:
                time.sleep(0.02)
                continue
            if msg.type == "sysex" and list(msg.data[:4]) == [0x7D, 0x46, 0x4C, 1]:
                print(json.dumps(parse_info(list(msg.data))))
                return 0
    print(json.dumps({"error": "no answer"}))
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
