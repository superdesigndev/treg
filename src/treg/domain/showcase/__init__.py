"""The showcase leaderboard's rules: what an application must contain, what an app's weekly
activity rows reveal, and to whom.

Pure functions over plain values - no database, no clock. `application.showcase` loads the rows and
calls these. Everything public passes through `public_entry`, which only ever returns bucketed values
for the metrics the maker consented to: never a team id, an exact call count or money.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

# How many weekly rows make the "monthly" numbers (tier, active days, providers, categories).
WINDOW_WEEKS = 4
# How many weekly rows are kept: enough for a streak to grow past the window. A team is counted
# back WINDOW_WEEKS when it first lists an app; older weeks fill in only as time passes.
KEEP_WEEKS = 13

# Call tiers over the window, highest first. Below the last floor an app shows no tier at all.
CALL_TIERS: tuple[tuple[int, str], ...] = (
    (100_000, "100k+"),
    (25_000, "25k+"),
    (10_000, "10k+"),
    (1_000, "1k+"),
    (100, "100+"),
)

STREAK_BADGE_DAYS = 7        # a streak earns a badge from a week up
PROVIDERS_BADGE_MIN = 10     # "runs on N providers" from ten up
RISING_MIN_WEEK_CALLS = 500  # growth on a tiny base is noise, not momentum
RISING_MIN_GROWTH_PCT = 50

BOARDS = ("most_loved", "most_active", "rising", "longest_streak")


@dataclass(frozen=True)
class Week:
    week_start: date            # a Monday, UTC
    calls: int
    active_days: int            # bit d = a call on day d (0 = Monday)
    providers: frozenset[str]
    categories: dict[str, int]


@dataclass(frozen=True)
class Consent:
    calls: bool = False
    streak: bool = False
    providers: bool = False
    categories: bool = False


@dataclass
class Activity:
    """What the rows say, exactly. Internal: `public_entry` decides what leaves the server."""

    calls: int = 0
    active_days: int = 0
    providers: int = 0
    categories: list[str] = field(default_factory=list)
    streak_days: int = 0
    growth_pct: int | None = None
    rising: bool = False


def week_start(day: date) -> date:
    """The Monday of `day`'s ISO week."""
    return day - timedelta(days=day.weekday())


def last_complete_week(today: date) -> date:
    """The Monday of the last week that has fully ended before `today`."""
    return week_start(today) - timedelta(days=7)


def window_start(today: date) -> date:
    """The Monday of the oldest week the monthly numbers read, as of `today`."""
    return last_complete_week(today) - timedelta(days=7 * (WINDOW_WEEKS - 1))


def keep_from(today: date) -> date:
    """The Monday of the oldest week row kept, as of `today`."""
    return last_complete_week(today) - timedelta(days=7 * (KEEP_WEEKS - 1))


def fold_calls(calls, category_of) -> dict[date, dict]:
    """New calls, as `(created_at, provider, endpoint_id)`, added up per UTC week into the parts a
    week row stores. Every part adds: counts sum, active-day bits OR, provider sets union."""
    weeks: dict[date, dict] = {}
    for at, provider, endpoint_id in calls:
        day = at.date() if isinstance(at, datetime) else at
        w = weeks.setdefault(week_start(day), {"calls": 0, "active_days": 0, "providers": set(), "categories": {}})
        w["calls"] += 1
        w["active_days"] |= 1 << day.weekday()
        if provider:
            w["providers"].add(provider)
        if endpoint_id:
            k = category_of(endpoint_id)
            w["categories"][k] = w["categories"].get(k, 0) + 1
    return weeks


def add_to_week(row: dict, delta: dict) -> dict:
    """A stored week row plus a `fold_calls` delta for the same week."""
    mix = dict(row.get("categories") or {})
    for k, n in delta["categories"].items():
        mix[k] = mix.get(k, 0) + n
    return {"calls": (row.get("calls") or 0) + delta["calls"],
            "active_days": (row.get("active_days") or 0) | delta["active_days"],
            "providers": sorted(set(row.get("providers") or []) | delta["providers"]),
            "categories": mix}


def merge_weeks(weeks) -> list[Week]:
    """Weeks of several sources (an app's API keys) as one: same-week rows add up."""
    by_start: dict[date, Week] = {}
    for w in weeks:
        prev = by_start.get(w.week_start)
        if prev is None:
            by_start[w.week_start] = w
            continue
        mix = dict(prev.categories)
        for k, n in w.categories.items():
            mix[k] = mix.get(k, 0) + n
        by_start[w.week_start] = Week(week_start=w.week_start, calls=prev.calls + w.calls,
                                      active_days=prev.active_days | w.active_days,
                                      providers=prev.providers | w.providers, categories=mix)
    return [by_start[d] for d in sorted(by_start)]


def call_tier(calls: int) -> str | None:
    for floor, label in CALL_TIERS:
        if calls >= floor:
            return label
    return None


def _streak(weeks_by_start: dict[date, Week], ref: date) -> int:
    """Consecutive active days ending on the last active day of week `ref`, walking back across
    stored weeks. A missing week, or a week `ref` with no activity at all, ends the streak."""
    week = weeks_by_start.get(ref)
    if week is None or week.active_days == 0:
        return 0
    last = max(d for d in range(7) if week.active_days >> d & 1)
    streak, cursor_week, day = 0, ref, last
    while True:
        w = weeks_by_start.get(cursor_week)
        if w is None or not (w.active_days >> day & 1):
            return streak
        streak += 1
        if day == 0:
            cursor_week, day = cursor_week - timedelta(days=7), 6
        else:
            day -= 1


def activity(weeks: list[Week], ref: date) -> Activity:
    """Fold the stored rows into the numbers the leaderboard ranks on, as of the week `ref`."""
    by_start = {w.week_start: w for w in weeks}
    window = [by_start[d] for d in (ref - timedelta(days=7 * i) for i in range(WINDOW_WEEKS)) if d in by_start]
    act = Activity()
    act.calls = sum(w.calls for w in window)
    act.active_days = sum(bin(w.active_days).count("1") for w in window)
    act.providers = len(frozenset().union(*(w.providers for w in window))) if window else 0
    mix: dict[str, int] = {}
    for w in window:
        for k, v in w.categories.items():
            mix[k] = mix.get(k, 0) + v
    act.categories = [k for k, _ in sorted(mix.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    act.streak_days = _streak(by_start, ref)
    this, prev = by_start.get(ref), by_start.get(ref - timedelta(days=7))
    if this is not None and prev is not None and prev.calls > 0:
        act.growth_pct = round(100 * (this.calls - prev.calls) / prev.calls)
    act.rising = bool(
        this is not None and this.calls >= RISING_MIN_WEEK_CALLS
        and act.growth_pct is not None and act.growth_pct >= RISING_MIN_GROWTH_PCT
    )
    return act


def badges(act: Activity, consent: Consent) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if consent.streak and act.streak_days >= STREAK_BADGE_DAYS:
        out.append({"key": "streak", "label": f"{act.streak_days}-day streak"})
    if consent.calls and act.rising:
        out.append({"key": "rising", "label": "Rising"})
    if consent.providers and act.providers >= PROVIDERS_BADGE_MIN:
        out.append({"key": "providers", "label": f"{act.providers} providers"})
    if consent.calls and act.calls >= CALL_TIERS[0][0]:
        out.append({"key": "calls", "label": f"{CALL_TIERS[0][1]} calls"})
    return out


def _rounded_growth(pct: int) -> int:
    """Growth to the nearest ten percent: enough to compare, too coarse to reverse into volume."""
    return int(round(pct / 10.0) * 10)


def public_entry(act: Activity, consent: Consent, upvotes: int) -> dict:
    """The only shape an app's activity leaves the server in."""
    entry: dict = {"upvotes": upvotes, "badges": badges(act, consent)}
    if consent.calls:
        entry["calls_tier"] = call_tier(act.calls)
        entry["rising"] = act.rising
        if act.rising and act.growth_pct is not None:
            entry["growth_pct"] = _rounded_growth(act.growth_pct)
    if consent.streak:
        entry["streak_days"] = act.streak_days
    if consent.providers:
        entry["providers"] = act.providers
    if consent.categories:
        entry["categories"] = act.categories
    return entry


def boards(apps: dict[int, tuple[Activity, Consent, int]]) -> dict[str, list[int]]:
    """Each board is an ordered list of app ids. An app joins a board only if its maker consented to
    the metric the board ranks on; ties fall back to the id (older first) so the order is stable."""
    loved = sorted(apps, key=lambda s: (-apps[s][2], s))
    active = sorted((s for s in apps if apps[s][1].calls and call_tier(apps[s][0].calls)),
                    key=lambda s: (-apps[s][0].calls, s))
    rising = sorted((s for s in apps if apps[s][1].calls and apps[s][0].rising),
                    key=lambda s: (-(apps[s][0].growth_pct or 0), s))
    streak = sorted((s for s in apps if apps[s][1].streak and apps[s][0].streak_days > 0),
                    key=lambda s: (-apps[s][0].streak_days, s))
    return {"most_loved": loved, "most_active": active, "rising": rising, "longest_streak": streak}


# What a team submits. Checked here so the /showcase form, the dashboard and the staff API refuse
# the same things. Text is stored as given and escaped wherever it is shown.

# A team with several apps links each to its own API keys; an app without keys shows the team's calls.
MAX_APPS_PER_TEAM = 5
MAX_SHOTS = 4
CATEGORIES: dict[str, str] = {
    "social-listening": "Social listening", "ads": "Ads", "sales": "Sales and leads",
    "seo": "SEO", "content": "Content and media", "business-ops": "Business ops",
    "company-data": "Company data", "developer-tools": "Developer tools", "other": "Other",
}
TEXT_LIMITS = {"name": (1, 60), "tagline": (1, 80), "description": (0, 500), "maker_name": (0, 60)}
REVISION_FIELDS = ("name", "url", "tagline", "description", "category", "maker_name", "x_handle",
                   "launch_post_url", "icon_key", "shot_keys")
# What the maker lets the leaderboard show. Not reviewed: they change no public text, link or
# picture, so a team turns any of them on or off at once (`split_submission`).
SHOW_FLAGS = ("show_calls", "show_streak", "show_providers", "show_categories")
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


class ApplicationInvalid(Exception):
    def __init__(self, field: str, detail: str):
        self.field, self.detail = field, detail
        super().__init__(f"{field}: {detail}")


def _https_url(value: str, field: str, *, required: bool) -> str:
    value = (value or "").strip()
    if not value:
        if required:
            raise ApplicationInvalid(field, "a link is required")
        return ""
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname \
            or parts.username or parts.password or len(value) > 500:
        raise ApplicationInvalid(field, "use a full https:// link")
    return value


def split_submission(raw: dict) -> tuple[dict, dict[str, bool]]:
    """A submitted form as (the fields that are reviewed, the show flags that are not)."""
    rest = {k: v for k, v in raw.items() if k not in SHOW_FLAGS}
    return rest, {f: bool(raw.get(f, False)) for f in SHOW_FLAGS}


def flag_changes(before: dict, after: dict) -> dict:
    """{flag: [before, after]} for every show flag that differs."""
    return {f: [bool(before.get(f)), after[f]] for f in SHOW_FLAGS if bool(before.get(f)) != after[f]}


def clean_revision(raw: dict) -> dict:
    """A complete, checked revision from submitted fields. Unknown fields are refused; image keys
    are checked for shape here and for kind and existence where they are published."""
    unknown = set(raw) - set(REVISION_FIELDS)
    if unknown:
        raise ApplicationInvalid(sorted(unknown)[0], "unknown field")
    out: dict = {}
    for name, (low, high) in TEXT_LIMITS.items():
        value = " ".join(str(raw.get(name) or "").split()) if name != "description" \
            else str(raw.get(name) or "").strip()
        if not low <= len(value) <= high:
            raise ApplicationInvalid(name, f"use {low}-{high} characters" if low else f"use at most {high} characters")
        out[name] = value
    out["url"] = _https_url(raw.get("url", ""), "url", required=True)
    out["launch_post_url"] = _https_url(raw.get("launch_post_url", ""), "launch_post_url", required=False)
    category = raw.get("category") or ""
    if category not in CATEGORIES:
        raise ApplicationInvalid("category", "pick a category")
    out["category"] = category
    handle = str(raw.get("x_handle") or "").strip().lstrip("@")
    if handle and not _HANDLE.fullmatch(handle):
        raise ApplicationInvalid("x_handle", "use an X handle like @treg_ai")
    out["x_handle"] = handle
    icon = raw.get("icon_key")
    out["icon_key"] = str(icon) if icon else None
    shots = raw.get("shot_keys") or []
    if not isinstance(shots, list) or len(shots) > MAX_SHOTS or not all(isinstance(k, str) for k in shots):
        raise ApplicationInvalid("shot_keys", f"use at most {MAX_SHOTS} screenshots")
    out["shot_keys"] = list(dict.fromkeys(shots))
    return out


def changes(before: dict, after: dict) -> dict:
    """{field: [before, after]} for every revision field that differs."""
    return {f: [before.get(f), after.get(f)] for f in REVISION_FIELDS if before.get(f) != after.get(f)}
