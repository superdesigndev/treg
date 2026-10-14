"""Framework-neutral upstream transport leaves.

The package stays light because ``treg.localrun`` imports the injector JSON helper.
"""

import sys

from . import ssrf as _ssrf

# Keep relay's moved function body byte-identical while the SSRF owner has a truthful name.
sys.modules.setdefault(f"{__name__}.health", _ssrf)
