"""tools-registry — a credential-injecting proxy + skill registry."""

__version__ = "0.0.1"

# Side-effect import: registers Feedjolt on oauth_providers.REGISTRY.
from . import oauth_feedjolt as _oauth_feedjolt  # noqa: F401
