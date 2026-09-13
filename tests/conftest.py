from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _clear_test_hooks():
    from migr8.testing import hooks

    hooks.clear()
    yield
    hooks.clear()


@pytest.fixture(autouse=True)
def _isolate_credential_env():
    """Keep MIGR8_PASSWORD from leaking between tests.

    The live-database fixtures set it; tests that assert a clean configuration
    must not see their leftovers.
    """
    import os

    from migr8.config import PASSWORD_ENV

    previous = os.environ.get(PASSWORD_ENV)
    os.environ.pop(PASSWORD_ENV, None)
    yield
    if previous is None:
        os.environ.pop(PASSWORD_ENV, None)
    else:
        os.environ[PASSWORD_ENV] = previous


@pytest.fixture(autouse=True)
def _unload_staged_units():
    """Ensure a failed test cannot leave a unit package registered."""
    from migr8.loader import PACKAGE_PREFIX

    yield
    for name in [n for n in list(sys.modules) if n.startswith(PACKAGE_PREFIX)]:
        sys.modules.pop(name, None)
