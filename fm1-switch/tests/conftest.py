"""Shared test setup.

The download-mode identity cache in ``registry`` is module-level, and it is keyed by device
node, port and USB device number. Tests reuse the same fake nodes, so each test starts and
ends with an empty cache. Otherwise one test's stubbed ``m-flasher info`` answer would leak
into the next.
"""

import pytest

from fm1_switch import registry


@pytest.fixture(autouse=True)
def _fresh_identity_cache():
    registry.forget_identities()
    yield
    registry.forget_identities()
