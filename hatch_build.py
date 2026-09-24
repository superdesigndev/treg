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
        legacy = Path(self.root) / "src/treg/web/dashboard-legacy/index.html"
        if not legacy.is_file():
            raise RuntimeError("Frozen legacy dashboard is missing; both frontends must ship during rollout.")
        build_data["artifacts"].append("src/treg/web/dashboard/**")
        web = legacy.parent.parent
        for page in [legacy, web / "enrich-arena.html"]:
            for url in re.findall(r'src="([^"]*/vendor/vue-[^"]+\.js)"', page.read_text()):
                relative = url.replace("/app/legacy/", "dashboard-legacy/").lstrip("/")
                if not (web / relative).is_file():
                    raise RuntimeError("Vue runtime is missing. Run bash scripts/build-dashboard.sh.")
        build_data["artifacts"].extend([
            "src/treg/web/vendor/*.js", "src/treg/web/vendor/LICENSE",
            "src/treg/web/dashboard-legacy/assets/*/vendor/**",
        ])
