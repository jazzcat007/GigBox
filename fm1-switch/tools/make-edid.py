#!/usr/bin/env python3
"""Build a minimal EDID 1.3 for the 5" 800x480 panel (which sends none of its own).

Layout matters: standard timings at 38-53 must be 01 01 padding, and the one detailed
timing (the preferred mode) must sit in the FIRST descriptor slot at byte 54.  An earlier
file put the timing at byte 38, so the kernel read its bytes as bogus standard timings
(1224x765, 504x403, ...).  No range-limits descriptor either: it makes the kernel add
GTF modes.

    python3 make-edid.py fm1-800x480.bin
"""
import sys

PIXEL_CLOCK_10KHZ = 2938            # 29.38 MHz
H_ACTIVE, H_BLANK, H_FP, H_SYNC = 800, 160, 48, 32
V_ACTIVE, V_BLANK, V_FP, V_SYNC = 480, 30, 3, 6
W_MM, H_MM = 108, 65


def dtd() -> bytes:
    b = bytearray(18)
    b[0:2] = PIXEL_CLOCK_10KHZ.to_bytes(2, "little")
    b[2] = H_ACTIVE & 0xFF
    b[3] = H_BLANK & 0xFF
    b[4] = ((H_ACTIVE >> 8) << 4) | (H_BLANK >> 8)
    b[5] = V_ACTIVE & 0xFF
    b[6] = V_BLANK & 0xFF
    b[7] = ((V_ACTIVE >> 8) << 4) | (V_BLANK >> 8)
    b[8] = H_FP & 0xFF
    b[9] = H_SYNC & 0xFF
    b[10] = ((V_FP & 0xF) << 4) | (V_SYNC & 0xF)
    b[11] = 0                        # border left/right (bits 7:6 = border top/bottom)
    b[12] = W_MM & 0xFF
    b[13] = H_MM & 0xFF
    b[14] = ((W_MM >> 8) << 4) | (H_MM >> 8)
    b[15] = 0                        # h-start, v-start high nibbles
    b[16] = 0                        # h-start / v-start low bytes
    b[17] = 0x18                     # flags: digital separate sync, +/+ polarity
    # NOTE: byte 17 of a DETAILED TIMING is the flag byte (0x18), NOT a terminator.
    # The "byte 17 == 0" rule applies only to the three *monitor descriptor* tags
    # (text / range / name), whose first two bytes -- the pixel clock -- are 0x0000.
    # The display's own timing descriptor legitimately carries flags in byte 17.
    return bytes(b)


def text_desc(tag: int, text: str) -> bytes:
    # Byte 17 of EVERY 18-byte descriptor must be 0x00.  It is easy to forget because the
    # text is padded to 13 bytes into positions 5..17, which silently overwrites it and
    # makes the descriptor malformed.  Pad to 12 and append the terminator explicitly.
    return bytes([0, 0, 0, tag, 0]) + (text + "\n").ljust(12).encode()[:12] + b"\x00"


def empty_desc() -> bytes:
    """An unused descriptor slot: 00 00 00 10 00 followed by thirteen zero bytes."""
    return bytes([0, 0, 0, 0x10, 0]) + bytes(13)


def build() -> bytes:
    e = bytearray(128)
    e[0:8] = bytes([0, 255, 255, 255, 255, 255, 255, 0])
    e[8:10] = bytes([0x4D, 0x56])    # manufacturer id
    e[10:12] = bytes([0x01, 0x00])
    e[16], e[17] = 1, 30             # week, year-1990
    e[18], e[19] = 1, 3              # EDID 1.3
    e[20] = 0x80                     # digital input
    e[21], e[22] = 11, 6             # size in cm
    e[23] = 0x78                     # gamma 2.2
    e[24] = 0x0A                     # preferred timing is native
    for i in range(8):
        e[38 + 2 * i], e[39 + 2 * i] = 0x01, 0x01
    e[54:72] = dtd()
    e[72:90] = text_desc(0xFC, "FM-1 PANEL")
    e[90:108] = text_desc(0xFF, "800X480-60")
    e[108:126] = empty_desc()
    e[127] = (-sum(e[:127])) & 0xFF  # do NOT ask for extensions: there is no ext block
    return bytes(e)


def problems(data: bytes) -> list[str]:
    """Return every way this EDID breaks the rules the kernel actually enforces.

    An EDID with only a correct checksum still parses into something; these checks catch
    the mistakes that parse cleanly but change the mode list.
    """
    bad: list[str] = []
    if len(data) != 128:
        return [f"length {len(data)}, want 128"]
    if sum(data) % 256:
        bad.append("checksum does not sum to zero")
    for i in range(4):                                  # the four descriptor slots
        s = data[54 + 18 * i:72 + 18 * i]
        if s[0:2] == b"\x00\x00":                       # a monitor descriptor, not a DTD
            if s[17] != 0:
                bad.append(f"descriptor {i} terminator byte 17 is 0x{s[17]:02x}, must be 0x00")
        elif i != 0:
            bad.append(f"descriptor {i} carries a pixel clock but is not the preferred timing")
    for i in range(8):                                  # standard timings
        if data[38 + 2 * i:40 + 2 * i] not in (b"\x01\x01",):
            bad.append(f"standard timing {i} is not 01 01 padding")
            break
    if data[126]:
        bad.append(f"byte 126 claims {data[126]} extension block(s); there are none")
    return bad


if __name__ == "__main__":
    data = build()
    assert len(data) == 128 and sum(data) % 256 == 0
    found = problems(data)
    if found:
        raise SystemExit("refusing to write a malformed EDID:\n  " + "\n  ".join(found))
    open(sys.argv[1] if len(sys.argv) > 1 else "fm1-800x480.bin", "wb").write(data)
