"""Shared test setup.

The download-mode identity cache in ``registry`` is module-level, and it is keyed by device
node, port and USB device number. Tests reuse the same fake nodes, so each test starts and
ends with an empty cache. Otherwise one test's stubbed ``m-flasher info`` answer would leak
into the next.
"""

import pytest

from fm1_switch import clockstate, hub, registry


@pytest.fixture(autouse=True)
def _fresh_identity_cache():
    registry.forget_identities()
    yield
    registry.forget_identities()


@pytest.fixture(autouse=True)
def _online_by_default(monkeypatch):
    """No test may probe the real network: the hub is reachable unless a test says otherwise
    (tests/test_offline.py does, and tests the connectivity check itself)."""
    monkeypatch.setattr(hub, "online", lambda **kw: True)


@pytest.fixture(autouse=True)
def _clock_trusted_by_default(monkeypatch):
    """The audit log marks lines written on an untrusted clock; tests compare log lines exactly, and a dev
    machine has no timedatectl, so the clock is trusted unless a test says otherwise."""
    st = clockstate.Status(synchronized=True, ntp=True, set_by_person=False, now=0.0)
    monkeypatch.setattr(clockstate, "status", lambda *a, **k: st)
