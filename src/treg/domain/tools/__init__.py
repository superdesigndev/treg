"""Team-owned tool rules: credential bindings, local-run profiles, and skill bundles."""


class ToolConfigError(Exception):
    """An invalid tool, binding, or bundle configuration, translated by the calling interface."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class SecretOwnershipError(Exception):
    """A member wired a teammate's secret into a tool they control, translated by the calling interface."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
