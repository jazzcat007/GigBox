"""Rig sets: a named group of firmwares (or saved images), one per unit, pushed in one go.

A *set* is an option, never the only way. It may cover one to four units; a unit the set does
not name is left alone, and a push can be limited to any subset of the set's units. Writing a
single unit from the panel works exactly as before.

Each member of a set is one of:

* a **package**: a file in the firmware library, pinned by its sha256. A push refuses a unit
  whose package is missing or has changed since the set was saved.
* a **backup**: a saved whole-flash image of that unit, so a set can also be "the rig as it was"
  and nothing is thrown away.

This module is the pure part: the model, the saved file, the preflight check and the
sequential push. It never talks to a unit. The write functions are passed in (the real ones
are ``web_writes.flash_unit_by_id`` and ``restore_unit_by_id``, which are id-matched and take a
backup of the unit before every write), so the tests run against fakes.

**Safety rules, all enforced here:**

* A push needs a confirm token equal to the set's name.
* Preflight runs first and a blocking problem on any selected unit refuses the whole push,
  before any unit is touched. The caller may deselect that unit and try again.
* Units are written one at a time, in sticker order. The first failure stops the push and
  leaves the remaining units untouched, with their state reported as ``not started``.
* A broken sets file is refused and left alone, never overwritten.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config
from .units import SLOTS

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class SetError(ValueError):
    """A set or push that cannot be used. The message says what and why."""


def store_path() -> Path:
    return config.path("sets")


# ------------------------------------------------------------------------------ model

@dataclass(frozen=True)
class Member:
    """What one unit gets. Exactly one of ``package`` (with ``sha256``) or ``backup``."""

    slot: str
    package: str = ""
    sha256: str = ""
    backup: str = ""

    def __post_init__(self) -> None:
        if self.slot not in SLOTS:
            raise SetError(f"{self.slot!r} is not a unit sticker ({', '.join(SLOTS)})")
        if bool(self.package) == bool(self.backup):
            raise SetError(f"{self.slot}: give either a package or a backup, not both or neither")
        if self.package:
            if "/" in self.package or "\\" in self.package or self.package.startswith("."):
                raise SetError(f"{self.slot}: package must be a plain file name")
            if not _SHA_RE.match(self.sha256):
                raise SetError(f"{self.slot}: the package needs its sha256 (64 hex digits)")
        else:
            parts = Path(self.backup).parts
            if (self.backup.startswith(("/", "\\")) or ".." in parts
                    or not self.backup.endswith(".bin")):
                raise SetError(f"{self.slot}: backup must be a relative .bin path")
            if self.sha256:
                raise SetError(f"{self.slot}: a backup is checked against its own record, "
                               "not a sha256 given here")

    @property
    def kind(self) -> str:
        return "package" if self.package else "backup"

    @property
    def label(self) -> str:
        return self.package or f"backup {Path(self.backup).stem}"

    def to_dict(self) -> dict:
        d = {"slot": self.slot}
        if self.package:
            d.update(package=self.package, sha256=self.sha256)
        else:
            d["backup"] = self.backup
        return d

    @classmethod
    def from_dict(cls, raw) -> "Member":
        if not isinstance(raw, dict):
            raise SetError("a member must be an object")
        unknown = set(raw) - {"slot", "package", "sha256", "backup"}
        if unknown:
            raise SetError(f"unknown member field(s): {', '.join(sorted(unknown))}")
        return cls(slot=str(raw.get("slot", "")), package=str(raw.get("package", "")),
                   sha256=str(raw.get("sha256", "")), backup=str(raw.get("backup", "")))


@dataclass(frozen=True)
class RigSet:
    name: str
    members: tuple[Member, ...]
    note: str = ""

    def __post_init__(self) -> None:
        if not NAME_RE.match(self.name or ""):
            raise SetError("a set name is 1-40 letters, digits, spaces, '.', '_' or '-', "
                           "starting with a letter or digit")
        if not self.members:
            raise SetError("a set needs at least one unit")
        slots = [m.slot for m in self.members]
        if len(set(slots)) != len(slots):
            raise SetError("a unit can appear only once in a set")
        if len(self.note) > 200:
            raise SetError("the note is limited to 200 characters")

    def member(self, slot: str) -> Member | None:
        return next((m for m in self.members if m.slot == slot), None)

    def to_dict(self) -> dict:
        ordered = sorted(self.members, key=lambda m: SLOTS.index(m.slot))
        d = {"name": self.name, "members": [m.to_dict() for m in ordered]}
        if self.note:
            d["note"] = self.note
        return d

    @classmethod
    def from_dict(cls, raw) -> "RigSet":
        if not isinstance(raw, dict):
            raise SetError("a set must be an object")
        members = raw.get("members")
        if not isinstance(members, list):
            raise SetError("a set needs a 'members' list")
        return cls(name=str(raw.get("name", "")),
                   members=tuple(Member.from_dict(m) for m in members),
                   note=str(raw.get("note", "")))


# ------------------------------------------------------------------------------ store

@dataclass
class SetStore:
    """All saved sets in one JSON file, ``{"sets": [...]}``, written atomically."""

    path: Path
    sets: dict[str, RigSet] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> "SetStore":
        path = path or store_path()
        store = cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return store
        except (OSError, ValueError) as exc:
            raise SetError(f"{path} cannot be read as JSON ({exc}); fix or move it") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("sets"), list):
            raise SetError(f"{path} must hold an object with a 'sets' list")
        for item in raw["sets"]:
            rs = RigSet.from_dict(item)
            if rs.name in store.sets:
                raise SetError(f"{path}: the set name {rs.name!r} is used twice")
            store.sets[rs.name] = rs
        return store

    def save_set(self, rigset: RigSet) -> None:
        self.sets[rigset.name] = rigset
        self._write()

    def delete(self, name: str) -> None:
        if name not in self.sets:
            raise SetError(f"no set named {name!r}")
        del self.sets[name]
        self._write()

    def _write(self) -> None:
        body = json.dumps({"sets": [s.to_dict() for _, s in sorted(self.sets.items())]},
                          indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# -------------------------------------------------------------------------- preflight

@dataclass(frozen=True)
class Step:
    """One unit's part of a push, with the reason it cannot go (``blocking``) or a note the
    operator should see first (``warning``)."""

    slot: str
    unit_id: str
    kind: str
    label: str
    package: str = ""
    sha256: str = ""
    backup: str = ""
    blocking: str = ""
    warning: str = ""

    def to_dict(self) -> dict:
        return {"slot": self.slot, "unit_id": self.unit_id, "kind": self.kind,
                "label": self.label, "blocking": self.blocking, "warning": self.warning}


def plan(rigset: RigSet, *, units: dict[str, dict], library_dir: Path,
         sha_of: Callable[[Path], str], backup_exists: Callable[[str, str], bool],
         compat: Callable[[Path, str | None], tuple[str, str]] | None = None,
         only: list[str] | None = None) -> list[Step]:
    """The steps a push would take, in sticker order, with every problem found up front.

    ``units`` maps a slot to ``{"unit_id", "chip"}`` as the registry reports it. ``only``
    limits the plan to those slots (every one must be in the set). Nothing is written.
    """
    if only is not None:
        stray = [s for s in only if rigset.member(s) is None]
        if stray:
            raise SetError(f"{', '.join(stray)} is not in set {rigset.name!r}")
        if not only:
            raise SetError("no units selected")
    wanted = [m for m in sorted(rigset.members, key=lambda m: SLOTS.index(m.slot))
              if only is None or m.slot in only]
    steps: list[Step] = []
    for m in wanted:
        info = units.get(m.slot) or {}
        unit_id = str(info.get("unit_id") or "")
        blocking = warning = ""
        # Local import: guided pulls in the whole device layer, which the pure tests avoid.
        from .guided import looks_like_flash_id
        if not looks_like_flash_id(unit_id):
            blocking = "this unit's flash id is not known yet (register it first)"
        elif m.kind == "package":
            path = library_dir / m.package
            if not path.is_file():
                blocking = f"{m.package} is not in the library"
            elif sha_of(path) != m.sha256:
                blocking = f"{m.package} has changed since the set was saved (sha256 differs)"
            elif compat is not None:
                severity, note = compat(path, info.get("chip"))
                if severity != "ok":
                    warning = note
        elif not backup_exists(unit_id, m.backup):
            blocking = f"the saved image {m.backup} is gone or belongs to another unit"
        steps.append(Step(m.slot, unit_id, m.kind, m.label, m.package, m.sha256, m.backup,
                          blocking, warning))
    return steps


# ------------------------------------------------------------------------------ push

@dataclass
class StepResult:
    slot: str
    label: str
    status: str = "pending"      # pending | writing | done | failed | not started
    message: str = ""

    def to_dict(self) -> dict:
        return {"slot": self.slot, "label": self.label, "status": self.status,
                "message": self.message}


def push(rigset: RigSet, steps: list[Step], confirm: str, *,
         flash: Callable[[str, Path, str, str], dict],
         restore: Callable[[str, str, str], dict],
         library_dir: Path,
         on_step: Callable[[StepResult], None] = lambda r: None) -> list[StepResult]:
    """Write the steps one at a time. Stops at the first failure.

    ``flash(unit_id, path, sha256, confirm)`` and ``restore(unit_id, backup, confirm)`` are the
    id-matched writers; each is given the unit's own flash id as its confirm token, exactly as
    the single-unit panel path does. Returns one result per step; steps after a failure are
    ``not started``. Raises ``SetError`` (before any write) on a wrong confirm or a blocked step.
    """
    if confirm != rigset.name:
        raise SetError("confirmation does not match the set")
    if not steps:
        raise SetError("no units selected")
    blocked = [f"{s.slot}: {s.blocking}" for s in steps if s.blocking]
    if blocked:
        raise SetError("not pushed, nothing was written: " + "; ".join(blocked))

    results = [StepResult(s.slot, s.label) for s in steps]
    failed = False
    for step, res in zip(steps, results):
        if failed:
            res.status, res.message = "not started", "an earlier unit failed"
            on_step(res)
            continue
        res.status = "writing"
        on_step(res)
        try:
            if step.kind == "package":
                event = flash(step.unit_id, library_dir / step.package, step.sha256,
                              step.unit_id)
            else:
                event = restore(step.unit_id, step.backup, step.unit_id)
        except Exception as exc:  # noqa: BLE001 - any write failure must stop the rest
            failed = True
            res.status, res.message = "failed", str(exc)
        else:
            res.status = "done"
            res.message = str((event or {}).get("message", "")) or "written"
        on_step(res)
    return results
