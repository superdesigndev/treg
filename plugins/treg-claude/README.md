# treg for Claude

This plugin connects Claude to treg's catalog of external data APIs: SEO and search results,
backlinks, social profiles, company and business-contact enrichment, ad libraries and web data.
Claude searches the catalog by the task, reads an endpoint's parameters, price and measured
reliability, and then calls it. treg keeps the provider credentials on its server, so you never
paste an API key into Claude.

## What it contains

- One skill, `skills/treg/SKILL.md`: how Claude searches, compares, calls, and asks you before it
  spends money or changes data.
- One remote MCP server, `https://treg.to/mcp/v2`, with six tools: `catalog_search`, `catalog_get`,
  `catalog_call_read`, `catalog_call_write`, `balance` and `catalog_request`.

The plugin runs no local code, installs nothing, and has no hooks or scripts.

## What it sends and where

- Tool calls go to `https://treg.to/mcp/v2`. You sign in to treg with OAuth the first time.
- For a catalog call, treg sends your request to the provider that serves that endpoint, with the
  provider credential added on treg's server, and returns the provider's answer unchanged.
- Paid calls use your team's prepaid treg balance, at the price `catalog_get` shows. The skill tells
  Claude to agree a budget with you before the first paid call in a task.

Privacy policy: https://treg.to/privacy. Terms: https://treg.to/terms.

## Example prompts

- "Find the top backlinks pointing to example.com."
- "What are the Google search results for 'best crm for startups' in the US?"
- "Get the company profile and headcount for stripe.com."

## Help

Documentation: https://treg.to/plugins/claude. Support: https://treg.to/support.
