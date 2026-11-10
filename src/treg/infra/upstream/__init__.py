"""Framework-neutral upstream transport leaves.

The package stays light because ``treg.localrun`` imports the injector JSON helper.
"""

import sys

from . import ssrf as _ssrf

sys.modules.setdefault(f"{__name__}.health", _ssrf)
