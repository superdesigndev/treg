"""Require prebuilt dashboard assets in distributable packages, never in editable installs."""
from pathlib import Path
import re

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return
        index = Path(self.root) / "src/treg/web/dashboard/index.html"
        if not index.is_file():
            raise RuntimeError(
                "Dashboard assets are missing. Run bash scripts/build-dashboard.sh before uv build."
            )
        build_data["artifacts"].append("src/treg/web/dashboard/**")
        web = index.parent.parent
        for url in re.findall(r'src="([^"]*/vendor/vue-[^"]+\.js)"', (web / "enrich-arena.html").read_text()):
            if not (web / url.lstrip("/")).is_file():
                raise RuntimeError("Vue runtime is missing. Run bash scripts/build-dashboard.sh.")
        build_data["artifacts"].extend(["src/treg/web/vendor/*.js", "src/treg/web/vendor/LICENSE"])
