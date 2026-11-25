"""fm1-switch — a touchscreen firmware switcher for the M-VAVE FM-1.

Thin, stdlib-only, daemon-less. It does not touch the flash itself: every write is
delegated to ``m-flasher``, which talks to the unit's download-mode SCSI device and
already enforces the safety invariants (backup, diff-only write, read-back verify).
"""

__version__ = "0.1.0"
