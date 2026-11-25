"""A minimal ALSA sequencer client over ``ctypes``: the router's only contact with the OS.

No dependency is added: the Pi already has ``libasound.so.2`` and the package keeps
``dependencies = []``. The client has one port. Each unit is attached to that port with a
subscription in both directions, so :meth:`AlsaSeq.disconnect` really lets go of the unit's
raw MIDI device (the flash and soft-key paths need it free).

Bytes in and out go through ALSA's own MIDI event codec (``snd_midi_event_*``), so running
status and system exclusive are handled by the library, not by us. A SysEx message can arrive
in several events; :class:`SysexAssembler` joins them.

This file is meant to be copied alone to a machine and run: it imports only the standard
library. Its self-test, ``python alsa_seq.py loopback``, talks only to ALSA's own "Midi
Through" client and never to a unit.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import select
import sys

# ALSA constants (alsa/seq.h, seq_event.h).
_PORT_CAPS = 1 | 2 | 32 | 64  # READ | WRITE | SUBS_READ | SUBS_WRITE
_PORT_TYPE = 2 | (1 << 20)  # MIDI_GENERIC | APPLICATION
_EVENT_NONE = 255
_QUEUE_DIRECT = 253
_FLAG_TIME_BITS = 0x13  # time stamp, time mode and priority: all cleared for a direct event
_MIDI_BUF = 65536
_MAX_SYSEX = 65536


class SeqError(Exception):
    """An ALSA call failed. The message names the call and the error ALSA gave."""


class _Event(ctypes.Structure):
    """``snd_seq_event_t``: 28 bytes, packed. Only the fields we set are named."""

    _pack_ = 1
    _layout_ = "ms"  # no bitfields, so the layout is the same either way; silences 3.14
    _fields_ = [
        ("type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("tag", ctypes.c_ubyte),
        ("queue", ctypes.c_ubyte), ("time", ctypes.c_ubyte * 8),
        ("src_client", ctypes.c_ubyte), ("src_port", ctypes.c_ubyte),
        ("dst_client", ctypes.c_ubyte), ("dst_port", ctypes.c_ubyte),
        ("data", ctypes.c_ubyte * 12),
    ]


class _PollFd(ctypes.Structure):
    _fields_ = [("fd", ctypes.c_int), ("events", ctypes.c_short), ("revents", ctypes.c_short)]


def _load_lib():
    name = ctypes.util.find_library("asound") or "libasound.so.2"
    try:
        return ctypes.CDLL(name)
    except OSError as e:
        raise SeqError(f"libasound is not available here ({e})") from e


class SysexAssembler:
    """Joins the pieces of one SysEx message, per source. Feed it each decoded chunk; it
    returns complete messages (``F0 ... F7``) and swallows partial ones.

    A chunk that is not SysEx and arrives mid-message is a real-time byte or a new message:
    real-time bytes (``F8``..``FF``) pass through and leave the SysEx open, anything else
    abandons the open SysEx, as MIDI says. A message longer than the limit is dropped.
    """

    def __init__(self, limit: int = _MAX_SYSEX):
        self.limit = limit
        self._open: dict[object, bytearray] = {}

    def feed(self, source, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        out: list[bytes] = []
        buf = self._open.get(source)
        if buf is None and chunk[0] != 0xF0:
            return [bytes(chunk)]
        if chunk[0] == 0xF0:
            buf = bytearray()
            self._open[source] = buf
        elif chunk[0] >= 0xF8:
            return [bytes(chunk)]
        elif chunk[0] & 0x80:  # a new status byte ends the open SysEx without an F7
            del self._open[source]
            return [bytes(chunk)]
        buf.extend(chunk)
        if len(buf) > self.limit:
            del self._open[source]
            return out
        if buf[-1] == 0xF7:
            del self._open[source]
            out.append(bytes(buf))
        return out


class AlsaSeq:
    """One sequencer client with one port, plus subscriptions to unit ports."""

    def __init__(self, name: str = "fm1-router", lib=None):
        self._lib = lib or _load_lib()
        # The default return type is int, which truncates a 64-bit pointer: that crashed.
        self._lib.snd_strerror.restype = ctypes.c_char_p
        self._lib.snd_seq_client_id.restype = ctypes.c_int
        self._seq = ctypes.c_void_p()
        self._codec_in = ctypes.c_void_p()
        self._codec_out = ctypes.c_void_p()
        self._buf = (ctypes.c_ubyte * _MIDI_BUF)()
        lib = self._lib
        self._check("snd_seq_open", lib.snd_seq_open(ctypes.byref(self._seq), b"default", 3, 1))
        # 3 = SND_SEQ_OPEN_DUPLEX, 1 = SND_SEQ_NONBLOCK
        self._check("snd_seq_set_client_name",
                    lib.snd_seq_set_client_name(self._seq, name.encode()))
        self._port = lib.snd_seq_create_simple_port(
            self._seq, b"router", _PORT_CAPS, _PORT_TYPE)
        self._check("snd_seq_create_simple_port", self._port)
        self.client = lib.snd_seq_client_id(self._seq)
        for attr in ("_codec_in", "_codec_out"):
            codec = ctypes.c_void_p()
            self._check("snd_midi_event_new",
                        lib.snd_midi_event_new(_MIDI_BUF, ctypes.byref(codec)))
            lib.snd_midi_event_no_status(codec, 1)  # always emit the status byte
            setattr(self, attr, codec)
        self._lib.snd_seq_event_input.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_Event))]
        self._lib.snd_midi_event_decode.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_Event)]
        self._lib.snd_midi_event_encode.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_Event)]
        self._lib.snd_seq_event_output_direct.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_Event)]
        n = lib.snd_seq_poll_descriptors_count(self._seq, select.POLLIN)
        self._pfds = (_PollFd * max(n, 1))()
        lib.snd_seq_poll_descriptors(self._seq, self._pfds, n, select.POLLIN)
        self._npfds = n
        self._poll = select.poll()
        for i in range(n):
            self._poll.register(self._pfds[i].fd, select.POLLIN)

    # ------------------------------------------------------------------ plumbing
    def _check(self, call: str, rc: int) -> int:
        if rc < 0:
            text = self._lib.snd_strerror(rc)
            raise SeqError(f"{call} failed: {text.decode() if text else rc}")
        return rc

    def connect(self, client: int, port: int = 0) -> None:
        """Subscribe both ways between our port and ``client:port``."""
        lib = self._lib
        self._check("snd_seq_connect_from",
                    lib.snd_seq_connect_from(self._seq, self._port, client, port))
        try:
            self._check("snd_seq_connect_to",
                        lib.snd_seq_connect_to(self._seq, self._port, client, port))
        except SeqError:
            lib.snd_seq_disconnect_from(self._seq, self._port, client, port)
            raise

    def disconnect(self, client: int, port: int = 0) -> None:
        """Let go of ``client:port``. Missing subscriptions are fine."""
        lib = self._lib
        for fn in (lib.snd_seq_disconnect_from, lib.snd_seq_disconnect_to):
            rc = fn(self._seq, self._port, client, port)
            if rc < 0 and rc not in (-errno.ENOENT, -errno.EINVAL, -errno.ENODEV, -errno.ENXIO):
                self._check("snd_seq_disconnect", rc)

    # ---------------------------------------------------------------------- input
    def read(self, timeout: float = 0.1) -> list[tuple[int, bytes]]:
        """Wait up to ``timeout`` seconds, then return every ``(source client, bytes)`` ready.
        Events that are not MIDI (announcements, subscriptions) are skipped."""
        out: list[tuple[int, bytes]] = []
        if not self._pending() and not self._poll.poll(int(timeout * 1000)):
            return out
        ev = ctypes.POINTER(_Event)()
        while True:
            rc = self._lib.snd_seq_event_input(self._seq, ctypes.byref(ev))
            if rc == -errno.EAGAIN:
                break
            if rc == -errno.ENOSPC:  # input queue overrun: say so rather than hide a gap
                raise SeqError("the sequencer input queue overran; messages were lost")
            if rc < 0:
                self._check("snd_seq_event_input", rc)
            n = self._lib.snd_midi_event_decode(self._codec_in, self._buf, _MIDI_BUF, ev)
            if n > 0:
                out.append((ev.contents.src_client, bytes(self._buf[:n])))
            self._lib.snd_midi_event_reset_decode(self._codec_in)
            if rc == 0:
                break
        return out

    def _pending(self) -> int:
        return self._lib.snd_seq_event_input_pending(self._seq, 1)

    # --------------------------------------------------------------------- output
    def send(self, client: int, data: bytes, port: int = 0) -> None:
        """Send one complete MIDI message to ``client:port`` now."""
        if not data:
            return
        raw = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        ev = _Event()
        self._lib.snd_midi_event_reset_encode(self._codec_out)
        pos = 0
        while pos < len(data):
            n = self._lib.snd_midi_event_encode(
                self._codec_out, ctypes.byref(raw, pos), len(data) - pos, ctypes.byref(ev))
            self._check("snd_midi_event_encode", n)
            pos += n
            if ev.type != _EVENT_NONE:
                ev.flags &= ~_FLAG_TIME_BITS & 0xFF
                ev.queue = _QUEUE_DIRECT
                ev.src_client, ev.src_port = self.client, self._port
                ev.dst_client, ev.dst_port = client, port
                self._check("snd_seq_event_output_direct",
                            self._lib.snd_seq_event_output_direct(self._seq, ctypes.byref(ev)))
                ev = _Event()
                self._lib.snd_midi_event_reset_encode(self._codec_out)

    def close(self) -> None:
        if self._seq:
            for codec in (self._codec_in, self._codec_out):
                if codec:
                    self._lib.snd_midi_event_free(codec)
            self._lib.snd_seq_close(self._seq)
            self._seq = ctypes.c_void_p()
            self._codec_in = self._codec_out = ctypes.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _loopback() -> int:
    """Send messages to ALSA's own Midi Through client (14) and read them back. Touches no unit."""
    messages = [bytes([0x90, 60, 100]), bytes([0xC3, 7]), bytes([0xB0, 123, 0]),
                bytes([0xF0, 0x7D, 0x01, 0x02, 0xF7])]
    asm = SysexAssembler()
    with AlsaSeq("fm1-router-selftest") as seq:
        seq.connect(14, 0)
        for m in messages:
            seq.send(14, m)
        got: list[bytes] = []
        for _ in range(20):
            for src, chunk in seq.read(0.1):
                if src == 14:
                    got.extend(asm.feed(src, chunk))
            if len(got) >= len(messages):
                break
        seq.disconnect(14, 0)
    ok = got == messages
    print("sent:", [m.hex() for m in messages])
    print("got: ", [m.hex() for m in got])
    print("loopback", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    if sys.argv[1:] == ["loopback"]:
        raise SystemExit(_loopback())
    print("usage: python alsa_seq.py loopback   (talks only to ALSA's Midi Through)")
    raise SystemExit(2)
