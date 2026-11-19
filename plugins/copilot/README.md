# treg for GitHub Copilot

A GitHub Copilot plugin (Agent Plugins 1.0.0) with two parts:

- **The treg MCP server** at `https://treg.to/mcp/`. Copilot signs you in with OAuth the first time
  it connects; the plugin holds no key or token.
- **The treg skill**, which tells Copilot how to use those tools: search the catalog by the job,
  read the parameters and price, then call the tool.

Through it, Copilot can look up SEO and SERP data, keyword volume, backlinks, people and company
details, ad libraries, social profiles and posts, and web pages, or run an image, video or voice
model. It can also call the API accounts and connections your team registered in treg, without
seeing their credentials.

## Install

From treg's own marketplace:

```bash
copilot plugin marketplace add superdesigndev/treg
copilot plugin install treg@treg
```

Then ask Copilot for something that needs external data, and sign in to treg when it asks.

## Cost

treg is a paid service with a prepaid balance. Catalog calls made on treg's keys are billed per call,
and the price is shown before the call; calls on your team's own keys are not billed.

## Limitations

- Each call reaches a third-party provider, so answers depend on that provider's coverage and uptime.
- Calls that generate video or run long jobs can take minutes and return a task to poll.

Disclosure: this plugin is published by the treg team (Superdesign dev, Inc.).

Source: [github.com/superdesigndev/treg](https://github.com/superdesigndev/treg) · Docs:
[treg.to/llms.txt](https://treg.to/llms.txt)
