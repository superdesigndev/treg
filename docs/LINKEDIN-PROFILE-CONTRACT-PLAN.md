# Richer `linkedin.user.profile` output - plan

Status: PLAN, not implemented (2026-10-22). Nothing here changes code, catalog or routing until
the owner approves it. Companion: treg-internal
`docs/production/routing/linkedin-user-profile-plan.md` (routing order, traffic and spend).
Merge order: this PR first (contract, engine, adapters, provider order); the treg-internal PR
only records evidence and the rollout and lands after this one is deployed.

## Why

`treg.linkedin.user.profile` returns `{output, raw, _treg}`. `output` carries nine scalars
(`full_name`, `first_name`, `last_name`, `headline`, `location`, `followers`, `connections`,
`about`, `linkedin_url`). Jobs, schools and skills live only in `raw`, whose shape depends on the
provider that served. An outside benchmark (datacircle.dev/linkedin-profiles) scored the routed
endpoint 18/100 on data for exactly this reason; Fetchin, read directly, scored 90.

treg's own launch benchmark (production traffic 2026-10-07 to 2026-10-20) shows the same gap as
"richness" (share of profile fields filled) in the raw body versus what reaches the caller in
`output`:

| endpoint | raw richness | output richness |
|---|---|---|
| fetchinio.linkedin.user.profile | 0.748 | 0.362 |
| harvestapi.linkedin.user.profile.main | 0.755 | 0.362 |
| harvestapi.linkedin.user.profile | 0.752 | 0.361 |
| anyapi.linkedin.user.profile.full | 0.737 | 0.362 |
| fiber-ai.linkedin.user.profile | 0.725 | 0.365 |
| brightdata.linkedin.user.profile | 0.483 | 0.273 |
| scrapecreators.linkedin.user.profile | 0.397 | 0.199 |
| anyapi.linkedin.user.profile | 0.383 | 0.204 |

The providers already return the data. The contract and the adapters throw it away.

## Contract diff (`src/treg/catalog/contracts.yaml`)

All new fields are optional. `full_name` stays the only required field and `miss` does not change,
so no answer that is a hit today becomes a miss.

```diff
   linkedin.user.profile:
     output:
       full_name: {type: str, required: true}
       first_name: {type: str}
       last_name: {type: str}
       headline: {type: str}
       location: {type: str}
       followers: {type: int}
       connections: {type: int}
       about: {type: str}
       linkedin_url: {type: str}
+      current_title: {type: str, note: "title of the current position; not the headline"}
+      current_company: {type: str}
+      current_company_linkedin_url: {type: str}
+      photo_url: {type: str}
+      experience: {type: list, note: "newest first; items {title, company, company_linkedin_url, start, end, current, location, description}; start/end are YYYY or YYYY-MM, end null while current"}
+      education: {type: list, note: "items {school, degree, field, start, end}; start/end YYYY or YYYY-MM"}
+      skills: {type: list, note: "skill names as strings"}
+      languages: {type: list, note: "language names as strings"}
+      certifications: {type: list, note: "items {name, issuer, date}"}
     miss: "full_name == null"
+    prefer: [fetchinio, harvestapi, anyapi]
```

Item shapes are fixed by the contract; a key a provider cannot fill is `null`, never absent. One
addition to the brief: `experience[].current` (bool), because several providers mark the current
role explicitly and an open `end` alone cannot tell "current" from "end date unknown".

Dates: one normalized string, `YYYY` or `YYYY-MM`. Providers send `{year, month}` objects
(Fetchin, HarvestAPI), `"Jan 2021"` / `"Present"` text (AnyAPI, Bright Data) or ISO timestamps
(Fiber). The original value stays in `raw`.

`prefer` is the routing change; the reasoning and numbers are in the treg-internal companion.

### Routing diff (same PR, can ship before the contract fields)

The order is decided entirely in this repository: there is no production variable for it.
`rank()` in `routing/plan.py` sorts by tier (a team's own key first), then the contract's
`prefer`, then expected cost per hit (`price x P(billed) / P(hit)`, measured hit rate from
`EndpointDayStat`). Today AnyAPI basic ranks first: its catalog cost is `per_success`, so
`P(billed) = P(hit)` and its expected cost per hit is its list price, $0.0012, below Fetchin's
$0.0015. It is cheap in fact too (reported charges averaged $0.00089 per hit over 14 days, misses
included, although it bills `found: false` answers). Price is not the problem; it is the thinnest
child (no headline, no first/last name, no current company, output richness 0.204), and it serves
55% of routed hits. `prefer` is how a contract says quality beats cost per hit, as
`ai-search.perplexity.answer` already does.

```diff
 # contracts.yaml, linkedin.user.profile
+    prefer: [fetchinio, harvestapi, anyapi]

 # adapters.yaml
   anyapi.linkedin.user.profile:
+    # thin tier: no headline, no first/last name, no current company; stays callable via /call/
+    route: false
   harvestapi.linkedin.user.profile:
+    # same data as .main at $0.0064 vs $0.004; after a .main miss it re-asks the same provider
+    route: false
```

Resulting order for a platform-key caller: Fetchin, HarvestAPI main, AnyAPI full, then by expected
cost per hit: ScrapeCreators, Bright Data, JustOneAPI, Fiber. A team's own key still goes first,
and a caller's `X-Treg-Route-Prefer` / `-Exclude` still overrides. `test_routing.py` gets a test
that pins this order for `{linkedin_url}` with no own keys.

Separately, `anyapi.linkedin.user.profile`'s cost `type` should describe its billing (misses are
charged). That is a billing-path change for direct `/call/` users and is left to its own PR.

### `people.enrich` alignment

`people.enrich` keeps `title` and `company` (they already mean the current role) and gains the
same optional fields with the same item shapes, so an agent that switches between the two
capabilities reads one schema:

```diff
   people.enrich:
     output:
       ...
       linkedin_url: {type: str}
       location: {type: str}
+      headline: {type: str}
+      company_linkedin_url: {type: str}
+      photo_url: {type: str}
+      experience: {type: list, note: "same item shape as linkedin.user.profile"}
+      education: {type: list, note: "same item shape as linkedin.user.profile"}
+      skills: {type: list}
```

Only LinkedIn-backed adapters (HarvestAPI today through `additional_capabilities`, Fetchin if it is
added) fill the new `people.enrich` fields. Email-first enrichers keep what they fill today. This
part is optional and can ship after the `linkedin.user.profile` change.

One adapter `out` map serves both contracts when an adapter lists `additional_capabilities`, and
`route.py` returns the adapter's whole `core` as `output` without projecting it onto the contract.
So a shared map emits `title` and `current_title` (same expression). That leak exists today
(HarvestAPI's `people.enrich` answers already carry `followers` and `about`). Projecting `output`
onto the contract's keys would drop keys callers may read, so it is a separate decision, not part
of this plan.

## Engine work (`src/treg/domain/catalog/routing/`)

The `out` expression language has no way to map over a list (dotted paths, `[i]`, named
transforms). Two generic additions, no provider-specific code:

1. **List projection in `out`.** An `out` value may be a mapping instead of a string:
   `{each: <path to a list>, item: {key: expr, ...}}` evaluates each `item` expression against one
   element and returns a list of objects; `{each: <path>, value: expr}` returns a list of scalars
   (skills as names). `null` or a missing list yields `[]` only when the path exists and is a list,
   otherwise `null`. Lives in `Adapter.from_upstream` (`contracts.py`) and `paths.py`.
2. **Date transform `ym(v)`** in `paths.py`: `{year, month}` → `YYYY-MM` or `YYYY`; `"Jan 2021"` →
   `2021-01`; `"2021"` → `2021`; ISO timestamp → `YYYY-MM`; `"Present"`, `""` and unknown text →
   `null`. Pure, table-tested.
3. **`fmt` for company URLs from ids** already exists (`fmt('https://www.linkedin.com/company/{}', id)`).

**Required router fix before any list field ships.** `route.py` computes
`n_rows = max(len(v) for v in core.values() if isinstance(v, list))` and marks a hit with
`n_rows < min_results` (default 1) as weak, which keeps the waterfall going and bills up to two
more providers. Today `linkedin.user.profile` has no list output, so `n_rows` defaults to 1. With
the new fields, a real profile with no jobs, schools or skills (common on sparse profiles) would
have every list empty, `n_rows == 0`, and be treated as weak. Fix: count rows only from the
contract's required list field (`_list_field(contract)`); a contract without one counts 1 per hit.
This is the same rule `merge` already uses. Add a test that a profile hit with empty optional lists
is a hit, not weak, and that `people.search` weak-hit behavior is unchanged.

## Field map: who can fill what today

Read from the adapter `out` maps in `adapters.yaml` and the example responses under
`src/treg/catalog/examples/`. "today" = mapped in `out` now; "raw" = present in the provider
body, not mapped; "-" = not in the body; "?" = the key exists but the example is empty or
synthetic, so the shape must be confirmed before mapping.

| field | Fetchin | Harvest full / main | AnyAPI full | AnyAPI basic | Bright Data | Fiber | ScrapeCreators |
|---|---|---|---|---|---|---|---|
| full_name ... about (9 core) | today | today | today | 4 of 9 today | today | today | 5 of 9 today |
| current_title | raw `jobTitle` | raw `currentPosition[0].position` | raw `currentPosition[0].position` | - | raw `current_company.title` | raw `current_job.title` | - |
| current_company | raw `companyName` | raw `currentPosition[0].companyName` | raw `currentPosition[0].companyName` | - (no current flag) | raw `current_company.name` | raw `current_job.company_name` | - |
| current_company_linkedin_url | raw `companyLinkedinUrl` | raw `currentPosition[0].companyLinkedinUrl` | raw `currentPosition[0].companyLinkedinUrl` | - | raw, built from `current_company.company_id` | raw, built from `current_job.linkedin_company_id` | - |
| experience[].title | raw `experiences[].jobTitle` | raw `experience[].position` | raw `experience[].position` | - | raw `experience[].title` | raw `experiences[].title` | - (probe: company only) |
| experience[].company / url | raw `company.name` / `company.url` | raw `companyName` / `companyLinkedinUrl` | raw `companyName` / `companyLinkedinUrl` | raw `company` / `companyUrl` | raw `company` / `url` | raw `company_name` / built from id | ? (probe: yes; example lacks it) |
| experience[].start / end | raw `timePeriod.startDate/endDate` | raw `startDate/endDate {year, month}` | raw `"Jan 2021"` / `"Present"` | raw text | raw `"Feb 2014"` / `"Present"` | raw ISO | - |
| experience[].current | raw `current` | derived: `endDate` without year | derived: `endDate == "Present"` | derived | derived | raw `is_current` | - |
| experience[].location | - | raw `location` | raw `location` | - | raw `location` | raw `locality` | - |
| experience[].description | raw `description` | raw `description` | raw `description` | - | raw `description_html` (HTML, leave null) | raw `summary` | - |
| education[].school | raw `educations[].name` | raw `education[].schoolName` | raw `education[].school` | raw `school` | raw `education[].title` | raw `school_name` | ? (probe: one entry) |
| education[].degree / field | ? (not in example) | raw `degree` / `fieldOfStudy` | raw `degree` / `fieldOfStudy` | - | ? | raw `degree` / `field_of_study_name` | ? |
| education[].start / end | raw `timePeriod` | raw `startDate/endDate` | raw text | raw text | raw `start_year/end_year` | raw ISO | ? |
| skills[] | ? (`skills: []` in example) | ? (`skills: []`, synthetic example) | raw `skills[].name` | - | - | ? (null in example) | - |
| languages[] | ? (`[]`) | ? (`[]`) | raw `languages[].name` | - | - | ? (null) | - |
| certifications[] | ? (`[]`) | ? (`[]`) | ? (`[]`) | - | - | ? (null) | - |
| photo_url | raw `profilePictureUrl` | ? (no key in synthetic example) | raw `photo` | raw `avatarUrl` | raw `avatar` | raw `profile_pic` | raw `image` |

Notes:

- Fetchin on treg's shared key is the standard one-credit profile; `fullProfile=true` is not served
  on the shared key (`fetchinio.yaml`). Whether the standard tier fills skills, languages and
  certifications for a typical profile is not shown by the example. The datacircle score of 90
  for Fetchin suggests it does, but that must be confirmed (test plan, step 5).
- `harvestapi.linkedin.user.profile.main` is documented as "limited experience, education and
  skills". The launch benchmark shows the same raw richness as the full tier (0.755 vs 0.752), so
  main stays the first Harvest choice; confirm on the probe set.
- ScrapeCreators' example has no `experience`/`education` keys, but the 2026-10-22 probe returned
  both (company and URL only). Its example must be replaced by a sanitized one that shows them
  before its adapter can map them. It also sends `connections` as a string; the contract says int.
- `justoneapi.x.linkedin-get-user-detail-v1` returns no jobs, schools or skills (5 routed calls in
  14 days). No change.
- `crawl4ai.linkedin.user.profile` has no routing adapter and is out of scope.

## Adapter work list (`src/treg/catalog/adapters.yaml`)

In order of traffic after the routing change.

1. `fetchinio.linkedin.user.profile`: add `current_title: jobTitle`, `current_company: companyName`,
   `current_company_linkedin_url: companyLinkedinUrl`, `photo_url: profilePictureUrl`;
   `experience: {each: experiences, item: {title: jobTitle, company: company.name,
   company_linkedin_url: company.url, start: "ym(timePeriod.startDate)", end: "ym(timePeriod.endDate)",
   current: current, location: null, description: "null_if(description, '')"}}`;
   `education: {each: educations, item: {school: name, degree: null, field: null,
   start: "ym(timePeriod.startDate)", end: "ym(timePeriod.endDate)"}}`; `skills`, `languages`,
   `certifications` after their item shape is confirmed. Consider `additional_capabilities:
   [people.enrich]` (with `title`/`company` aliases), measured first like any new child.
2. `harvestapi.linkedin.user.profile.main` and `harvestapi.linkedin.user.profile`: same fields from
   `element.currentPosition[0]`, `element.experience`, `element.education`; add `title` and
   `company` aliases for its existing `people.enrich` membership. Replace the synthetic example's
   empty `skills`/`languages` with a sanitized non-empty one, or leave those fields unmapped.
3. `anyapi.linkedin.user.profile.full`: all fields including `skills: {each: output.data.skills,
   value: name}` and `languages: {each: output.data.languages, value: name}`.
4. `brightdata.linkedin.user.profile`: current role, experience (without description), education
   school and years, `photo_url: "[0].avatar"`.
5. `fiber-ai.linkedin.user.profile`: current role, experience, education, `photo_url`.
6. `anyapi.linkedin.user.profile` (basic): `photo_url` and `education` only; no current role
   (it has no current flag). If routing excludes it (companion plan), skip.
7. `scrapecreators.linkedin.user.profile`: `photo_url: image` now; experience/education after a
   sanitized example shows them.

Each example that gains mapped list fields must keep synthetic or public-figure data only, as the
current examples do.

## Backward compatibility

- Additive only: no field removed, renamed or retyped; `full_name` remains the only required field;
  `miss` unchanged. Existing callers that read `output` keep working; callers that read `raw` see
  no change at all.
- Response size grows (experience descriptions). `raw` already carries the same data, so the body
  at most roughly doubles in the worst case; no new upstream request or price.
- Call events: new fields default to `analytics: skip`, so the event stream does not grow.
- The weak-hit fix above is a behavior change only for contracts without a required list field,
  and for those it removes a path that today cannot trigger.
- Public contract view, MCP `catalog_get` and the generated docs show the new optional fields
  automatically; nothing to hand-edit.

## Test plan

1. `tests/test_routing.py` (where the `paths` transforms are tested today): table tests for `ym` and for
   `{each, item}` / `{each, value}` including null, missing path, non-list and empty list.
2. `tests/test_routing.py::test_every_shipped_adapter_round_trips_its_fixture`: unchanged, must stay
   green; add one test per changed adapter asserting the example yields non-empty `experience` and
   `education` with the documented item keys, and `current_company` where the provider has one.
3. New `route.py` test: a `linkedin.user.profile` hit whose optional lists are all empty is a
   `hit`, the waterfall stops, one child billed. Existing `min_results` tests for `people.search`
   stay green.
4. `tests/test_additional_capabilities.py`: HarvestAPI's `people.enrich` answer fills `title` and
   `company` from the same fixture.
5. Live check before merge (needs owner approval and a cap): 20 public profiles through Fetchin,
   Harvest main and AnyAPI full via `/call/` (about $0.20 in total at catalog prices), to confirm
   which of skills, languages, certifications, degree and photo each standard tier really fills.
   Aggregate field counts only; no profile contents in the PR.
6. After deploy: the launch benchmark's `richness_treg` for the served providers should move from
   about 0.36 to near the raw figure (about 0.75); rerun datacircle's public scorer if it is
   rerunnable.
7. `uv run pytest` full suite and the catalog validator before pushing.

## Rollback

Each step is a revert of its own commit. The contract and adapter additions are optional fields, so
reverting them removes keys from `output` but leaves `raw` intact; an agent that started reading
the new keys falls back to `raw`. Revert order if needed: adapters, then contract, then the engine
transforms; the weak-hit fix can stay because it is correct on its own. The `prefer` line reverts
independently (companion plan).
