"""Compatibility alias for credential-network safety checks used by MCP client metadata."""

import sys as _sys

from ... import health as _implementation

_sys.modules[__name__] = _implementation
