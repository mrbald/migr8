"""Engine-boundary test hooks for the commit-acknowledgement failure harness
(spec Section 14.3).

A signal emitted by migration code is not sufficient, because further driver
calls can precede the COMMIT.  These hooks fire at the engine's own durable
transition boundaries, so a harness can synchronise exactly there.

Activation is deliberately awkward so that no ordinary deployment can switch it
on by accident:

* in-process tests call :func:`register` directly;
* an out-of-process harness must set ``MIGR8_TEST_HOOKS_ENABLE`` to the exact
  token below *and* name an importable module in ``MIGR8_TEST_HOOKS_MODULE``.

Neither variable is read from any configuration file.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable

ENABLE_ENV = "MIGR8_TEST_HOOKS_ENABLE"
MODULE_ENV = "MIGR8_TEST_HOOKS_MODULE"
ENABLE_TOKEN = "migr8-test-harness-not-a-deployment"

#: Phase names passed to callbacks.
BEFORE_COMMIT = "before_commit"
AFTER_COMMIT = "after_commit"
BEFORE_DDL = "before_ddl"
AFTER_DDL = "after_ddl"

HookCallback = Callable[[str, str], None]

_callbacks: list[HookCallback] = []
_module_loaded = False


def register(callback: HookCallback) -> None:
    """Register an in-process callback taking ``(boundary, phase)``."""
    _callbacks.append(callback)


def clear() -> None:
    _callbacks.clear()


def _ensure_module_loaded() -> None:
    global _module_loaded
    if _module_loaded:
        return
    _module_loaded = True
    if os.environ.get(ENABLE_ENV) != ENABLE_TOKEN:
        return
    module_name = os.environ.get(MODULE_ENV)
    if not module_name:
        return
    importlib.import_module(module_name)


def fire(boundary: str, phase: str) -> None:
    """Invoke every registered callback.  Exceptions propagate deliberately."""
    _ensure_module_loaded()
    for callback in list(_callbacks):
        callback(boundary, phase)
