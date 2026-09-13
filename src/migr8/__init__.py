"""migr8 - an inspectable database migration engine.

``docs/SPEC.md`` is the maintained specification; ``docs/ACCEPTANCE.md`` records
what has been tested and against which versions.
"""

from __future__ import annotations

from .errors import Exit
from .version import TOOL_NAME, TOOL_VERSION

__all__ = ["TOOL_NAME", "TOOL_VERSION", "Exit"]
