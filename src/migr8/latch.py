"""The run latch (spec Section 7.2).

Once the engine records an unknown operation outcome or a detected
transaction-contract violation, the run is unusable.  Author code that catches
the exception must not be able to continue: every subsequent facade call and the
completion path re-raise the latched error.
"""

from __future__ import annotations

from enum import StrEnum

from .errors import ContractViolationError, Migr8Error, UnknownOutcomeError


class LatchState(StrEnum):
    OPEN = "open"
    UNKNOWN_OUTCOME = "unknown_outcome"
    CONTRACT_VIOLATION = "contract_violation"


class RunLatch:
    """Single-shot, non-clearable."""

    __slots__ = ("_error", "_state")

    def __init__(self) -> None:
        self._state = LatchState.OPEN
        self._error: Migr8Error | None = None

    @property
    def state(self) -> LatchState:
        return self._state

    @property
    def latched(self) -> bool:
        return self._state is not LatchState.OPEN

    @property
    def unknown(self) -> bool:
        return self._state is LatchState.UNKNOWN_OUTCOME

    @property
    def error(self) -> Migr8Error | None:
        return self._error

    def latch_unknown(self, error: UnknownOutcomeError) -> UnknownOutcomeError:
        if self._state is LatchState.OPEN:
            self._state = LatchState.UNKNOWN_OUTCOME
            self._error = error
        return error

    def latch_violation(self, error: ContractViolationError) -> ContractViolationError:
        if self._state is LatchState.OPEN:
            self._state = LatchState.CONTRACT_VIOLATION
            self._error = error
        return error

    def check(self) -> None:
        """Re-raise the latched error, if any.

        The re-raised object is the original error, so a caller cannot tell the
        difference between the first failure and a refused later call -- which
        is the point.
        """
        if self._error is not None:
            raise self._error
