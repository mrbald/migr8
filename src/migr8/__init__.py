"""migr8 - an experimental, inspectable database migration engine.

Status: experimental.  See ``docs/SPEC.md`` for the maintained specification and
``docs/ACCEPTANCE.md`` for what has actually been tested.
"""

from __future__ import annotations

from .errors import Exit
from .version import TOOL_NAME, TOOL_VERSION

__all__ = ["Exit", "TOOL_NAME", "TOOL_VERSION"]
