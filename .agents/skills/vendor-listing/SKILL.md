---
name: vendor-listing
description: >
  Writes the pull request that lists an API in the treg catalog (github.com/superdesigndev/treg):
  eligibility check, endpoint selection, the registry entry in oauth_providers.py, the catalog file
  in the v2 spelling, a credit rate in fx.yaml, live verification with the vendor's own key, and the
  pull request description. Use when an API vendor wants their API listed on treg, when someone asks
  how to get an API into the treg catalog, or when writing or reviewing a listing pull request.
metadata:
  internal: true  # repo tooling: hidden from `npx skills add superdesigndev/treg`
---

# Listing an API in the treg catalog

One pull request on superdesigndev/treg adds one API. The pull request is the whole submission:
treg's maintainers then verify it and email the contact address in its description about the
next steps.

Needs: a clone of the repository with `origin` fetched (`git fetch origin`), `uv`, npm for the
full test run, and a key for the API in an environment variable. Never write the key into a file,
a commit or the pull request.

When the API does something this skill does not cover (two APIs or pricing models to choose from,
vendor sources that disagree, a price that has several parts or is missing for some modes, a request
value that must be fixed, an endpoint that answers before its work is done, a setup step beyond the
key), list what you can and describe the rest under "Open questions" in the pull request.
Maintainers decide those; do not guess.

Reference files, one level down:

- [references/example.md](references/example.md): a complete listing for a fictional API. Read it
  before writing anything.
- [references/catalog-v2.md](references/catalog-v2.md): every key of the catalog file.
- [references/key-check.md](references/key-check.md): registry fields for the ways an API answers an
  invalid key.
- [references/maintainers.md](references/maintainers.md): treg maintainers only, for reviewing and
  finishing a listing.

## Workflow

Copy this checklist and track your progress:

```
Listing progress:
- [ ] 1. Check eligibility
- [ ] 2. Map every endpoint, choose which to list
- [ ] 3. Pick a capability for each
- [ ] 4. Registry entry and logo
- [ ] 5. Catalog file (and an fx.yaml rate when priced in credits)
- [ ] 6. Validate until clean
- [ ] 7. Verify live with your own key
- [ ] 8. Rebase and open the pull request
```

The pull request changes only these files:

- `src/treg/oauth_providers.py`: the registry entry, and nothing else in that file
- `src/treg/web/logos/<service>.svg`: the logo
- `src/treg/catalog/<service>.yaml`: the catalog file
- `src/treg/catalog/fx.yaml`: only when prices are in credits
- the copies `scripts/build_plugin.py` regenerates

### 1. Check eligibility

All must hold, or the pull request is declined. If one fails, stop and tell the user which.

- Self-serve keys: sign up and get a key without a sales call.
- The key goes in a header, a query parameter or the JSON body, never in the URL path.
- A free or near-free endpoint answers a valid key and an invalid key differently, by status or by
  a body field. treg checks every connected key there.
- A public pricing page at a stable URL.
- Docs with parameter names, types and example values for the endpoints you list.
- No legal or shutdown risk, not deprecated, not UI-only.
- A contact email for the vendor's team. If you do not have one, ask the user now.

### 2. Map every endpoint, choose which to list

List every documented endpoint of the API. The pull request carries this map, each endpoint
marked catalogued or excluded with the reason (an endpoint whose docs lack parameters or examples is
excluded, not a reason to decline the API). Catalogue the core endpoints an agent would call directly,
usually no more than 15. Maintainers can list the rest from an OpenAPI spec, if you publish one.
Favor:

- endpoints doing a job other providers in the catalog already do (agents compare and route
  between those);
- free count, preview or pre-flight endpoints that size a query before paying for it;
- the cheapest tier of an operation, beside the default one.

Leave out endpoints that manage the caller's own account (settings, saved lists, reports, users) or
change data in it, unless managing that account is what the API is for. Mark any you do list
`scope: own_account`: treg never serves those on its own key. List one row per operation: when the
API offers the same operation as GET and POST, list the form its docs lead with, and the other only
if it can do something the first cannot.

### 3. Pick a capability for each

A capability (`people.email.find`) names the job an endpoint does and puts it beside other
providers doing that job.

1. Search `src/treg/catalog/capabilities.yaml` and the other `src/treg/catalog/*.yaml` files. Reuse
   an existing id when its description covers the job, even if your endpoint is narrower (a
   company's employees fit `people.search`); never create a near-duplicate.
2. Only when none fits, propose one under `proposed_capabilities:` in your own catalog file. If
   another provider has an endpoint doing that job without a capability, name it in the pull request.
3. Name a proposal like the existing ones on the same platform: `<platform>.<object>.<action>`,
   the object singular for one item (`amazon.product.detail`), and the platform's own pattern for
   finding many (Amazon has `amazon.search.products`, so categories are `amazon.search.categories`).
   Prefer the actions the catalog uses most: `detail` for one item by id, `list` for a fixed list,
   `search` for a query.

### 4. Registry entry and logo

Add an `OAuthProvider(auth_kind="key", ...)` to `src/treg/oauth_providers.py`, shaped like the
example's, and add it to the provider tuple that builds `REGISTRY`, beside providers of the same
`category`. `category` is one of `CATEGORY_ORDER` in that file; never add one. If none fits, use the
closest and say so in the pull request.

- `service` is the brand name in lowercase without spaces (`pinpointdata`). It is also the catalog
  file name, its `provider`, and the `fx.yaml` key.
- Put the key in a header (`token_header`, `token_format`) when the API allows it, so it never
  appears in a URL.
- `probe_path` is the free endpoint from step 1.
- If the API answers 200 to an invalid key, add the field that tells them apart, from
  [references/key-check.md](references/key-check.md).
- A header every call needs (an API version) goes in `required_headers=(("name", "value"),)`.
- A setup step beyond the key (a profile to complete, a plan to enable) goes in `setup_steps`.

The logo is your own icon as an SVG (an embedded PNG is fine, as `hunter.svg` does), or a neutral
lettermark (a letter on a plain shape).

### 5. Catalog file

Write `src/treg/catalog/<service>.yaml` in the v2 spelling: each endpoint states a `price`, never a
`cost:` block. Start from the example and look up keys in
[references/catalog-v2.md](references/catalog-v2.md). Live files such as `hunter.yaml` also use keys
few listings need (`cache`, plugins); write only keys that reference lists.

**Price: the entry-level public price.** What an ordinary customer pays at the entry level:

- the pay-as-you-go rate, if there is one;
- otherwise the cheapest paid plan, divided down to one unit (Starter, $49 a month for 2,000
  credits, is $0.0245 a credit);
- not a free tier, a volume or annual discount, or an enterprise or negotiated price.

Credit prices also need a rate in `src/treg/catalog/fx.yaml` at the same entry level, or validation
fails.

**Required beyond the obvious:**

- A `miss` rule on every endpoint that finds or enriches something (a person, an email, a company):
  how its answer looks when there is nothing to return. A verdict or a count is an answer, not a
  miss: an email check that answers "invalid" is a hit.
- A `test_request` that is the cheapest real call: one item, the smallest limit, a well-known
  public target (a large company, a company founder), never a private person. An email input takes
  an address of your own company (`support@yourapi.example`), never a third party's personal
  address. Never leave parameters empty to provoke an error: an endpoint with no required
  parameters returns its full default result and bills for it.

**Never write** `verified:`, `example_response`, or files under `src/treg/catalog/examples/`.
Maintainers add those from their own run.

### 6. Validate until clean

Run, fix, and run again until both pass:

```bash
uv run --frozen python scripts/catalog_added.py                         # writes `added:` dates
uv run --frozen python scripts/catalog_validate.py --base origin/main   # must exit 0
```

Read every warning printed for your endpoints. Fix it, or be able to say why it stays. Messages
that name `cost.*` refer to the view treg derives from your `price`, `reserve` and `settle`.

### 7. Verify live with your own key

1. Call `probe_path` with your key and with a made-up key. Record both statuses and bodies, with
   the date.
2. Run every `test_request`, fix what fails, and run again until all pass:

   ```bash
   TREG_CATALOG_CRED="$YOUR_KEY_VAR" uv run --frozen python scripts/catalog_verify.py <service>
   ```

   Do not pass `--write`.
3. Check each endpoint's price against the meter: a charge field in the answer, a rate-card
   endpoint, or the account balance before and after. The meter wins over the docs: correct the
   file to the observed charge.
   - Observe a charge on a target your key has not queried before; repeats can be cached and free.
   - A deliberate miss (a target chosen so a pay-on-success endpoint charges nothing) proves only
     that a miss is free. Observe one hit on a real target too.
4. Call each endpoint that has a `miss` rule with a target that cannot exist (a search takes a
   nonsense keyword). Record the status, body, latency and charge.
5. Run the remaining checks:

   ```bash
   uv run --frozen python scripts/build_plugin.py        # commit every file it changes
   uv run --with pytest-xdist pytest -n auto -q          # must pass
   ```

### 8. Rebase and open the pull request

Rebase on the latest `main` first; catalog files move fast. Use this description, filling every
section:

```markdown
Adds <Display name> (<what it does>) as a key provider: <N> endpoints.

Contact: <a team address, e.g. partnerships@yourapi.example; the repository is public>

## Verification ledger (<date>)

| endpoint | http | test target | charge observed | catalog price | matches? | meter |
|---|---|---|---|---|---|---|
| <id> | 200 | <target> | <charge> | <price> | ✅ | <charge field, rate-card endpoint, or balance before → after> |

Valid key on <probe_path> (<date>): <status> <body>
Invalid key on <probe_path> (<date>): <status> <body>
Miss: <id> on <target>: <status> <body>, <latency>, <charge>   (one line per endpoint with a miss rule)

## Endpoint map

Catalogued: ...
Excluded: <endpoint> (<reason>), ...

## Pricing

<pricing page URL>, <the plan or rate the prices come from>, <billing model: per call, per result,
per success, credits>.
OpenAPI: <URL, or none>

## Open questions

<what the skill did not cover, and endpoints that save something in the caller's account; or none>

catalog_validate.py: <result>. pytest: <result>.
```

In `matches?`: ✅ observed equals the file; ⚠️ only part was observed (say what was not); ❌ they
differed and the file now holds the observed value. A ledger that disagrees with its own file gets
the pull request sent back.

After it is up: answer review comments and keep the branch rebased. Maintainers re-run every check,
stamp the endpoints that pass, and email the contact address.
