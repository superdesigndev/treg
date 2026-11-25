# List your API on treg

treg is the tool catalog for AI agents. Agents find endpoints by the job they want done ("get a
domain's backlink summary") rather than by vendor name, compare the providers that do that job on
price, measured success rate and speed, and call the one they pick through treg's proxy, which adds
the credential on the server.

## What listing gets you

- **Paid traffic from teams that never signed up with you.** treg serves listed endpoints on its own
  key and charges each call to the calling team's prepaid balance, so agents can call your API
  without anyone opening an account with you first. Whether treg serves an API on its own key is
  decided per provider after it is listed.
- **Discovery.** Every agent connected to treg can find your endpoints when it searches for the job
  your API does, next to your competitors and compared with them.

Teams that already have an account with you can also connect their own key and call you directly.

## Requirements, and why

Every listing is tested live, not transcribed from docs, so all of these must hold:

| Requirement | Why |
|---|---|
| Self-serve API keys: sign up and get a key without a sales call | treg's maintainers verify every listing with a key of their own |
| The key goes in a header, a query parameter or the JSON body | treg adds the key there on every call; a key inside the URL path cannot be added |
| A free or near-free endpoint that answers a valid key and an invalid key differently | treg checks every key a team connects. We send your API a made-up key and watch it get refused: an API that accepts any key cannot be listed |
| A public pricing page at a stable URL | Every price in the catalog cites where it came from; "contact sales" cannot be listed as a number |
| Docs for your endpoints with parameter names, types and example values | Every listed endpoint gets a real test call |

Each listed price is your **entry-level public price**: what an ordinary customer pays at the entry
level (your pay-as-you-go rate, or your cheapest paid plan divided down to one unit), not a free
tier, a volume discount or an enterprise price.

Each of these improves a listing:

- **An OpenAPI spec at a stable URL.** treg can then list your whole surface from it, beyond the
  core endpoints curated by hand.
- **An endpoint that returns your current prices.** The strongest evidence a price can cite.
- **Per-success billing** (failed calls free). Verification and retries stay cheap, and a broken
  call never bills a treg user.
- **Documented rate limits**, per endpoint as well as per account.

## How to apply

Give your coding agent a prompt like this one (the **List as vendor** button on the treg
dashboard's Catalog page copies one):

```
Help me create a PR to treg (https://github.com/superdesigndev/treg) that adds our API to its tool
catalog. Follow the instructions at https://treg.to/vendor-listing and include our contact email in
the PR description.
```

The agent opens one pull request on this repository: the registry entry, the catalog file, a logo,
and a description with the evidence from its own live run against your account. Use a team address
for the contact email; the repository is public. Never put a key in the pull request.

## After the pull request

Maintainers re-run every check with a key they control, correct any price that disagrees with
their meter, and email the contact address about the next steps. Once it merges, agents can find
and call your API, and treg shows each endpoint's measured success rate and latency next to its
price: reliability is your ranking.

## What gets declined

Reasons from past reviews:

- The API accepts any key, so a connection cannot be checked.
- The key goes inside the URL path.
- Sales-gated or enterprise-only access.
- Legal or shutdown risk; deprecated or absorbed products.
- UI-only products with no API.

## Keeping your listing healthy

- **Prices moved?** Open a pull request on the `price` and `provenance` keys, or tell us. A price
  not checked for 90 days is flagged.
- **Endpoints changed?** The hand-curated catalog file is updated by hand, so breaking changes need
  a pull request or a heads-up. Endpoints listed from an OpenAPI spec are regenerated from it.
