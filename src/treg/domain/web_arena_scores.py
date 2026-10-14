"""Quality comparison values for live Web Arena runs."""
from __future__ import annotations

def winner_values(task: str, attempts: list[dict]) -> dict[str, float]:
    values = {}
    for a in attempts:
        if a.get("state") != "hit":
            continue
        q = a.get("quality") or {}
        if task in {"search", "news", "papers", "youtube"} and q.get("state") == "checked" and q.get("estimated_match") is not None:
            if q.get("recent_data_needed"):
                if q.get("freshness_percent") is None:
                    continue
                values[a["provider"]] = 0.75 * q["estimated_match"] + 0.25 * q["freshness_percent"]
            else:
                values[a["provider"]] = q["estimated_match"]
        elif task == "fetch" and q.get("state") == "checked" and q.get("relative_coverage") is not None and q.get("token_efficiency") is not None:
            values[a["provider"]] = (q["relative_coverage"] + q["token_efficiency"]) / 2
        elif task == "sitemap" and q.get("coverage_percent") is not None:
            values[a["provider"]] = q["coverage_percent"]
    return values if len(values) >= 2 else {}
