"""The hand-written copy of the comparison pages (`/compare`, `/compare/treg-vs-<slug>`).

One record per product, read from that product's own public pages on the `checked` date, with the
pages it was read from in `sources`. The renderer (`routers/compare.py`) holds no copy of its own,
so a correction made here reaches the page, its markdown twin and its structured data together.

Logos are each company's own published site icon (or the repo's provider logo), used only to name
the product being compared; they are not treg's marks.

Rules the tests enforce:
  - every page names the date its facts were read and at least one source on the product's site;
  - `treg` facts are true of the hosted treg.to, so the pages exist only on the reference hosts;
  - catalog sizes are `{ENDPOINTS}` / `{PROVIDERS}`, filled from the loaded catalog, never typed.

Writing rules: their words where we quote them, "Not published" where their pages say nothing
(which says nothing about the product itself), dates on prices, and no guessed motives.
"""

from __future__ import annotations

from datetime import date

CHECKED = "2026-11-03"  # the day a record was read, unless the record names its own "checked"
CORRECTIONS = "jason@treg.to"

NOT_PUBLISHED = "Not published"

# True once treg.to takes x402 or MPP payments. The x402 row then shows on every page; until then it
# shows only on pages whose record fills it (the products where it is a real difference).
TREG_X402 = False
OPTIONAL_ROWS = frozenset() if TREG_X402 else frozenset({"x402"})

# The rows of the side-by-side table, in order, under their group headings.
ROWS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Billing", (
        ("how_pay", "How you pay"),
        ("free_start", "Free start"),
        ("fees", "Fees"),
        ("x402", "x402 or stablecoin pay per call"),
    )),
    ("What your agent can use", (
        ("catalog", "Size, in their words"),
        ("data", "Paid data with no account at the vendor"),
        ("own_accounts", "Your own accounts"),
        ("actions", "Actions (writes)"),
        ("answer", "What a call returns"),
    )),
    ("How your agent connects", (
        ("mcp", "MCP"),
        ("api_cli", "API, CLI and SDKs"),
        ("own_keys", "Bring your own keys"),
        ("open_source", "Open source and self-host"),
    )),
    ("Control", (
        ("price_before", "Price before the call"),
        ("caps", "Spend limits"),
        ("logs", "Call records"),
        ("team", "Team"),
    )),
)

TREG_CELLS: dict[str, str] = {
    "how_pay": "A prepaid team balance. Each catalog call is billed at the provider's own rate. "
               "No subscription and no seats.",
    "free_start": "$1.00 of credit, once per new verified account. No card.",
    "fees": "0% markup, a commitment in the terms. No top-up fee. Calls on your own keys, tools and "
            "connected accounts are never metered, except X through treg's own X app, which X bills "
            "per use.",
    "x402": "Yes." if TREG_X402 else "Not yet. Top-ups are by card today.",
    "catalog": "{ENDPOINTS} tools across {PROVIDERS} providers, plus the tools your team registers.",
    "data": "Yes: SEO and SERP data, backlinks, people and company enrichment, social profiles and "
            "posts, ad libraries, web search and scraping, image, video and voice generation.",
    "own_accounts": "Google Analytics, Search Console, Google Ads, Business Profile, Tag Manager, "
                    "Meta Ads, TikTok Ads, Microsoft Advertising, Pinterest Ads, Snapchat Ads, "
                    "YouTube, LinkedIn, Slack, X, TikTok, Instagram and Facebook Pages.",
    "actions": "Writes in your connected accounts (ad changes, Slack messages, social posts) and "
               "any API or CLI your team registers. No triggers or scheduled workflows: the agent "
               "runs the steps.",
    "answer": "The provider's own answer, unchanged. Routed tools add a small wrapper and say so.",
    "mcp": "A remote MCP server with OAuth or a token header, for Claude, ChatGPT, Claude Code, "
           "Codex, Cursor and others.",
    "api_cli": "An HTTP API (method, path, query and body reach the provider unchanged), a CLI and "
               "an agent skill. No SDKs.",
    "own_keys": "Yes, on every account. Your team's key always wins and is never metered.",
    "open_source": "Code on GitHub under Apache 2.0 with added terms. You can self-host for your "
                   "own team; offering it as a hosted service to others needs permission.",
    "price_before": "Yes. Each tool's rate, with its source and the date it was checked.",
    "caps": "A team daily spend cap, a per-call cost ceiling, per-member daily call caps and "
            "daily budgets per tagged customer (advisory). No per-key hourly or lifetime caps.",
    "logs": "An audit log of calls with their cost, and every charge in the balance ledger.",
    "team": "Teams with owner, admin, member and viewer roles, and tool access per member.",
}

# The four short lines on each hero card.
SPEC_LABELS = (("billing", "Billing"), ("start", "Free start"), ("keys", "Your own keys"),
               ("code", "Code"))
TREG_SPEC = {
    "billing": "Prepaid, per call, 0% fee",
    "start": "$1.00, no card",
    "keys": "Yes, never metered",
    "code": "On GitHub, self-host",
}

# Hub sections, in order. Every competitor names one of these keys as its `group`.
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("per-call", "Paid tools per call",
     "The same job as treg: one connection and one balance for many paid tools."),
    ("apps", "Act in your own apps",
     "These connect an agent to apps you already use. treg's catalog is mostly data you have no account for."),
    ("gtm", "Sales and GTM data",
     "Platforms with their own interface and database. treg gives an agent their kind of data by API."),
    ("models", "Model routers",
     "One API for many language models. treg is the same idea for tools."),
)


def _c(**cells: str) -> dict[str, str]:
    return cells


COMPETITORS: dict[str, dict] = {
    "agentik": {
        "name": "Agentik",
        "site": "https://www.agentik.cc",
        "group": "per-call",
        "mono": "#2E5BFF",
        "logo": "/media/compare/logos/agentik.svg",
        "logo_fills": True,
        "title": "treg vs Agentik: an Agentik alternative with no top-up fee",
        "description": "Looking for an Agentik alternative? treg gives an AI agent paid tools from one "
                       "prepaid balance, with 0% markup and no top-up fee.",
        "sub": "No top-up fee, and your own keys win. treg and Agentik both bill paid tools to an "
               "agent from one balance.",
        "their_words": "The tool gateway for your agents.",
        "tldr": "Agentik and treg do the same job: one connection, one prepaid balance, many paid "
                "tools and no provider signup. treg is like OpenRouter for tools: one API key and one "
                "balance for many paid APIs. treg adds no markup and no top-up fee, uses your own keys "
                "without metering them, puts your team's own tools behind the same token, and its code "
                "is on GitHub. Agentik offers hourly, daily, lifetime and per-tool caps on each key, "
                "and $5 of starting credit. Both speak MCP, so one agent can use both.",
        "spec": {"billing": "Prepaid, per call, 5.5% top-up fee", "start": "$5 for 14 days",
                 "keys": "No", "code": "Hosted only"},
        "fit_them": [
            "You want hard spending caps on each key: hourly, daily, lifetime and per tool.",
            "You want $5 of starting credit for 14 days, and free public-data tools (50 to 1,000 "
            "free runs a day).",
            "You run ad accounts on networks its Get MCP Ads flow connects, such as Amazon Ads or "
            "Reddit.",
            "You want a run history that keeps no tool inputs or outputs.",
        ],
        "fit_treg": [
            "You want the provider's price with 0% markup, a commitment in the terms, and no top-up "
            "fee.",
            "You hold keys for some providers and want treg to use them first and never meter those "
            "calls, beside the shared catalog.",
            "Your agent works in your Google Analytics, Search Console, Google Ads or Meta Ads "
            "accounts, and you want those calls never metered.",
            "You want your team's own APIs, CLIs and skills behind the same token.",
            "You want to read the code, or self-host treg for your own team.",
        ],
        "cells": _c(
            how_pay="A prepaid balance. Each call at the provider's price. No plan.",
            free_start="$5 of credit for eligible new Google and GitHub accounts, for 14 days, for "
                       "low-cost calls.",
            fees="No markup on tool prices. A 5.5% platform fee on each credit purchase (minimum "
                 "$0.80); none on a first top-up of $10 or less.",
            x402="No, by design. Card only.",
            catalog="2,436 tools from 52 providers (developers page).",
            data="Yes: social data, SEO, web search and reading, ad libraries, people and "
                 "companies, image, video and audio generation, and free public data.",
            own_accounts="Ad and analytics accounts through Get MCP Ads: Meta, Google, TikTok, "
                         "Pinterest, Snapchat, LinkedIn, Reddit, X, Amazon Ads, GA4 and Search "
                         "Console.",
            actions="Ad account changes, previewed first and applied after a person approves. No "
                    "mail, chat or CRM actions.",
            answer=NOT_PUBLISHED,
            mcp="A remote MCP server with OAuth or an API key, plus a public read-only discovery "
                "endpoint.",
            api_cli="A REST API with OpenAPI. No general CLI.",
            own_keys="No. Agentik holds the provider accounts.",
            open_source="No. Hosted service.",
            price_before="Yes: each tool's price and the most a call can reserve.",
            caps="Hourly, daily, lifetime and per-tool caps on each key, plus a tool allowlist per "
                 "key.",
            logs="Price, latency and status of every call. Tool inputs and outputs are not kept.",
            team="Invite teammates, with roles.",
        ),
        "better": [
            ("Spending caps on each key.", "Agentik keys carry hourly, daily, lifetime and per-tool "
             "caps and an allowlist of tools."),
            ("More credit to start.", "Eligible new Google and GitHub accounts get $5 for 14 days, and "
             "free public-data tools give 50 to 1,000 free runs a day."),
            ("Amazon Ads and Reddit.", "Its Get MCP Ads flow connects Amazon Ads and Reddit beside "
             "Meta, Google, TikTok and others, and previews each change for a person to approve."),
            ("Run history without payloads.", "Agentik keeps execution and billing metadata, not the "
             "payloads of tool calls."),
        ],
        "different": [
            ("No fee at all.", "0% markup is a commitment in treg's terms, and top-ups carry no fee. "
             "Agentik's tool prices carry no markup either, and its top-ups after a first small one "
             "carry 5.5%."),
            ("Your own keys win.", "Register your key for a provider and treg uses it in place of its "
             "own, and never meters the call. Agentik holds every provider account."),
            ("Your accounts, never metered.", "Calls on your connected Google Analytics, Search "
             "Console, Google Ads and Meta Ads accounts are never metered by treg. Agentik prices "
             "ad-account reads at $0.005 and heavy reports or changes at $0.05."),
            ("A larger catalog, plus your own tools.", "{ENDPOINTS} tools across {PROVIDERS} "
             "providers, and your team's own APIs, CLIs and skills behind the same token. Agentik "
             "lists 2,436 tools from 52 providers."),
            ("The code is on GitHub.", "Read it, or self-host treg for your own team."),
        ],
        "both": "Both speak MCP, so one agent can hold both connections. A team could keep Agentik "
                "for capped keys and its Amazon Ads and Reddit connections, and use treg for its own "
                "keys, its team's tools and the wider catalog.",
        "faq": [
            ("Is there an Agentik alternative for AI agents?",
                 "Yes. treg is an Agentik alternative that does the same job: one connection, one "
                 "prepaid balance and many paid tools for an agent. treg also puts your team's own "
                 "APIs, CLIs and skills behind the same token, and calls on your connected Google "
                 "Analytics, Search Console, Google Ads and Meta Ads accounts are never metered."),
            ("How much does Agentik cost?",
                 "Agentik pricing is pay per call from a prepaid balance, with no plan. Agentik gives "
                 "eligible new Google and GitHub accounts $5 of credit for 14 days, and its free "
                 "public-data tools allow 50 to 1,000 free runs a day."),
            ("Can I use my own API keys with Agentik?",
                 "No. Agentik holds the provider accounts, while treg accepts your team's own keys on "
                 "every account. On treg, your team's key always wins over treg's, and those calls are "
                 "never metered."),
            ("Agentik vs treg: which is cheaper for an AI agent?",
                 "On fees, treg. Agentik and treg both bill tools at the provider's price, but Agentik "
                 "adds a 5.5% platform fee (minimum $0.80) to each top-up after a first top-up of $10 "
                 "or less. treg adds 0% markup, a commitment in its terms, and no top-up fee."),
            ("Can I use Agentik and treg together?",
                 "Yes. Agentik and treg both speak MCP, so one agent can hold both connections. A team "
                 "could keep Agentik for capped keys and its Amazon Ads and Reddit connections, and "
                 "use treg for its own keys, its team's tools and the wider catalog."),
            ("How does treg set spending limits compared with Agentik?",
                 "treg sets limits for the team: a team daily spend cap, a per-call cost ceiling and "
                 "per-member daily call caps. Agentik sets limits on each key, with hourly, daily, "
                 "lifetime and per-tool caps and a tool allowlist."),
            ("Can I self-host treg?",
                 "Yes. treg's code is on GitHub under Apache 2.0 with added terms, and you can "
                 "self-host treg for your own team. Agentik is a hosted service."),
        ],
        "sources": [
            ("Agentik home page", "https://www.agentik.cc"),
            ("Agentik pricing", "https://www.agentik.cc/pricing.md"),
            ("Agentik vs treg, by Agentik", "https://www.agentik.cc/compare/agentik-vs-treg.md"),
            ("Get MCP Ads docs", "https://www.agentik.cc/docs/getmcpads.md"),
            ("Limits", "https://www.agentik.cc/docs/limits.md"),
            ("Developers page", "https://www.agentik.cc/developers.md"),
        ],
    },
    "firecrawl-alexandria": {
        "name": "Firecrawl Alexandria",
        "short": "Alexandria",
        "provider": "firecrawl",
        "site": "https://firecrawl.dev/alexandria",
        "group": "per-call",
        "mono": "#FF5A1F",
        "logo": "/logos/firecrawl.svg",
        "title": "treg vs Firecrawl Alexandria: Firecrawl pricing compared",
        "description": "Firecrawl pricing for Alexandria is credits from a monthly plan. treg bills "
                       "each data call from a prepaid balance at 0% markup.",
        "sub": "SEO, people and ad data per call, with no plan. Alexandria centres on public records "
               "and Firecrawl's own indexes.",
        "their_words": "The knowledge library for superintelligence.",
        "tldr": "Both give an agent data from many providers through one key. treg is like OpenRouter "
                "for tools: one API key and one balance for many paid APIs. treg is a catalog of "
                "commercial data APIs (SEO, enrichment, social, ads, generation), billed per call from "
                "a prepaid balance at 0% markup, with your own keys and connected accounts behind the "
                "same token. Alexandria, part of Firecrawl, mixes the live web, Firecrawl's own "
                "indexes and official providers in one search, with deep public-records coverage, paid "
                "in Firecrawl credits from a plan. A team can use both, and Firecrawl's own tools are "
                "in the treg catalog too.",
        "spec": {"billing": "Firecrawl credits, monthly plans", "start": "1,000 credits a month",
                 "keys": NOT_PUBLISHED, "code": "Core AGPL-3.0"},
        "fit_them": [
            "You need public records: regulators, registries, procurement and government sources.",
            "You want one query that mixes live web results with structured provider tools.",
            "Your coding agent needs Firecrawl's index of papers, READMEs, issues and docs.",
            "You already use Firecrawl, or need SOC 2 Type II and cloud-marketplace billing.",
        ],
        "fit_treg": [
            "You need SEO metrics, backlinks, people and company enrichment, social or ad-library data.",
            "You want to pay per call from a prepaid balance at the provider's rate, with no monthly "
            "plan.",
            "You want your own provider keys used first, with those calls never metered.",
            "Your agent works in your own Google Analytics, Search Console, Meta Ads or LinkedIn "
            "accounts.",
            "You want the provider's own response, with every field it returns.",
        ],
        "cells": _c(
            how_pay="Firecrawl credits from a monthly plan: free, $19, $99, $399 or $749 a month, or "
                    "Enterprise. Pay-as-you-go top-ups on paid plans.",
            free_start="Free plan: 1,000 credits a month, no card. Alexandria needs an account.",
            fees="Tools run at their listed price in credits. A markup is not published. Credits roll "
                 "over only on Scale and Enterprise.",
            catalog="430 providers and 2,462 capabilities, plus 113M+ indexed sources.",
            data="Public records and government (the largest group), company, retail, finance, real "
                 "estate, health, research, places and more, plus the live web.",
            own_accounts=NOT_PUBLISHED,
            actions="Not published. The documented examples are data reads.",
            answer="Structured results through Firecrawl's search and scrape endpoints.",
            mcp="A remote MCP server (OAuth or API key), a local MCP server, and a plugin for ChatGPT "
                "and Codex.",
            api_cli="A REST API, JavaScript and Python SDKs, and a CLI.",
            own_keys=NOT_PUBLISHED,
            open_source="Firecrawl's core is AGPL-3.0 and can be self-hosted. Whether Alexandria works "
                        "on a self-hosted install is not published.",
            price_before="Yes: free discovery shows each tool's inputs, response shape and price.",
            caps="A monthly pay-as-you-go limit. Per-key endpoint limits on Enterprise.",
            logs="Not published for Alexandria.",
            team="Teams and org admins. SSO and SCIM on Enterprise.",
        ),
        "better": [
            ("Public records.", "Hundreds of official regulators, registries and government sources, "
             "plus free Research and Government indexes."),
            ("Web and tools in one query.", "A search can return web pages and provider tools together, "
             "and falls back to search and scraping when no provider fits."),
            ("Its own indexes.", "113M+ indexed papers, READMEs, issues and docs, useful for coding agents."),
            ("Licensed sources.", "Providers are paid under agreement, with terms an admin accepts for "
             "the whole organization."),
            ("A mature platform.", "SOC 2 Type II, SDKs, a CLI, local and remote MCP, and a ChatGPT and "
             "Codex plugin."),
        ],
        "different": [
            ("Pay per call, with no plan.", "treg bills each call from a prepaid balance at the "
             "provider's rate, with 0% markup in its terms and no top-up fee. Alexandria uses Firecrawl "
             "credits from a monthly plan."),
            ("Commercial GTM and SEO data.", "Keyword volume, backlinks, SERPs, work emails and phones, "
             "social profiles and ad libraries, from many vendors."),
            ("Your keys and accounts.", "Your own provider keys always win and are never metered, and "
             "your connected Google Analytics, Search Console, Meta Ads, LinkedIn and Slack accounts "
             "sit behind the same token, also never metered."),
            ("The provider's own answer.", "treg relays the provider's response unchanged, so every "
             "field it returns reaches your agent."),
            ("Spend controls for the team.", "A team daily spend cap, a per-call cost ceiling and "
             "per-member daily call caps, with each tool's rate, source and checked date shown before "
             "the call."),
        ],
        "both": "Firecrawl's scrape, crawl and search tools are in the treg catalog too, billed per call "
                "on treg's Firecrawl account. A team can use Alexandria for public records and research, "
                "and treg for SEO, enrichment, social and ads.",
        "faq": [
            ("Is there a Firecrawl Alexandria alternative for AI agents?",
                 "It depends on the data. treg is a Firecrawl Alexandria alternative for commercial "
                 "data: SEO and SERP data, backlinks, people and company enrichment, social profiles "
                 "and ad libraries. Firecrawl Alexandria has deep public-records coverage, so a team "
                 "can use Firecrawl Alexandria and treg side by side."),
            ("How much does Firecrawl Alexandria cost?",
                 "Firecrawl Alexandria pricing depends on the Firecrawl plan: tools run at their "
                 "listed price in Firecrawl credits. The free plan gives 1,000 credits a month with no "
                 "card, and paid plans cost $19, $99, $399 or $749 a month. Credits roll over only on "
                 "Scale and Enterprise."),
            ("Can I self-host Firecrawl Alexandria?",
                 "It depends on the part. Firecrawl's core is AGPL-3.0 and can be self-hosted, but "
                 "Firecrawl does not publish whether Firecrawl Alexandria runs on a self-hosted "
                 "install. treg's code is on GitHub under Apache 2.0 with added terms, and you can "
                 "self-host treg for your own team."),
            ("Firecrawl Alexandria vs treg: what is the difference?",
                 "The main difference is the answer. Firecrawl Alexandria returns structured results "
                 "through Firecrawl's search and scrape endpoints, mixing the live web, Firecrawl's "
                 "own indexes and official providers in one query. treg relays each provider's own "
                 "answer unchanged, so every field the provider returns reaches the agent."),
            ("Can I use Firecrawl Alexandria and treg together?",
                 "Yes. Firecrawl's scrape, crawl and search tools are in the treg catalog too, billed "
                 "per call on treg's Firecrawl account. A team can use Firecrawl Alexandria for public "
                 "records and research, and treg for SEO, enrichment, social and ads."),
            ("How does treg pricing work compared with Firecrawl Alexandria?",
                 "treg has no plan: a prepaid team balance pays each call at the provider's rate, with "
                 "0% markup in its terms and no top-up fee. treg gives $1.00 of credit once per new "
                 "verified account, with no card. Firecrawl Alexandria uses Firecrawl credits from a "
                 "monthly plan."),
            ("Can I use my own API keys with treg?",
                 "Yes. treg accepts your team's keys on every account: your own key for a provider "
                 "always wins over treg's, and those calls are never metered. Firecrawl does not "
                 "publish whether Firecrawl Alexandria takes your own provider keys."),
        ],
        "sources": [
            ("Alexandria", "https://firecrawl.dev/alexandria"),
            ("Alexandria docs", "https://docs.firecrawl.dev/features/alexandria"),
            ("Firecrawl pricing", "https://www.firecrawl.dev/pricing"),
            ("Alexandria in ChatGPT and Codex", "https://www.firecrawl.dev/blog/alexandria-chatgpt-codex"),
            ("Firecrawl on GitHub", "https://github.com/firecrawl/firecrawl"),
        ],
    },
    "aisa": {
        "name": "AIsa",
        "site": "https://aisa.one",
        "group": "per-call",
        "mono": "#7C3AED",
        "logo": "/media/compare/logos/aisa.svg",
        "title": "treg vs AIsa: an AIsa alternative for paid data APIs",
        "description": "Looking for an AIsa alternative? treg relays each provider's own answer at 0% "
                       "markup, with own keys and team roles on every account.",
        "sub": "Each provider's own answer, with every field it returns. AIsa reshapes answers into "
               "one schema, sourced per value.",
        "their_words": "The resource layer for AI agents.",
        "tldr": "Both sell data to agents per call from a prepaid balance. treg is like OpenRouter for "
                "tools: one API key and one balance for many paid APIs. treg relays each provider's "
                "own answer unchanged at the provider's rate with 0% markup, and adds your own keys, "
                "your connected accounts and team roles on every account. AIsa returns data in one "
                "normalized schema, labels the source of every value, and bills per result. One agent "
                "can hold both connections and use each for its own jobs.",
        "spec": {"billing": "Prepaid, per result", "start": "$1 every month",
                 "keys": "Enterprise only", "code": "CLI and skills"},
        "fit_them": [
            "You want one response shape across platforms, so your agent never parses vendor formats.",
            "You want the source, method and time of every value, with published calibration.",
            "You want empty responses and failed prechecks to cost nothing.",
            "You want $1 of free balance every month.",
            "You want language models on the same key and balance.",
        ],
        "fit_treg": [
            "You want every field the provider returns, in the provider's own format.",
            "You want your own provider keys used first and never metered, on every account.",
            "Several people on your team share one account, with owner, admin, member and viewer roles.",
            "Your agent works in your own Google Analytics, Search Console or ad accounts, and you want "
            "those calls never metered.",
            "You want the provider's own rate with 0% markup, a commitment in the terms.",
        ],
        "cells": _c(
            how_pay="A prepaid balance by card, pay per result from $0.001. Some premium endpoints need "
                    "a subscription.",
            free_start="$1 of free balance every month, no card. Unused free balance expires each month.",
            fees="Prices use a per-plan multiplier. A markup over the provider price is not published. "
                 "Empty responses and failed prechecks are free.",
            x402="Yes: x402 and MPP, with no account.",
            catalog="5,000+ APIs on the home page; 1,927 endpoints on the pricing page. Plus 110+ "
                    "language models.",
            data="Yes: companies and people, SEO, AI answers, commerce, social, finance, crypto and "
                 "prediction markets.",
            own_accounts="Publishing runs under the account owner's authorization. Which accounts can be "
                         "connected is not published.",
            actions="Posting to X today. More platforms and ad campaigns are marked coming soon. Agent mail.",
            answer="One normalized schema, each value labeled by source.",
            mcp="A remote MCP server with OAuth 2.1 or an API key, plus one endpoint per module.",
            api_cli="A REST API with OpenAPI, a CLI, and OpenAI-compatible model endpoints.",
            own_keys="Enterprise only.",
            open_source="The CLI, skills and docs are on GitHub (MIT and Apache-2.0). The service is not "
                        "published as open source.",
            price_before="Yes: free quotes for up to 20 exact calls.",
            caps="A spending limit per key. The max-price header rejects a high estimate but does not "
                 "cap the final charge.",
            logs="A usage console lists every request with the amount charged.",
            team="1 seat below Enterprise; unlimited seats on Enterprise.",
        ),
        "better": [
            ("One schema.", "The same shape across platforms, so Amazon and Temu, or Google and Bing, read "
             "the same way."),
            ("Provenance on every value.", "Each value says where it came from and how, and AIsa publishes "
             "its calibration."),
            ("Pay for results.", "Empty responses and failed prechecks are free, and verified fields are "
             "billed only when verification passes."),
            ("x402 and MPP.", "An agent with no account can pay per call."),
            ("Language models on the same key.", "110+ LLMs on the same balance as its data tools."),
        ],
        "different": [
            ("The provider's own answer.", "treg relays the response unchanged, so every field reaches "
             "your agent, including the ones a vendor adds later."),
            ("Your own keys on every account.", "Your team's key always wins and is never metered, with no "
             "Enterprise contract."),
            ("Your connected accounts.", "Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn "
             "and Slack, never metered."),
            ("Teams on every account.", "Owner, admin, member and viewer roles, and tool access per member."),
            ("0% markup, in the terms.", "Each call is billed at the provider's own rate, with no top-up "
             "fee, and the price shows with its source and checked date before the call."),
        ],
        "both": "If your agent needs one schema for commerce or social data, AIsa does that work for you. "
                "If it needs a vendor's full response, your own keys or your own accounts, treg fits. Both "
                "are remote MCP servers.",
        "faq": [
            ("Is there an AIsa alternative for AI agents?",
                 "Yes. treg is an AIsa alternative that gives agents paid data tools from a prepaid "
                 "balance, plus your connected accounts and the tools your team registers. treg "
                 "connects your Google Analytics, Search Console, Google Ads and Meta Ads accounts, "
                 "and those calls are never metered."),
            ("How much does AIsa cost?",
                 "AIsa pricing starts free: AIsa gives $1 of free balance every month, and unused free "
                 "balance expires each month. After that, AIsa bills per result from $0.001 times a "
                 "plan multiplier, and empty responses and failed prechecks are free."),
            ("Can I use my own API keys with AIsa?",
                 "On Enterprise. AIsa lists your own vendor keys on its Enterprise plan, while treg "
                 "accepts your team's keys on every account. On treg, your team's key always wins and "
                 "those calls are never metered."),
            ("AIsa vs treg: what is the difference?",
                 "The main difference is the response. AIsa returns its own normalized schema with "
                 "each value labeled by source, while treg relays the provider's own answer unchanged. "
                 "With treg, every field a provider returns reaches the agent, including fields the "
                 "vendor adds later."),
            ("Can I use AIsa and treg together?",
                 "Yes. AIsa and treg are both remote MCP servers, so one agent can hold both "
                 "connections. If the agent needs one schema for commerce or social data, AIsa does "
                 "that work; if it needs a vendor's full response, your own keys or your own accounts, "
                 "treg fits."),
            ("How does treg pricing work compared with AIsa?",
                 "treg bills each call at the provider's own rate with 0% markup, a commitment in its "
                 "terms, and no top-up fee. treg gives $1.00 of credit once per new verified account, "
                 "with no card. AIsa prices calls with a per-plan multiplier and does not publish a "
                 "markup over the provider price."),
            ("Can my team share one treg account?",
                 "Yes. treg teams work on every account, with owner, admin, member and viewer roles "
                 "and tool access per member. AIsa's Starter and Developer plans have 1 seat, and AIsa "
                 "Enterprise has unlimited seats."),
        ],
        "sources": [
            ("AIsa home page", "https://aisa.one"),
            ("AIsa pricing", "https://aisa.one/pricing"),
            ("Pricing guide", "https://aisa.one/docs/guides/pricing"),
            ("AIsa llms.txt", "https://aisa.one/llms.txt"),
            ("AIsa on GitHub", "https://github.com/AIsa-team"),
        ],
    },
    "locus": {
        "name": "Locus",
        "site": "https://paywithlocus.com",
        "group": "per-call",
        "mono": "#0F9D58",
        "logo": "/media/compare/logos/locus.svg",
        "title": "treg vs Locus: a Locus alternative with no subscription",
        "description": "Looking for a Locus alternative? treg gives AI agents paid APIs from a prepaid "
                       "balance, with no subscription and 0% markup.",
        "sub": "Your team's own keys, accounts and tools, beside the catalog. Locus is built to resell "
               "paid tools to your customers.",
        "their_words": "Agent tools, billed per user.",
        "tldr": "Both give agents paid APIs from a prepaid balance through a remote MCP server. treg "
                "is like OpenRouter for tools: one API key and one balance for many paid APIs. treg "
                "bills per call with no subscription, 0% markup and no top-up fee, uses your own keys "
                "on every account without metering them, and adds your connected accounts and your "
                "team's own tools. Locus Pro has monthly plans and is built to resell: per-customer "
                "balances, your own markup and an embedded billing widget. One agent can hold both "
                "connections.",
        "spec": {"billing": "Plans from $0 to $1,000 a month", "start": "Free plan, no credit",
                 "keys": "Enterprise only", "code": NOT_PUBLISHED},
        "fit_them": [
            "You resell tools to your own customers, with your markup, per-customer balances and a "
            "billing widget.",
            "You want each agent scoped: allowed tools, a maximum per call, a budget per run and an expiry.",
            "You want signed webhooks for every charge, top-up and low balance.",
            "You want OpenAI, Anthropic and Gemini models on the same balance as your tools.",
        ],
        "fit_treg": [
            "You want no subscription: top up a balance and pay per call at the provider's rate.",
            "You want your own provider keys used first and never metered, on every account.",
            "Your agent works in your own Google Analytics, Search Console, Meta Ads, LinkedIn or Slack "
            "accounts.",
            "You need SEO, enrichment, social and ad-library data.",
            "You want your team's own APIs, CLIs and skills behind the same token.",
        ],
        "cells": _c(
            how_pay="Locus Pro: a prepaid balance with monthly plans: Free $0, Plus $20, Enterprise $200, "
                    "Enterprise Plus $1,000. Pay With Locus: a USDC wallet with no subscription.",
            free_start="Free plan at $0 with 5 calls a minute and no included credit. Minimum card "
                       "top-up $5.",
            fees="Tools at catalog base price. A Locus markup is not published. A card processing fee on "
                 "top-ups, paid by the buyer or the workspace.",
            x402="Yes: x402 and MPP, plus a USDC wallet on Base.",
            catalog="3,954 services from 57 reviewed providers (home page). Other pages give other counts.",
            data="Yes: AI models, search, scraping, browser automation, enrichment, finance, crypto, "
                 "weather, OCR, email and more.",
            own_accounts=NOT_PUBLISHED,
            actions="Custom API actions you import; they start disabled.",
            answer=NOT_PUBLISHED,
            mcp="A remote MCP server with browser OAuth.",
            api_cli="A REST API with OpenAPI and a TypeScript SDK. A CLI is listed.",
            own_keys="Enterprise only, for custom API actions.",
            open_source=NOT_PUBLISHED,
            price_before="Yes: prices shown before you use each tool.",
            caps="A tool allowlist per agent connection, a maximum per call, a shared budget per run, an "
                 "expiry, and a monthly workspace cap.",
            logs="Activity per customer, agent, provider and endpoint, and signed webhooks.",
            team="Team access on Enterprise.",
        ),
        "better": [
            ("Reselling.", "Per-customer balances, a configurable markup, an embedded billing widget and "
             "payouts."),
            ("Agent payments.", "x402, MPP and a USDC wallet on Base let an agent pay with no account."),
            ("Agent scoping.", "A maximum per call and a shared budget per run, set on each agent "
             "connection, checked before the provider is called."),
            ("Webhooks.", "Signed, retried webhooks for charges, top-ups, refunds and low balance."),
            ("Language models.", "OpenAI, Anthropic, Gemini and others on the same balance."),
        ],
        "different": [
            ("No subscription, no fee.", "treg bills each call from a prepaid balance at the provider's "
             "rate, with 0% markup in its terms and no top-up fee. Locus Pro's paid plans run from $20 "
             "to $1,000 a month."),
            ("Your keys on every account.", "Your team's key always wins and is never metered. On Locus, "
             "your own keys need Enterprise."),
            ("Your accounts.", "Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn and "
             "Slack, connected by OAuth and never metered."),
            ("GTM and SEO data.", "Keyword volume, backlinks, work emails and phones, social profiles and "
             "ad libraries."),
            ("The code is on GitHub.", "Read it, or self-host treg for your own team."),
        ],
        "both": "A platform that resells tools to its customers may want Locus's billing layer. A team "
                "whose agent needs GTM data and its own accounts may want treg. Both are remote MCP "
                "servers, so one agent can hold both connections.",
        "faq": [
            ("Is there a Locus alternative for AI agents?",
                 "Yes. treg is a Locus alternative that gives agents paid tools from one prepaid "
                 "balance, built around your team's own keys, accounts and tools. treg puts your "
                 "team's own APIs, CLIs and skills behind the same token, and adds SEO, enrichment, "
                 "social and ad-library data."),
            ("How much does Locus cost?",
                 "Locus pricing is a set of monthly plans for Locus Pro: Free at $0 with no included "
                 "credit, Plus at $20, Enterprise at $200 and Enterprise Plus at $1,000 a month. Locus "
                 "runs tools at catalog base price, does not publish a markup, and adds a card "
                 "processing fee on top-ups."),
            ("Can I use my own API keys with Locus?",
                 "On Enterprise. Locus connects your own provider credential to custom API actions in "
                 "an Enterprise workspace, while treg accepts your team's keys on every account. On "
                 "treg, your key always wins and those calls are never metered."),
            ("Locus vs treg: what is the difference?",
                 "The main difference is who each one is built for. Locus is built to resell tools to "
                 "your customers, with per-customer balances, your own markup and an embedded billing "
                 "widget. treg is built around your team: your connected Google Analytics, Search "
                 "Console, Meta Ads, LinkedIn or Slack accounts, and your team's own tools."),
            ("Can I use Locus and treg together?",
                 "Yes. Locus and treg are both remote MCP servers, so one agent can hold both "
                 "connections. A platform that resells tools to its customers may want Locus's billing "
                 "layer, and a team whose agent needs GTM data and its own accounts may want treg."),
            ("How does treg pricing work compared with Locus?",
                 "treg has no subscription: you top up a team balance and pay per call at the "
                 "provider's rate, with 0% markup in its terms and no top-up fee. treg gives $1.00 of "
                 "credit once per new verified account, with no card. Locus Pro's paid plans run from "
                 "$20 to $1,000 a month."),
        ],
        "sources": [
            ("Locus home page", "https://paywithlocus.com"),
            ("Locus pricing", "https://paywithlocus.com/pricing"),
            ("Funding and billing", "https://docs.paywithlocus.com/locus-pro/funding-and-billing"),
            ("Connect agents", "https://docs.paywithlocus.com/locus-pro/connect-agents"),
            ("API keys and BYOK", "https://paywithlocus.com/guides/api-key-management-and-byok"),
            ("x402", "https://paywithlocus.com/x402/index.md"),
        ],
    },
    "monid": {
        "name": "Monid",
        "site": "https://monid.ai",
        "group": "per-call",
        "mono": "#111827",
        "logo": "/media/compare/logos/monid.svg",
        "logo_fills": True,
        "title": "treg vs Monid: two tool catalogs for AI agents",
        "description": "Monid and treg both give agents paid tools from a prepaid balance. treg adds "
                       "0% markup in its terms and your own keys, never metered.",
        "sub": "0% markup in the terms, and your own keys first. Like Monid, treg sells paid tools "
               "from a prepaid balance.",
        "their_words": "Connect your agent to every tool it needs.",
        "tldr": "Monid and treg both give agents a catalog of paid tools from a prepaid balance, with "
                "$1 to start and no subscription. treg is like OpenRouter for tools: one API key and "
                "one balance for many paid APIs. treg has {ENDPOINTS} tools across {PROVIDERS} "
                "providers, 0% markup written into its terms, your own keys and connected accounts "
                "never metered, and team spend caps. Monid publishes its connector definitions and "
                "engine under MIT, with a large community on GitHub. treg's whole registry is on "
                "GitHub too, and you can self-host it for your team.",
        "spec": {"billing": "Prepaid, per call or result", "start": "$1",
                 "keys": NOT_PUBLISHED, "code": "Connectors MIT"},
        "fit_them": [
            "You want to read or contribute to MIT-licensed connector definitions.",
            "You want agent email and agent phone tools.",
            "You embed it in your product with a \"Sign in with Monid\" OAuth flow or your own proxy.",
            "You want discovery that ranks candidate tools by fit and price.",
        ],
        "fit_treg": [
            "You want your own provider keys on the hosted service, used first and never metered.",
            "Your agent works in your own Google Analytics, Search Console, Google Ads or Meta Ads "
            "accounts.",
            "You want your team's own APIs, CLIs and skills behind the same token.",
            "You want a team daily spend cap, a per-call cost ceiling and per-member call caps.",
            "You want 0% markup written into the terms, and code on GitHub you can self-host.",
        ],
        "cells": _c(
            how_pay="A prepaid wallet, pay as you go. No subscription.",
            free_start="$1 to start.",
            fees="Per-call or per-result prices. A markup is not published.",
            x402="Yes: USDC on Base or Monad.",
            catalog="2,800+ tools across 90+ providers.",
            data="Yes: sales intelligence, media generation, voice, web search and scraping, social and "
                 "marketplace data, SEO, people and brand data, DeFi.",
            own_accounts=NOT_PUBLISHED,
            actions="Agent email and agent phone tools.",
            answer=NOT_PUBLISHED,
            mcp="A remote MCP server with OAuth.",
            api_cli="A REST API, a CLI and an agent skill.",
            own_keys="Not published for the hosted service. The open-source engine runs locally with your "
                     "own vendor key.",
            open_source="The connector layer and engine are MIT-licensed. The hosted platform is not "
                        "published.",
            price_before="Yes: prices in discover, inspect and run responses.",
            caps=NOT_PUBLISHED,
            logs="Runs and wallet activity.",
            team="Workspaces. Roles are not published.",
        ),
        "better": [
            ("Open connectors.", "Monid's connector definitions and engine are MIT-licensed, with a large "
             "community on GitHub."),
            ("x402.", "An agent can pay a run in USDC on Base or Monad, with no Monid balance needed for "
             "that run."),
            ("Agent email and phone.", "Tools that let an agent send email or make a phone call."),
            ("Embedding.", "\"Sign in with Monid\" for your users' own workspaces, or your own proxy for "
             "billing control."),
            ("Ranked discovery.", "Discovery returns candidate tools ranked by fit and price, with a public "
             "rankings page."),
        ],
        "different": [
            ("0% markup, in the terms.", "Each tool is billed at the provider's own rate, and no markup is "
             "a commitment in treg's terms. No top-up fee."),
            ("A large catalog, plus your own tools.", "{ENDPOINTS} tools across {PROVIDERS} providers "
             "(Monid lists 2,800+ across 90+), and your team's own APIs, CLIs and skills behind the same "
             "token."),
            ("Your own keys, on the hosted service.", "Register your key for a provider and treg uses it "
             "first, every time, and never meters the call."),
            ("Your connected accounts.", "Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn, "
             "Slack and YouTube, connected by OAuth and never metered."),
            ("Spend controls for a team.", "A team daily spend cap, a per-call cost ceiling and per-member "
             "daily call caps, with owner, admin, member and viewer roles."),
        ],
        "both": "Both are remote MCP servers with an API, a CLI and an agent skill. Search both for the "
                "tools you need, and compare the price of the calls you will make.",
        "faq": [
            ("Is there a Monid alternative for AI agents?",
                 "Yes. treg is a Monid alternative for AI agents: like Monid, treg gives an agent a "
                 "catalog of paid tools from a prepaid balance. treg also puts your team's own APIs, "
                 "CLIs and skills behind the same token, beside the catalog."),
            ("How much does Monid cost?",
                 "Monid pricing is per call or per result from a prepaid wallet, for example $0.003 a "
                 "call, with $1 to start and no subscription. Monid shows prices in its discover, "
                 "inspect and run responses."),
            ("Can I use my own API keys with Monid?",
                 "Monid does not publish this for its hosted service; the open-source Monid engine can "
                 "run a connector locally with your own vendor key. On treg, your team's key for a "
                 "provider always wins and is never metered, on every account."),
            ("Monid vs treg: what is the difference?",
                 "The main difference is what each one adds. Monid offers MIT-licensed connectors, "
                 "agent email and phone tools, and discovery ranked by fit and price. treg connects "
                 "your Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn, Slack and "
                 "YouTube accounts, never metered, and sets a team daily spend cap, a per-call cost "
                 "ceiling and per-member daily call caps."),
            ("Can I use Monid and treg together?",
                 "Yes. Monid and treg are both remote MCP servers with an API, a CLI and an agent "
                 "skill, so one agent can hold both connections. Search Monid and treg for the tools "
                 "you need, and compare the price of the calls you will make."),
            ("How does treg pricing work compared with Monid?",
                 "treg bills each call at the provider's own rate, with 0% markup written into its "
                 "terms and no top-up fee. treg gives $1.00 of credit once per new verified account, "
                 "with no card. Monid does not publish a markup."),
            ("Can I self-host treg?",
                 "Yes. treg's code is on GitHub under Apache 2.0 with added terms, and you can "
                 "self-host treg for your own team. Monid's connector layer and engine are "
                 "MIT-licensed on GitHub; Monid does not publish its hosted platform."),
        ],
        "sources": [
            ("Monid home page", "https://monid.ai"),
            ("How it works", "https://monid.ai/docs/guide/how-it-works.md"),
            ("Pay with x402", "https://monid.ai/docs/guide/pay-with-x402.md"),
            ("API overview", "https://monid.ai/docs/api/overview.md"),
            ("Monid on GitHub", "https://github.com/monid-ai/monid"),
        ],
    },
    "anyapi": {
        "name": "AnyAPI",
        "site": "https://getanyapi.com",
        "group": "per-call",
        "mono": "#1F4FE0",
        "logo": "/logos/anyapi.svg",
        "logo_fills": True,
        "checked": "2026-11-05",
        "title": "treg vs AnyAPI: an AnyAPI alternative for AI agents",
        "description": "AnyAPI routes data APIs per request with failover. treg's catalog includes "
                       "AnyAPI's endpoints beside many other providers.",
        "sub": "AnyAPI's endpoints are in the treg catalog, beside SEO, people and ad data on one key "
               "and one balance.",
        "their_words": "We route you to the cheapest, most reliable data.",
        "tldr": "Like OpenRouter for models, AnyAPI and treg both give an agent one API key and one "
                "prepaid balance for many paid data APIs, priced per call in US dollars. AnyAPI's "
                "endpoints are in the treg catalog, so your agent can call AnyAPI beside SEO, people "
                "data, ad library and generation providers through one token, with 0% markup and your "
                "own keys never metered. AnyAPI routes each request to the cheapest of several "
                "sources, fails over to the next one on its own, and returns one schema for every "
                "source. Use AnyAPI directly, or reach it through treg beside everything else.",
        "spec": {
            "billing": "Prepaid USD wallet, per request",
            "start": "Free requests, no card",
            "keys": "Not published",
            "code": "CLI, MCP and skills on GitHub",
        },
        "fit_them": [
            "Your agent mostly reads social, search and web data, and you want failover across "
                "sources built in.",
            "You want agents to pay per call with x402 or MPP, with no account.",
            "You want typed TypeScript or Python SDKs, an n8n node or LangChain tools.",
            "You want a USD spend limit, an expiry and an access level on each key you hand out.",
            "You want to choose, skip or prefer faster sources on each request.",
        ],
        "fit_treg": [
            "Your agent calls AnyAPI beside SEO, people data, ad library and generation providers, "
                "and you want one token and one balance for all of them.",
            "You want your team's Google Analytics, Search Console, ad accounts and Slack beside "
                "the data, never metered.",
            "You have your own AnyAPI key, or other providers' keys, and want them to win, never "
                "metered.",
            "You want a team daily spend cap, a per-call cost ceiling and per-member daily call "
                "caps.",
            "You want the code on GitHub and the option to self-host for your team.",
        ],
        "cells": {
            "how_pay": "A prepaid USD wallet, drawn per request (most APIs per result returned) at the "
                       "catalog price. No subscription, seat or minimum. Card top-ups on Stripe from "
                       "$10 to $10,000, with optional auto top-up.",
            "free_start": "Free credit on a new account, no card: the home page says ~160 free "
                          "requests, its FAQ $0.10 of credit. An agent can mint a trial key (about "
                          "$0.05, 7 days) with no account.",
            "fees": "No top-up fee. A call one source missed is charged at the rescuing source's "
                    "price, capped at the API's published maximum. Some failed requests carry "
                    "processing charges passed through at cost. Sellers pay a 20% commission.",
            "x402": "Yes: every endpoint takes inline x402 (USDC on Base) or MPP (Tempo) payment, with "
                    "no account or key.",
            "catalog": "472 APIs across 97 platforms (catalog page).",
            "data": "Yes: social profiles and posts, search and SEO, people and company enrichment, "
                    "maps, e-commerce, travel and finance data. No login at the platform is used.",
            "own_accounts": "Not published. AnyAPI says no endpoint asks for a login, cookie or "
                            "session token for the platform you read.",
            "actions": "Not published. The catalog is scraping and data APIs.",
            "answer": "One normalized JSON shape per endpoint, the same for every source: output, "
                      "provider, costUsd and items.",
            "mcp": "A remote MCP server (Streamable HTTP) with your AnyAPI key as a Bearer token, a "
                   "local stdio package, and a listing in the official MCP Registry. Discovery works "
                   "without a key.",
            "api_cli": "A REST API with an OpenAPI spec, typed TypeScript and Python SDKs, a CLI, an "
                       "agent skill, an n8n node and LangChain tools.",
            "own_keys": "Not published. Calls run on AnyAPI's sources; you can choose or skip sources "
                        "per request.",
            "open_source": "The CLI, MCP server, agent skills and n8n node are on GitHub under MIT or "
                           "Apache 2.0. Self-hosting the gateway is not published.",
            "price_before": "Yes: each endpoint's price, and each source's price and median latency, "
                            "in the public catalog, plus a quote for your exact request.",
            "caps": "Per-key USD spend limits that reset daily, weekly, monthly or never, key expiry "
                    "and access levels, and a per-request max_cost_usd ceiling.",
            "logs": "Per-key usage by API and day, request history, and stored responses for 3 days by "
                    "default (7 days or none). A Team audit log of changes.",
            "team": "Teams with one balance and Admin, Developer, Billing and Viewer roles. Team keys "
                    "keep working when the person who made them leaves.",
        },
        "treg_cells": {
            "catalog": "{ENDPOINTS} tools across {PROVIDERS} providers, including AnyAPI's social, "
                       "search and web data endpoints, plus the tools your team registers.",
        },
        "better": [
            ("Failover across sources.",
                 "Each endpoint has ranked sources behind it. When the first one fails, AnyAPI sends "
                 "the request to the next, and it publishes each source's record."),
            ("One schema per job.",
                 "Every source behind an endpoint returns the same normalized JSON, so code does not "
                 "change when the source does."),
            ("Pay with x402 or MPP.",
                 "Agents can pay each call inline in USDC on Base or on Tempo, with no account and no "
                 "key."),
            ("Budgets on every key.",
                 "Each key can carry its own USD limit with a daily, weekly, monthly or lifetime "
                 "window, an expiry and an access level."),
            ("SDKs and self-signup.",
                 "Typed TypeScript and Python SDKs with a method for every API, and an agent can mint "
                 "its own trial key."),
        ],
        "different": [
            ("AnyAPI and more, one token.",
                 "AnyAPI's endpoints are in the catalog, beside SEO and SERP data, people and company "
                 "enrichment, ad libraries and image, video and voice generation: {ENDPOINTS} tools "
                 "across {PROVIDERS} providers."),
            ("Your connected accounts.",
                 "Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn, Slack, X, TikTok "
                 "and YouTube through the same token, never metered."),
            ("Your own keys win.",
                 "Connect your own AnyAPI key, or any provider's, and treg uses it instead of its own, "
                 "never metered."),
            ("0% markup, prices first.",
                 "Each catalog call is billed at the provider's rate, a commitment in the terms, with "
                 "no top-up fee. Each tool's price shows with its source and checked date before the "
                 "call."),
            ("Team caps and open code.",
                 "A team daily spend cap, a per-call cost ceiling and per-member call caps. The code "
                 "is on GitHub, and you can self-host for your team."),
        ],
        "both": "AnyAPI's endpoints are in the treg catalog, so a team can use AnyAPI directly, or "
                "call it through treg beside other providers. Connect your own AnyAPI key to treg, and "
                "your agent's AnyAPI calls run on your AnyAPI wallet, never metered by treg.",
        "faq": [
            ("Is there an AnyAPI alternative for AI agents?",
                 "Yes. treg is an AnyAPI alternative that gives an agent a catalog of paid tools from "
                 "one prepaid balance. AnyAPI focuses on scraping and data APIs with failover across "
                 "sources, while treg adds SEO, people data, ad library and generation tools and your "
                 "connected accounts."),
            ("How much does AnyAPI cost?",
                 "AnyAPI pricing is per request in US dollars from a prepaid wallet, with no "
                 "subscription, seat or minimum, and no top-up fee. AnyAPI prices most APIs per result "
                 "returned, and every AnyAPI response reports its exact charge as costUsd."),
            ("Does AnyAPI have an MCP server?",
                 "Yes. AnyAPI has a remote MCP server at api.getanyapi.com/mcp that takes an AnyAPI "
                 "key as a Bearer token, and treg has a remote MCP server with OAuth or a token "
                 "header. AnyAPI also publishes a local stdio package and lists its server in the "
                 "official MCP Registry."),
            ("AnyAPI vs treg: what is the difference?",
                 "The main difference is routing. AnyAPI routes each request to the cheapest of "
                 "several sources, fails over on its own and returns one schema for every source. treg "
                 "relays each provider's own answer unchanged and adds your connected accounts, such "
                 "as Google Analytics, Search Console, Google Ads and Slack, through the same token, "
                 "never metered."),
            ("Can I use AnyAPI through treg?",
                 "Yes. AnyAPI's endpoints are in the treg catalog, so an agent can call AnyAPI on "
                 "treg's balance through the same token as every other provider. A team that connects "
                 "its own AnyAPI key to treg has those calls run on its AnyAPI wallet, never metered "
                 "by treg."),
            ("How does treg pricing work compared with AnyAPI?",
                 "treg bills each catalog call at the provider's rate with 0% markup, a commitment in "
                 "its terms, and no top-up fee. Each tool's price shows with its source and checked "
                 "date before the call, and treg gives $1.00 of credit once per new verified account. "
                 "AnyAPI also shows each endpoint's price in its public catalog."),
            ("Can I set a spending limit on treg?",
                 "Yes. treg has a team daily spend cap, a per-call cost ceiling and per-member daily "
                 "call caps. AnyAPI sets a USD spend limit on each API key that resets daily, weekly, "
                 "monthly or never, and takes a max_cost_usd ceiling on each request."),
        ],
        "sources": [
            ("AnyAPI home page",
                 "https://getanyapi.com"),
            ("API catalog",
                 "https://getanyapi.com/catalog"),
            ("Introduction and pricing",
                 "https://getanyapi.com/docs"),
            ("MCP server",
                 "https://getanyapi.com/docs/mcp-server"),
            ("API keys and spend controls",
                 "https://getanyapi.com/docs/api-keys"),
            ("Pay per call with x402 and MPP",
                 "https://getanyapi.com/docs/agent-payments"),
            ("Terms of Service",
                 "https://getanyapi.com/terms"),
            ("AnyAPI on GitHub",
                 "https://github.com/getanyapi-com"),
        ],
    },
    "fetchapi": {
        "name": "FetchAPI",
        "site": "https://fetchapi.co",
        "group": "per-call",
        "checked": "2026-11-05",
        "mono": "#15161A",
        "logo": "/media/compare/logos/fetchapi.svg",
        "logo_fills": True,
        "title": "treg vs FetchAPI: a FetchAPI alternative for AI agents",
        "description": "FetchAPI routes data API calls to the cheapest working provider. treg bills "
                       "paid tools at 0% markup. MCP, keys and caps compared.",
        "sub": "Every tool at its provider's own rate, 0% markup in the terms. FetchAPI routes jobs to "
               "the cheapest working provider.",
        "their_words": "One key for every data API.",
        "tldr": "FetchAPI and treg both give an agent one key, prepaid credit and an MCP server for "
                "many paid data APIs. Like OpenRouter for models, both give an agent one API key and "
                "one balance for many paid APIs. treg bills each tool at the provider's rate with 0% "
                "markup in its terms, lets your own keys and connected accounts run unmetered, and "
                "puts its code on GitHub. FetchAPI routes each job to the cheapest provider that "
                "passed its daily live test, and a team can use both.",
        "spec": {
            "billing": "Prepaid, per call, markup",
            "start": "Free key, no card",
            "keys": "By request",
            "code": "Hosted only",
        },
        "fit_them": [
            "You want each job sent to the cheapest provider that works today, with automatic "
                "fallback.",
            "You want every endpoint tested live each day and hidden while it fails.",
            "You want to see who sells a website's data, and at what price, before you buy.",
            "You want to turn a web page into a private data API for your agent.",
            "You need sports and esports odds, prediction markets or DeFi data.",
        ],
        "fit_treg": [
            "You want each call billed at the provider's rate, with 0% markup written in the terms.",
            "You want your own keys to win and never be metered, on every account.",
            "Your agent works in your own accounts: Google Analytics, Search Console, ads, Slack, "
                "X or YouTube.",
            "You want a team daily spend cap, a per-call cost ceiling and per-member call caps.",
            "You want the code on GitHub and a team with owner, admin, member and viewer roles.",
        ],
        "cells": {
            "how_pay": "Prepaid credit bought through Stripe Checkout, $5 to $1,000 a top-up. Each "
                       "successful call is charged from it; failed calls are free.",
            "free_start": "A free key with an email and a code, no card. Some endpoints are priced $0. "
                          "The amount of any free credit is not published.",
            "fees": "The terms say a call costs the provider's price multiplied by FetchAPI's markup "
                    "in force at the time; its llms.txt says calls are charged at the provider's cost. "
                    "The markup rate is not published. Failed calls are free.",
            "catalog": "388 working endpoints, tested daily, from dozens of providers (catalog page).",
            "data": "Yes: X, TikTok, Instagram, YouTube and Reddit data, company and funding data, "
                    "SEO, web traffic, sports odds and prediction markets.",
            "own_accounts": "Not published. Endpoints that need the end user's own X login are listed "
                            "but never called.",
            "actions": "Reads. Write actions (post, follow, like) are listed in the docs but never "
                       "called or exposed over MCP.",
            "answer": "The provider's JSON, unchanged, inside a status, data and meta wrapper with the "
                      "cost; ?raw=1 returns the body as is. Standard job endpoints return one answer "
                      "shape.",
            "mcp": "A remote MCP server (streamable HTTP, Bearer key) with tools to search endpoints, "
                   "read their docs with a real sample answer, and call them. Search needs no key.",
            "api_cli": "An HTTP API by provider path, over 100 standard /api/v1 job endpoints, an "
                       "OpenAPI 3.1 spec and an agent skill file. A CLI or SDK is not published.",
            "own_keys": "By request: \"talk to us about routing your own account through the gateway.\"",
            "open_source": "Not published. Hosted service; no public repository is linked.",
            "price_before": "Yes: every endpoint shows its price per call. The terms call it a guide; "
                            "the charge recorded with the call is what you pay.",
            "caps": "A monthly spending cap and a per-minute rate limit on each key, and a "
                    "service-wide daily cap. The terms say FetchAPI sets the limits and may change "
                    "them.",
            "logs": "A request log per key (what, when, status, cost) and usage by provider and "
                    "endpoint over 24 hours, 7 or 30 days. Response data is not stored.",
            "team": "An account is for one person or one organisation. Team roles or seats are not "
                    "published.",
        },
        "better": [
            ("The cheapest route, picked for you.",
                 "Standard endpoints such as /api/v1/x/search go to the cheapest provider that passed "
                 "the day's check, and fall back to the next if it fails. You pay only for the call "
                 "that answered."),
            ("Tested every day.",
                 "Each endpoint is called for real through the gateway daily and hidden while it "
                 "fails, with a public health status."),
            ("A priced map of the market.",
                 "/api/cheapest ranks who sells a website's data, cheapest first, with the source of "
                 "each price and how sure it is. No key needed."),
            ("Make an API from a web page.",
                 "An AI studies a page once and writes a private API for it, usually for under $0.40 "
                 "of AI cost. Later calls run without an LLM."),
            ("Odds and markets data.",
                 "Sports and esports odds, prediction markets and DeFi data sit beside its social and "
                 "company data."),
        ],
        "different": [
            ("0% markup, in the terms.",
                 "treg bills each tool at the provider's own rate, with 0% markup as a commitment in "
                 "its terms and no top-up fee. FetchAPI's terms apply a markup whose rate is not "
                 "published."),
            ("Your own keys and accounts win.",
                 "Register your key for a provider and treg uses it instead of its own, never metered. "
                 "Connect Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn, Slack, X, "
                 "TikTok or YouTube, and the agent reads and writes there, unmetered."),
            ("A larger catalog, plus your own tools.",
                 "{ENDPOINTS} tools across {PROVIDERS} providers, with image, video and voice "
                 "generation, and your team's own APIs, CLIs and skills behind the same token."),
            ("Your own caps.",
                 "A team daily spend cap, a per-call cost ceiling and per-member daily call caps, set "
                 "by your team. Each tool's price shows its source and the date it was checked."),
            ("Code on GitHub.",
                 "treg's code is on GitHub and you can self-host it for your team. Teams have owner, "
                 "admin, member and viewer roles, with tool access per member."),
        ],
        "both": "An agent can hold both. A team could use FetchAPI for cheapest-route social data, "
                "sports odds or a page turned into an API, and use treg for its own keys, its "
                "connected accounts, the wider catalog and team spend caps.",
        "faq": [
            ("Is there a FetchAPI alternative for AI agents?",
                 "Yes. treg is a FetchAPI alternative for AI agents: one token reaches a catalog of "
                 "paid tools through a remote MCP server, an HTTP API and a CLI, billed per call from "
                 "one prepaid balance at the provider's rate. FetchAPI also gives one key for many "
                 "data APIs, and routes each job to the cheapest working provider."),
            ("How much does FetchAPI cost?",
                 "FetchAPI pricing is prepaid credit, bought through Stripe in top-ups of $5 to $1,000 "
                 "and charged per successful call. Failed FetchAPI calls are free. FetchAPI shows a "
                 "price per call on every endpoint, and its terms call that price a guide."),
            ("Can I use my own API keys with FetchAPI?",
                 "By request. FetchAPI's home page says to talk to FetchAPI about routing your own "
                 "provider account through the gateway. On treg, your team registers its own key on "
                 "every account, and that key always wins and is never metered."),
            ("FetchAPI vs treg: which is cheaper for an AI agent?",
                 "It depends on the markup and the route. treg bills each tool at the provider's own "
                 "rate, with 0% markup in its terms and no top-up fee. FetchAPI's terms multiply the "
                 "provider's price by a markup whose rate is not published, and FetchAPI routes "
                 "standard jobs to the cheapest provider that passed its daily test."),
            ("Can I use FetchAPI and treg together?",
                 "Yes. An agent can hold both FetchAPI and treg. A team could use FetchAPI for "
                 "cheapest-route social data, sports odds or a page turned into an API, and use treg "
                 "for its own keys, its connected accounts, the wider catalog and team spend caps."),
            ("Does treg have an MCP server?",
                 "Yes. treg has a remote MCP server with OAuth or a token header, for Claude, ChatGPT, "
                 "Claude Code, Codex, Cursor and others, beside an HTTP API, a CLI and an agent skill. "
                 "FetchAPI also has a remote MCP server, with a Bearer key."),
            ("Can I set a spending limit on treg?",
                 "Yes. treg has a team daily spend cap, a per-call cost ceiling and per-member daily "
                 "call caps, all set by your team. FetchAPI gives each key a monthly spending cap and "
                 "a per-minute rate limit, and its terms say FetchAPI sets these limits."),
        ],
        "sources": [
            ("FetchAPI home page",
                 "https://fetchapi.co"),
            ("llms.txt",
                 "https://fetchapi.co/llms.txt"),
            ("Catalogue",
                 "https://fetchapi.co/catalog.md"),
            ("MCP and API docs",
                 "https://fetchapi.co/mcp"),
            ("Account (credit and keys)",
                 "https://fetchapi.co/account"),
            ("Terms of Service",
                 "https://fetchapi.co/terms"),
        ],
    },
    "apihub": {
        "checked": "2026-11-05",
        "name": "APIHub",
        "site": "https://apihub.io",
        "group": "per-call",
        "mono": "#3B82F6",
        "logo": "/media/compare/logos/apihub.svg",
        "logo_fills": True,
        "title": "treg vs APIHub: an APIHub alternative for AI agents",
        "description": "APIHub sells APIs to agents for USDC credits. treg bills paid tools from one "
                       "balance at 0% markup. Fees, MCP, keys and caps compared.",
        "sub": "Card top-ups at 0% markup, and your own keys win. APIHub sells listed APIs to agents "
               "for USDC credits or x402.",
        "their_words": "The API marketplace built for agents.",
        "tldr": "Like OpenRouter for models, APIHub and treg both give an agent one API key, one "
                "prepaid balance and one MCP server for many paid APIs. treg bills each call at the "
                "provider's own rate with 0% markup in the terms, uses your own keys first and never "
                "meters them, and connects your Google Analytics, Search Console and ad accounts. "
                "APIHub lets an autonomous agent sign up with only a wallet signature, and lets any "
                "developer list an API and keep 90% of each call. One agent can hold both connections.",
        "spec": {
            "billing": "Prepaid USDC credits or x402",
            "start": "Free account, some free quotas",
            "keys": "Not published",
            "code": "Hosted; CLI on npm",
        },
        "fit_them": [
            "Your agent holds a wallet and you want it to sign up and pay in USDC with no human "
                "step.",
            "You want to call x402 services from one prepaid balance, with no wallet in the agent.",
            "You want to sell your own API to agents, priced per endpoint, and keep 90%.",
            "You want an API's free monthly quota before you pay.",
            "You want to read paid web pages through a content gateway.",
        ],
        "fit_treg": [
            "You want each call billed at the provider's own rate, with 0% markup in the terms.",
            "You want your own provider keys used first and never metered.",
            "Your agent works in your own Google Analytics, Search Console or ad accounts, and you "
                "want those calls never metered.",
            "You want a team daily spend cap, a per-call cost ceiling and per-member call caps.",
            "Several people share one account with owner, admin, member and viewer roles, and you "
                "want the code on GitHub.",
        ],
        "cells": {
            "how_pay": "Prepaid credits bought in USDC on Base (from $5), or per-request x402 in USDC. "
                       "Each provider sets a price per call.",
            "free_start": "A free account by magic email link or wallet signature. Some APIs have a "
                          "free monthly quota. A signup credit is not published.",
            "fees": "A 10% default commission, taken from the provider's payout. External x402 calls "
                    "are charged the provider price plus a platform markup, 10% by default.",
            "x402": "Yes: x402 and USDC on Base, and external x402 services paid from credits.",
            "catalog": "334 services on the marketplace page, 267 of them external x402 services. The "
                       "home page says 75+ external x402 services.",
            "data": "Yes: listed APIs in AI, data, search, finance and trading, media, communication, "
                    "content and travel.",
            "own_accounts": "Not published",
            "actions": "Whatever each listed API offers.",
            "answer": "Native calls: the upstream response with x-apihub headers (charge, balance, "
                      "receipt). External x402 calls: the upstream response wrapped in charge metadata.",
            "mcp": "A remote MCP server (Streamable HTTP) with a Bearer key and 9 tools to search, "
                   "call, top up and check the balance.",
            "api_cli": "A REST API, a proxy, a CLI (MIT on npm), starter kits and agent skills. An SDK "
                       "is not published.",
            "own_keys": "Not published",
            "open_source": "No. Hosted service. The MCP server's README and the CLI (MIT) are public. "
                           "Self-hosting is not published.",
            "price_before": "Yes: each endpoint's price per request on the marketplace and through MCP.",
            "caps": "Not published. Rate limits of 100 requests a minute, or 1,000 with a credit "
                    "balance.",
            "logs": "Credit history by API, onboarded call charges in the dashboard, and a receipt "
                    "hash on each paid response.",
            "team": "Up to 25 keys on one profile, sharing one balance. Teams and roles are not "
                    "published.",
        },
        "better": [
            ("x402 and USDC on Base.",
                 "An agent can buy credits in USDC or pay per request with x402, and call external "
                 "x402 services from its credits with no wallet of its own."),
            ("Wallet sign-up.",
                 "An autonomous agent can register by signing a challenge with its EVM wallet, with no "
                 "email and no approval."),
            ("Sell your own API.",
                 "Any developer can list an API, set a price per endpoint, offer a free monthly quota, "
                 "and keep 90%."),
            ("Free monthly quotas.",
                 "Providers can give each agent a set number of free calls a month, used before "
                 "credits."),
            ("Paid web content.",
                 "A content gateway reads pages from sites that set a per-page price and returns clean "
                 "structured text."),
        ],
        "different": [
            ("0% markup, in the terms.",
                 "treg bills each tool at the provider's own rate, with no top-up fee. APIHub keeps a "
                 "10% default commission from each provider's payout."),
            ("Your own keys win.",
                 "Register your key for a provider and treg uses it instead of its own, never metered."),
            ("Your connected accounts.",
                 "Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn and Slack, never "
                 "metered."),
            ("Your own caps.",
                 "A team daily spend cap, a per-call cost ceiling and per-member daily call caps, with "
                 "each tool's price, source and checked date before the call."),
            ("Teams and open code.",
                 "Owner, admin, member and viewer roles with tool access per member. The code is on "
                 "GitHub, and you can self-host for your team."),
        ],
        "both": "An agent can hold both. A team could keep APIHub for x402 services, wallet-paid calls "
                "or to sell its own API, and use treg for the tools it calls often, its own keys, its "
                "connected accounts and one team balance.",
        "faq": [
            ("Is there an APIHub alternative for AI agents?",
                 "Yes. treg is an APIHub alternative that gives an agent a catalog of paid tools "
                 "through one MCP server, an HTTP API and a CLI, billed per call from one prepaid "
                 "balance. treg also connects your own Google Analytics, Search Console and ad "
                 "accounts, and those calls are never metered."),
            ("How much does APIHub cost?",
                 "APIHub pricing is per call: each provider sets a price, from $0.001 to $10+ per "
                 "request. APIHub credits are bought in USDC on Base and start at $5. Some APIs on "
                 "APIHub have a free monthly quota."),
            ("Can I use my own API keys with APIHub?",
                 "APIHub does not publish a way to use your own provider key; providers add their own "
                 "upstream credential. treg accepts your team's keys on every account, uses them first "
                 "and never meters those calls."),
            ("APIHub vs treg: what is the difference?",
                 "The main difference is who sets the price. On APIHub each provider sets a price per "
                 "call and sells through APIHub, while treg relays each provider's own API at the "
                 "provider's own rate. treg returns the provider's own answer, unchanged."),
            ("Can I use APIHub and treg together?",
                 "Yes. An agent can hold both APIHub and treg. A team could keep APIHub for x402 "
                 "services, wallet-paid calls or to sell its own API, and use treg for the tools it "
                 "calls often, its own keys, its connected accounts and one team balance."),
            ("How does treg pricing work compared with APIHub?",
                 "treg bills each call at the provider's own rate from a prepaid balance, with 0% "
                 "markup in its terms and no top-up fee. treg gives $1.00 of credit once per new "
                 "verified account, with no card. APIHub keeps a 10% default commission from each "
                 "provider's payout."),
            ("Can I set a spending limit on treg?",
                 "Yes. treg has a team daily spend cap, a per-call cost ceiling and per-member daily "
                 "call caps, and shows each tool's price with its source before the call. APIHub does "
                 "not publish a spend cap; APIHub publishes rate limits of 100 requests a minute, or "
                 "1,000 with a credit balance."),
        ],
        "sources": [
            ("APIHub home page",
                 "https://apihub.io"),
            ("APIHub marketplace",
                 "https://apihub.io/marketplace"),
            ("APIHub docs",
                 "https://apihub.io/docs"),
            ("Credits",
                 "https://apihub.io/docs/agents/credits"),
            ("MCP integration",
                 "https://apihub.io/docs/agents/mcp"),
            ("Provider API services",
                 "https://apihub.io/docs/providers/api-services"),
            ("Terms of service",
                 "https://apihub.io/terms"),
            ("APIHub MCP server on GitHub",
                 "https://github.com/apihubio/mcp-server"),
        ],
    },
    "rapidapi": {
        "checked": "2026-11-05",
        "name": "RapidAPI",
        "site": "https://rapidapi.com",
        "group": "per-call",
        "mono": "#0055DA",
        "logo": "/media/compare/logos/rapidapi.png",
        "logo_fills": False,
        "title": "treg vs RapidAPI: RapidAPI pricing vs one prepaid balance",
        "description": "RapidAPI pricing is a plan per API, set by its provider. treg bills each tool "
                       "from one prepaid balance at 0% markup, on one MCP server.",
        "sub": "A single prepaid balance at 0% markup for every tool. On RapidAPI, each API has its "
               "own plan.",
        "their_words": "Nokia API Hub is more than just a catalog for APIs.",
        "tldr": "RapidAPI and treg both put many APIs behind one key. treg is like OpenRouter for "
                "tools: one API key and one balance for many paid APIs. treg bills every call from one "
                "prepaid balance at the provider's rate, with 0% markup, one MCP server for the whole "
                "catalog, and your own keys used first and never metered. RapidAPI, now the Nokia API "
                "Hub, lists tens of thousands of APIs, each on a plan its provider sets, and lets any "
                "developer list and sell an API. A team can use both, with treg as the one balance for "
                "the tools its agents call most.",
        "spec": {"billing": "A plan per API, set by its provider", "start": "Free BASIC plans on many APIs",
                 "keys": "Some APIs need the provider's key", "code": "Hosted only"},
        "fit_them": [
            "You need an API that only RapidAPI lists: it has tens of thousands.",
            "You want to list and sell your own API, with plans, quotas and billing handled.",
            "You want to try an API on its free BASIC plan before you pay.",
            "You want a year of call history, with full request and response logs.",
        ],
        "fit_treg": [
            "Your agent uses many tools and you want one balance, not a subscription for each.",
            "You want each call billed at the provider's rate, with 0% markup and no top-up fee.",
            "You want one MCP server with OAuth for the whole catalog, plus an HTTP API, a CLI and a skill.",
            "You want a team daily spend cap, a per-call cost ceiling and per-member call caps.",
            "You want your own keys to win and never be metered, and the code on GitHub.",
        ],
        "cells": _c(
            how_pay="A subscription to each API, on plans its provider sets (monthly, pay per use or "
                    "tiers), with quotas and overage. Billed to a card.",
            free_start="A free account. Many APIs have a free BASIC plan with a quota. Freemium APIs "
                       "need a card even on the free plan.",
            fees="Each provider sets its prices. Rapid keeps a flat 25% marketplace fee on all "
                 "payments through the hub, from the provider's payout.",
            catalog="Tens of thousands of APIs (home page).",
            data="Yes: APIs from thousands of providers, in many categories.",
            own_accounts="Some APIs, such as Slack, take a token from your own account at the provider.",
            actions="Whatever each API offers, writes included.",
            answer="The provider's response through the Rapid Runtime, which adds rate-limit headers. "
                   "Providers can set request and response transformations.",
            mcp="An MCP host (mcp.rapidapi.com) that turns an API's endpoints into tools, where it is "
                "enabled. You connect each API with your Rapid key and a subscription.",
            api_cli="Each API's REST endpoint with one Rapid key, code snippets in many languages, and "
                    "GraphQL and REST Platform APIs. A current SDK or CLI is not published.",
            own_keys="Some APIs need a key or token from the provider's own site, beside your Rapid key.",
            open_source="No. Hosted service. The Enterprise Hub is SaaS and can front your own gateways.",
            price_before="Yes: each API's Pricing tab shows plans, quotas, rate limits and overage.",
            caps="Quotas from the plan: the provider sets a hard limit or a soft limit with overage. "
                 "Alerts at 85% and 100% of quota. Your own spend cap is not published.",
            logs="Call history for up to 1 year by app, API and endpoint, raw log export, and full "
                 "request and response logs for recent calls.",
            team="Organizations with teams and admin or developer roles. The first 5 seats are free, "
                 "then $10 a seat a month.",
        ),
        "better": [
            ("Size.", "Tens of thousands of APIs from thousands of providers, used by millions of "
             "developers."),
            ("Sell your own API.", "Any developer can list an API, set plans and quotas, and get paid. "
             "It can also stay private or be shared with chosen teams."),
            ("Free plans to test.", "Many APIs have a free BASIC plan, so you can try calls before you "
             "pay."),
            ("Long call history.", "Up to a year of call history, with full request and response logs "
             "for recent calls and raw log export."),
            ("An internal API hub.", "Teams can publish internal APIs, and the Enterprise Hub adds SSO "
             "and connects existing API gateways."),
        ],
        "different": [
            ("One balance for every tool.", "treg bills each call from one prepaid balance. No "
             "subscription per API, and no seats."),
            ("0% markup, in the terms.", "Each tool is billed at the provider's own rate, with no markup "
             "and no top-up fee."),
            ("One MCP server for the whole catalog.", "One remote MCP connection with OAuth reaches every "
             "tool in treg's catalog, beside an HTTP API, a CLI and an agent skill."),
            ("Spend limits you set.", "A team daily spend cap, a per-call cost ceiling and per-member "
             "daily call caps."),
            ("Your own keys win.", "Register your key for a provider and treg uses it first, never "
             "metered. The code is on GitHub, and you can self-host for your team."),
        ],
        "both": "An agent can hold both. A team could keep RapidAPI for an API it alone lists, or to "
                "sell its own API, and use treg for the tools it calls often, its own keys and one balance.",
        "faq": [
            ("Is there a RapidAPI alternative for AI agents?",
                 "Yes. treg is a RapidAPI alternative for AI agents that bills every call from one "
                 "prepaid balance at the provider's own rate. On RapidAPI, you subscribe to each API "
                 "on a plan that its provider sets."),
            ("How much does RapidAPI cost?",
                 "RapidAPI pricing depends on the API: each provider sets the plans, such as monthly, "
                 "pay per use or tiers, with quotas and overage. Many RapidAPI APIs have a free BASIC "
                 "plan with a quota. RapidAPI team seats cost $10 a month after the first 5."),
            ("Is RapidAPI now part of Nokia?",
                 "Yes. RapidAPI's site says Nokia acquired the technology and R&D unit of Rapid at the "
                 "end of 2024, and the hub is now called Nokia API Hub. treg's code is on GitHub, and "
                 "a team can self-host treg for its own use."),
            ("RapidAPI vs treg: which is cheaper for an AI agent?",
                 "It depends on the APIs and how often the agent calls them. RapidAPI prices come from "
                 "each provider's plan, and Rapid keeps a flat 25% marketplace fee from the provider's "
                 "payout. treg bills each call at the provider's own rate with 0% markup and no top-up "
                 "fee, from one balance with no subscription per API."),
            ("Can I use RapidAPI and treg together?",
                 "Yes. An agent can hold both RapidAPI and treg. A team could keep RapidAPI for an API "
                 "it alone lists, or to sell its own API, and use treg for the tools it calls often, "
                 "its own keys and one balance."),
            ("Does treg have an MCP server?",
                 "Yes. treg has one remote MCP server, with OAuth or a token header, for every tool in "
                 "the catalog. RapidAPI's MCP host at mcp.rapidapi.com turns an API's endpoints into "
                 "tools where the provider enables MCP, with access set by your subscription to that "
                 "API."),
            ("Can I set a spending limit on treg?",
                 "Yes. treg has a team daily spend cap, a per-call cost ceiling and per-member daily "
                 "call caps. RapidAPI limits come from each API's plan, with a hard limit or billed "
                 "overage and alerts at 85% and 100% of quota; a spend cap you set yourself is not "
                 "published."),
        ],
        "sources": [
            ("RapidAPI home page", "https://rapidapi.com"),
            ("What is rapidapi.com?", "https://docs.rapidapi.com/docs/what-is-rapidapi"),
            ("Plans and pricing", "https://docs.rapidapi.com/docs/api-pricing"),
            ("Payouts and finance", "https://docs.rapidapi.com/docs/payouts-and-finance"),
            ("Consume APIs using AI (MCP)", "https://docs.rapidapi.com/docs/consume-apis-using-ai"),
            ("API keys", "https://docs.rapidapi.com/docs/keys-and-key-rotation"),
            ("Consumer analytics", "https://docs.rapidapi.com/docs/consumer-analytics"),
            ("RapidAPI for Teams FAQ", "https://docs.rapidapi.com/docs/faqs-rapidapi-for-teams"),
        ],
    },
    "apify": {
        "checked": "2026-11-05",
        "name": "Apify",
        "site": "https://apify.com",
        "group": "per-call",
        "mono": "#246DFF",
        "logo": "/media/compare/logos/apify.svg",
        "logo_fills": False,
        "title": "treg vs Apify: Apify MCP, pricing and an alternative",
        "description": "Apify MCP reaches 109,000+ scraping Actors. treg's MCP server gives agents "
                       "data APIs per call at 0% markup. Pricing and caps compared.",
        "sub": "Data APIs that answer in the same call, at 0% markup. Apify runs Actors that scrape "
               "almost any site.",
        "their_words": "Apify is a marketplace of ready-to-run tools for AI that automate work on the web.",
        "tldr": "Apify and treg both give your agent paid tools through one MCP server. treg is like "
                "OpenRouter for tools: one API key and one balance for many paid APIs. treg is a "
                "catalog of commercial data and generation APIs, billed per call from a prepaid "
                "balance at the provider's rate with 0% markup, and each call returns the provider's "
                "own answer. Apify Store has 109,000+ Actors, cloud programs by Apify and other "
                "developers that scrape and automate the web, paid from a plan's monthly usage. Use "
                "Apify for a site with no API, and treg for SEO, enrichment, ad and generation data, "
                "with your own keys and accounts beside it.",
        "spec": {"billing": "Plans with monthly usage, then per event", "start": "$5 a month, no card",
                 "keys": NOT_PUBLISHED, "code": "MCP server and Crawlee open source"},
        "fit_them": [
            "You need data from a site that has no API: Apify has an Actor, or you can build one.",
            "You want residential and SERP proxies for scraping hard sites.",
            "You want $5 of free usage every month, with no card.",
            "You want to build Actors and sell them in Apify Store.",
            "You want runs on a schedule, with webhooks and workflow integrations.",
        ],
        "fit_treg": [
            "Your agent needs SEO, enrichment, ad or generation data from commercial APIs.",
            "You want the provider's own answer in the call, not a run and a dataset to read after.",
            "You want a prepaid balance with no plan and no seats, at 0% markup.",
            "You want your own keys to win and never be metered, and your own accounts behind one token.",
            "You want a team daily spend cap, a per-call cost ceiling and per-member call caps.",
        ],
        "cells": _c(
            how_pay="Plans with prepaid monthly usage: Free $0 ($5), Starter $19, Scale $199, Business "
                    "$999. Actors bill per event or per compute unit; paid plans pay overage up to a "
                    "limit you set.",
            free_start="$5 of usage every month, no card. Access pauses when it runs out.",
            fees="Actor prices are set by their developers; Apify keeps 20% of an Actor's revenue. "
                 "Compute is $0.13 to $0.20 per unit by plan. Unused monthly usage expires.",
            x402="Yes: x402 or MPP (USDC on Base or Solana, and more) buys a prepaid token with a fixed "
                 "spend cap, from $1. The token expires after 14 days.",
            catalog="109,000+ ready-to-run tools called Actors (Apify Store).",
            data="Yes, mostly web scraping: social platforms, maps, search results, e-commerce and any "
                 "website, through Actors by Apify and other developers.",
            own_accounts="Integrations send run results to Google Drive, Airtable, HubSpot, Snowflake, "
                         "Zapier, Make and n8n.",
            actions="Actors automate work on the web. Schedules, tasks and webhooks run without the agent.",
            answer="An Actor run. Its results go to a dataset that you read with a second call, in the "
                   "shape the Actor's developer chose.",
            mcp="A remote MCP server (mcp.apify.com) with OAuth or a token, a local stdio server and "
                "plugins for coding agents. The server is open source (MIT).",
            api_cli="A REST API with OpenAPI, JavaScript and Python clients, a CLI and Agent Skills.",
            own_keys=NOT_PUBLISHED,
            open_source="Crawlee and the CLI under Apache 2.0, the MCP server under MIT. Self-hosting "
                        "the platform is not published.",
            price_before="Yes for pay-per-event Actors, on each Actor's page. Pay-per-usage cost is "
                         "known after a test run.",
            caps="A monthly usage limit on paid plans (at most $100 of overage on Starter) and a "
                 "maximum charge per run.",
            logs="A log for each run and a daily usage chart in billing.",
            team="Organization accounts on all plans, with members, permissions and one bill.",
        ),
        "better": [
            ("The size of the store.", "109,000+ Actors, built by Apify and other developers. If a site "
             "has no API, there may be an Actor for it, or you can build one with the open-source "
             "Crawlee."),
            ("Pay with no account.", "An agent can pay with x402 or MPP and get a prepaid token with a "
             "fixed spend cap, from $1."),
            ("Free usage every month.", "$5 of usage renews each month, with no card."),
            ("Sell your own tools.", "Developers publish Actors to Apify Store and keep 80% of the "
             "revenue."),
            ("Runs without the agent.", "Schedules, tasks, webhooks and integrations with Zapier, Make, "
             "n8n and data stores."),
        ],
        "different": [
            ("The provider's own answer.", "A treg call returns the provider's answer, unchanged, in the "
             "same call. An Apify call starts a run, and you read its dataset after."),
            ("Commercial data APIs.", "SEO and SERP data, backlinks, people and company enrichment, ad "
             "libraries and image, video and voice generation, on treg's accounts."),
            ("No plan, 0% markup.", "A prepaid balance billed at each provider's rate, with no markup, no "
             "top-up fee and no seats."),
            ("Your own keys and accounts.", "Your key for a provider wins and is never metered. Your "
             "connected Google, Meta and other accounts are behind the same token."),
            ("Self-host for your team.", "treg's code is on GitHub under Apache 2.0 with added terms, and "
             "you can run it for your own team."),
        ],
        "both": "Both are MCP servers, so one agent can hold both. A team could use Apify to scrape a "
                "site without an API, and treg for SEO, enrichment and ad data, its own keys and its "
                "connected accounts.",
        "faq": [
            ("Is there an Apify alternative for AI agents?",
                 "Yes, for data APIs. treg is an Apify alternative that gives an agent SEO and SERP "
                 "data, enrichment, ad libraries and image, video and voice generation, billed per "
                 "call. Apify is the choice for a site with no API, with 109,000+ Actors in Apify "
                 "Store."),
            ("How much does Apify cost?",
                 "Apify pricing starts free, with $5 of usage a month and no card. Paid Apify plans "
                 "cost $19, $199 and $999 a month with the same amount of usage, and Actors bill per "
                 "event or per compute unit. Apify keeps 20% of an Actor's revenue."),
            ("Does Apify have an MCP server?",
                 "Yes. Apify runs a remote MCP server at mcp.apify.com with OAuth or a token, a local "
                 "stdio version and plugins for coding agents. Apify's MCP server is open source under "
                 "MIT."),
            ("Apify vs treg: what is the difference?",
                 "The main difference is what a call returns. An Apify call starts an Actor run, and "
                 "you read its dataset with a second call, in the shape the Actor's developer chose. A "
                 "treg call returns the provider's own answer, unchanged, in the same call."),
            ("Can I use Apify and treg together?",
                 "Yes. Apify and treg are both MCP servers, so one agent can hold both. A team could "
                 "use Apify to scrape a site without an API, and treg for SEO, enrichment and ad data, "
                 "its own keys and its connected accounts."),
            ("How does treg pricing work compared with Apify?",
                 "treg bills each call from a prepaid balance at the provider's own rate, with 0% "
                 "markup, no top-up fee, no plan and no seats. treg gives $1.00 of credit once per new "
                 "verified account. On Apify, unused monthly usage expires."),
            ("Can I self-host treg?",
                 "Yes. treg's code is on GitHub under Apache 2.0 with added terms, and you can "
                 "self-host treg for your own team. Apify does not publish a way to self-host its "
                 "platform; Apify open-sources Crawlee and its CLI under Apache 2.0."),
        ],
        "sources": [
            ("Apify home page",
                 "https://apify.com"),
            ("Apify pricing",
                 "https://apify.com/pricing.md"),
            ("Apify Store",
                 "https://apify.com/store"),
            ("Agentic payments (x402, MPP)",
                 "https://agi.apify.com/AGENTS.md"),
            ("Apify MCP server docs",
                 "https://docs.apify.com/platform/integrations/mcp"),
            ("Monetize your Actor",
                 "https://docs.apify.com/platform/actors/publishing/monetize"),
            ("Organization accounts",
                 "https://docs.apify.com/platform/collaboration/organization-account"),
            ("Apify MCP server on GitHub",
                 "https://github.com/apify/apify-mcp-server"),
        ],
    },
    "composio": {
        "name": "Composio",
        "site": "https://composio.dev",
        "group": "apps",
        "mono": "#1F2937",
        "logo": "/media/compare/logos/composio.png",
        "logo_fills": True,
        "title": "treg vs Composio: Composio MCP for apps, treg for paid data",
        "description": "Composio MCP connects agents to 1,000+ apps in each user's own accounts. treg "
                       "gives agents paid data per call at 0% markup.",
        "sub": "Paid data with no vendor accounts, at each provider's rate. Composio connects agents "
               "to apps in each user's accounts.",
        "their_words": "Give your AI secure access to 1,500+ apps in minutes.",
        "tldr": "Both give an agent tools through one MCP server, with your own keys welcome. treg is "
                "like OpenRouter for tools: one API key and one balance for many paid APIs. treg gives "
                "your agent paid data from {ENDPOINTS} tools across {PROVIDERS} providers with no "
                "vendor accounts, at 0% markup, and your own keys and connected accounts are never "
                "metered. Composio connects your agent to 1,000+ apps (Gmail, Slack, Salesforce, "
                "HubSpot) so it acts inside each user's own accounts, with managed OAuth and triggers. "
                "Many agents use both: Composio for the app actions, treg for the data.",
        "spec": {"billing": "Plans, then per call", "start": "100K calls a month free",
                 "keys": "Yes", "code": "SDKs MIT"},
        "fit_them": [
            "Your agent acts inside many SaaS apps for each of your end users.",
            "You need triggers, so the agent reacts to events in those apps.",
            "You are building a multi-user product, with white-labeled OAuth and per-user accounts.",
            "You make very many app calls: 100,000 free a month, then from $0.0003 each.",
            "You need SOC 2 Type II, BAA or zero data retention options.",
        ],
        "fit_treg": [
            "Your agent needs keyword volume, backlinks, work emails, social profiles, ad libraries or "
            "image and video generation, with no account at each vendor.",
            "You want each call billed at the provider's own rate from a prepaid balance, with no plan "
            "and no seats.",
            "You want your own provider keys to win over treg's and never be metered.",
            "You want Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn and Slack on the "
            "same token, never metered.",
            "You want to self-host for your team from the code on GitHub.",
        ],
        "cells": _c(
            how_pay="Plans: Hobby $0 with 100,000 tool calls a month, Pro $29 a month with usage after "
                    "that, Enterprise custom.",
            free_start="Hobby plan, no card, with $2 of Instant Tools credit.",
            fees="Own-app calls $0.0003 each after 100K; managed-app calls $0.0005 to $0.01. Instant Tools "
                 "at 0% markup plus the tool-call charge.",
            catalog="1,500+ apps (home page); 1,000+ toolkits (GitHub README).",
            data="Some, through Instant Tools (off by default): search, scraping and maps from Serper, "
                 "Tavily, SerpApi, Firecrawl, Exa and others. SEO metrics, enrichment and ad data are not "
                 "listed.",
            own_accounts="Yes, the core of the product: each end user's own accounts, through managed OAuth.",
            actions="Yes: actions in 1,000+ apps, plus realtime and polling triggers.",
            answer="Composio tool results. Proxy Execute calls provider APIs directly.",
            mcp="A remote MCP server and one-click plugins for ChatGPT, Claude, Claude Code and others.",
            api_cli="A REST API, TypeScript and Python SDKs, and a CLI.",
            own_keys="Yes: your own OAuth app or API keys.",
            open_source="SDKs under MIT. Platform self-host on Enterprise.",
            price_before="Instant Tools report each charge in USD.",
            caps="Spend caps listed on Pro. Their Instant Tools docs say that spend cannot be capped by "
                 "amount.",
            logs="A usage summary API.",
            team="3 members on Hobby, unlimited on Pro; SSO and SCIM on Enterprise; SOC 2 Type II.",
        ),
        "better": [
            ("App actions.", "1,000+ apps where the agent sends email, updates a CRM or posts in Slack, in "
             "each user's own account."),
            ("Triggers.", "Realtime and polling triggers let agents react to events in those apps."),
            ("Multi-user products.", "Per-user connected accounts, a hosted Connect Link and white-labeled "
             "OAuth."),
            ("Cheap volume.", "100,000 free calls a month, then $0.0003 per call on your own apps."),
            ("Compliance.", "SOC 2 Type II, with BAA, zero data retention and IP allowlists as options."),
        ],
        "different": [
            ("Paid data, no vendor accounts.", "{ENDPOINTS} tools across {PROVIDERS} providers: SEO and SERP "
             "data, backlinks, people and company enrichment, social, ad libraries and image, video and "
             "voice generation, on treg's accounts."),
            ("0% markup, in the terms.", "Each call is billed at the provider's own rate from a prepaid "
             "balance. No plan, no seats and no top-up fee."),
            ("Your own keys win.", "Register a key and treg uses it instead of its own, never metered."),
            ("Your accounts, never metered.", "Google Analytics, Search Console, Google Ads, Meta Ads, "
             "TikTok Ads, LinkedIn, Slack, TikTok and YouTube, on the same token as the catalog."),
            ("Self-host for your team.", "treg's code is on GitHub, and you can run it for your own team."),
        ],
        "both": "Many agents need both jobs. Composio can send the email, update the CRM and listen for "
                "events; treg can find the keyword volume, the work email and the competitor's ads. Both "
                "are MCP servers, so one agent can hold both.",
        "faq": [
            ("Is there a Composio alternative for AI agents?",
                 "It depends on the job. Composio connects an agent to 1,000+ apps in each user's own "
                 "accounts, with managed OAuth and triggers. treg gives an agent paid data from "
                 "providers you have no account with, billed per call at each provider's own rate."),
            ("How much does Composio cost?",
                 "Composio pricing starts with the Hobby plan at $0 and 100,000 tool calls a month; "
                 "Pro is $29 a month with usage after that, and Enterprise is custom. The Composio "
                 "Hobby plan needs no card and includes $2 of Instant Tools credit."),
            ("Can I get SEO or enrichment data from Composio without a vendor account?",
                 "Partly. Composio's Instant Tools, off by default, run some search, scraping and maps "
                 "tools on Composio's accounts; Composio does not list SEO metrics, enrichment or ad "
                 "data there. treg's catalog has SEO and SERP data, backlinks, people and company "
                 "enrichment and ad libraries on treg's accounts."),
            ("Composio vs treg: which is cheaper for an AI agent?",
                 "It depends on the job. For app calls, Composio charges $0.0003 per call on your own "
                 "apps after the free allowance, and $0.0005 to $0.01 on managed apps. For paid data, "
                 "treg bills each call at the provider's own rate with 0% markup and no plan; "
                 "Composio's Instant Tools add the tool-call charge."),
            ("Can I use Composio and treg together?",
                 "Yes. Composio and treg are both MCP servers, so one agent can hold both. Composio "
                 "can send the email, update the CRM and listen for events; treg can find the keyword "
                 "volume, the work email and the competitor's ads."),
            ("Can I use my own API keys with treg?",
                 "Yes. treg uses your team's own key for a provider instead of its own and never "
                 "meters it. Your connected Google Analytics, Search Console, Google Ads and Meta Ads "
                 "accounts on treg are never metered either. Composio also takes your own OAuth app or "
                 "API keys."),
            ("Can I self-host treg?",
                 "Yes. treg's code is on GitHub, and you can self-host treg for your own team. "
                 "Composio's platform self-hosts on the Enterprise plan, and Composio's SDKs are MIT."),
        ],
        "sources": [
            ("Composio home page", "https://composio.dev"),
            ("Composio pricing", "https://composio.dev/pricing"),
            ("Authentication", "https://docs.composio.dev/docs/authentication"),
            ("Instant Tools", "https://docs.composio.dev/docs/instant-tools"),
            ("Composio on GitHub", "https://github.com/ComposioHQ/composio"),
        ],
    },
    "zapier": {
        "name": "Zapier",
        "short": "Zapier MCP",
        "site": "https://zapier.com/mcp",
        "group": "apps",
        "mono": "#FF4F00",
        "logo": "/media/compare/logos/zapier.png",
        "logo_fills": True,
        "title": "treg vs Zapier MCP: data tools or app actions for agents",
        "description": "Zapier MCP lets an agent act in 9,000+ apps you connect. treg gives an agent "
                       "paid data per call in dollars at 0% markup.",
        "sub": "Dollars per call, not tasks, for data with no vendor accounts. Zapier MCP lets an "
               "agent act in the apps you connect.",
        "their_words": "Go from AI chat to AI action.",
        "tldr": "Both connect an agent through a remote MCP server. treg is like OpenRouter for tools: "
                "one API key and one balance for many paid APIs. treg gives your agent paid data from "
                "{ENDPOINTS} tools across {PROVIDERS} providers with no vendor accounts, each call "
                "priced in dollars at 0% markup, and your own keys and connected accounts are never "
                "metered. Zapier MCP lets your agent take actions in the 9,000+ apps you connect to "
                "Zapier, with each successful call costing 2 tasks from your plan. Teams that need "
                "both jobs often use both.",
        "spec": {"billing": "Tasks from a plan", "start": "100 tasks a month",
                 "keys": "Your connected apps", "code": "Hosted only"},
        "fit_them": [
            "Your agent acts in long-tail SaaS apps among Zapier's 9,000+.",
            "You already use Zapier: MCP draws on the same task pool, with no new contract.",
            "You want Zaps and triggers that run without the agent.",
            "You need SAML SSO, SCIM and audit logs kept up to a year.",
        ],
        "fit_treg": [
            "Your agent needs keyword volume, backlinks, work emails, social profiles or ad libraries, "
            "with no account at each vendor.",
            "You want each call priced in dollars before it runs, at the provider's own rate.",
            "You want Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn and Slack on the "
            "same token, never metered.",
            "You want your own keys and your team's own APIs, CLIs and skills behind one token.",
            "You want to read the code on GitHub or self-host for your team.",
        ],
        "cells": _c(
            how_pay="Plans billed in tasks: Free, Pro from $19.99 a month and Team from $69 a month "
                    "(annual). Each successful MCP tool call is 2 tasks.",
            free_start="Free plan: 100 tasks a month, about 50 MCP tool calls.",
            fees="Failed calls use no tasks. Pay-per-task overage at 1.25x to 2.5x the base rate.",
            catalog="9,000+ apps and tens of thousands of actions.",
            data="Not published. Actions run in apps you connect with your own accounts.",
            own_accounts="Yes, the core of the product: apps connected to your Zapier account.",
            actions="Yes: send email, update a CRM, post to Slack and more, plus Zaps and triggers.",
            answer=NOT_PUBLISHED,
            mcp="A remote MCP server (Streamable HTTP), included on every plan.",
            api_cli="A TypeScript SDK and a CLI. SDK calls count as 1 task from 1 December 2026.",
            own_keys="You connect your own app accounts.",
            open_source="No. Hosted service.",
            price_before="Task use per call is published; the dollar price per call depends on your plan.",
            caps="Your task allowance. With pay-per-task off, usage stops at the limit.",
            logs="A history of MCP actions; audit logs 6 months on Team, 1 year on Enterprise.",
            team="Shared MCP servers on Team and Enterprise; SAML SSO and SCIM; SOC 2 Type II.",
        ),
        "better": [
            ("A very wide app catalog.", "9,000+ apps with tens of thousands of actions."),
            ("Actions in your apps.", "Send the email, update the CRM, post in Slack, in the apps you "
             "connect."),
            ("Automations that run alone.", "Zaps and triggers run without the agent."),
            ("Governance.", "SOC 2 Type II, SAML SSO, SCIM, audit logs up to a year, and access control per "
             "app and action."),
            ("One bill for Zapier customers.", "MCP uses the task pool you already pay for."),
        ],
        "different": [
            ("Paid data with no accounts.", "{ENDPOINTS} tools across {PROVIDERS} providers: keyword volume, "
             "backlinks, SERPs, work emails, social profiles, ad libraries and generation models, on "
             "treg's own accounts."),
            ("Dollars, not tasks.", "Each tool shows its price, with its source and checked date, before the "
             "call. 0% markup, a commitment in the terms."),
            ("Your own keys and tools.", "Your key always wins and is never metered; your team's own APIs, "
             "CLIs and skills sit behind the same token."),
            ("Your accounts, never metered.", "Google Analytics, Search Console, Google Ads, Meta Ads, "
             "TikTok Ads, LinkedIn, Slack, TikTok and YouTube, next to the catalog."),
            ("Public code.", "treg's code is on GitHub, and you can self-host for your team."),
        ],
        "both": "Use Zapier MCP for the actions in your apps and treg for the data. For example, treg finds "
                "the decision maker's work email, and Zapier adds the lead to your CRM.",
        "faq": [
            ("Is there a Zapier MCP alternative for AI agents?",
                 "Yes, for paid data. treg is a Zapier MCP alternative when an agent needs keyword "
                 "volume, backlinks, work emails or ad libraries from vendors you have no account "
                 "with. Zapier MCP's pages describe actions in the apps you connect with your own "
                 "accounts."),
            ("How much does Zapier MCP cost?",
                 "Zapier MCP pricing comes from your Zapier plan: Zapier MCP is included on every "
                 "plan, Free included. The Free plan has 100 tasks a month, Pro starts at $19.99 a "
                 "month and Team at $69 a month (annual), and each successful tool call uses 2 tasks."),
            ("What happens when my Zapier tasks run out?",
                 "It depends on pay-per-task. With pay-per-task off, Zapier stops usage at your task "
                 "limit; with it on, overage costs 1.25x to 2.5x the base rate and pauses at 3x your "
                 "plan. treg bills from a prepaid team balance, with a team daily spend cap and a "
                 "per-call cost ceiling you set."),
            ("Zapier MCP vs treg: what is the difference?",
                 "The job differs. Zapier MCP lets an agent take actions in 9,000+ apps you connect, "
                 "and Zaps and triggers run without the agent. treg gives an agent the catalog's data "
                 "tools, writes in connected accounts such as Google Ads and Slack, and your team's "
                 "own APIs, CLIs and skills behind one token."),
            ("Can I use Zapier MCP and treg together?",
                 "Yes. Use Zapier MCP for the actions in your apps and treg for the data. For example, "
                 "treg finds the decision maker's work email, and Zapier MCP adds the lead to your CRM."),
            ("Can I use my own API keys with treg?",
                 "Yes. treg accepts your own keys on every account, and your team's key always wins "
                 "over treg's and is never metered. Zapier MCP acts through the app accounts connected "
                 "to your Zapier account, and credentials stay in Zapier's managed connection layer."),
            ("Can I self-host treg?",
                 "Yes. treg's code is on GitHub under Apache 2.0 with added terms, and you can "
                 "self-host treg for your own team. Zapier is a hosted service and does not publish a "
                 "self-host option."),
        ],
        "sources": [
            ("Zapier MCP", "https://zapier.com/mcp"),
            ("Zapier pricing", "https://zapier.com/pricing"),
            ("MCP usage", "https://docs.zapier.com/mcp/overview/usage"),
            ("What is Zapier MCP", "https://help.zapier.com/hc/en-us/articles/48308034391821-What-is-Zapier-MCP"),
            ("Zapier SDK", "https://zapier.com/sdk"),
        ],
    },
    "pipedream": {
        "name": "Pipedream",
        "short": "Pipedream Connect",
        "site": "https://pipedream.com",
        "group": "apps",
        "mono": "#22C55E",
        "logo": "/media/compare/logos/pipedream.svg",
        "logo_fills": True,
        "title": "treg vs Pipedream Connect, now part of Workday",
        "description": "Pipedream Connect, now part of Workday, gives the agents you build 10,000+ "
                       "tools. treg gives agents paid data per call at 0% markup.",
        "sub": "Billed in dollars per call, for data you hold no account for. Pipedream Connect lets "
               "agents act in your users' apps.",
        "their_words": "The integration layer for AI agents.",
        "tldr": "Both reach an agent through remote MCP servers and keep credentials out of the model. "
                "treg is like OpenRouter for tools: one API key and one balance for many paid APIs. "
                "treg gives your agent paid data from {ENDPOINTS} tools across {PROVIDERS} providers "
                "with no vendor accounts, billed per call at 0% markup, and your own keys always win. "
                "Pipedream Connect, now part of Workday, gives the agents you build 10,000+ tools "
                "across 3,000+ APIs, acting in each end user's own accounts with managed OAuth. A "
                "product can use both: Pipedream for its users' apps, treg for the data.",
        "note": "Pipedream Workflows and String shut down on 31 March 2027. Pipedream's docs say that "
                "Connect, its API, its MCP servers and its API proxy are not affected.",
        "spec": {"billing": "Plan, users and credits", "start": "Free for development",
                 "keys": "Your users' accounts", "code": "Source-available"},
        "fit_them": [
            "You build a product whose users connect their own apps to your agent.",
            "You want a raw API proxy with managed credentials for any endpoint of a connected app.",
            "You need triggers that react to events in your users' apps.",
            "You need HIPAA, SSO, SCIM or static egress IPs.",
        ],
        "fit_treg": [
            "Your agent needs keyword volume, backlinks, work emails, social profiles or ad libraries, "
            "with no account at each vendor.",
            "You want each call billed in dollars at the provider's own rate, with no per-user fee and no "
            "plan.",
            "You want Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn and Slack on the "
            "same token, never metered.",
            "You want your team's own keys and tools behind one token.",
            "You want code on GitHub you can self-host for your team.",
        ],
        "cells": _c(
            how_pay="Free for development. Startup $99 a month (annual) with 100 external users and 10,000 "
                    "credits, then $2 per user and $0.012 per credit. One credit is 30 seconds of compute.",
            free_start="Free for development and personal MCP use, no card. Production needs a paid plan.",
            fees="MCP tool calls use credits; there is no separate MCP fee.",
            catalog="10,000+ tools and triggers across 3,000+ APIs.",
            data="Not published. Tools act in accounts your users connect.",
            own_accounts="Yes, the core of the product: each end user's own accounts, through managed OAuth "
                         "or your own OAuth client.",
            actions="Yes, plus deployed triggers.",
            answer="Prebuilt tool results, or the raw API through the Connect proxy.",
            mcp="Remote MCP servers at mcp.pipedream.com, one per app or one endpoint for all.",
            api_cli="The Connect API and API proxy, plus TypeScript and browser SDKs.",
            own_keys="Your users connect their own keys or OAuth accounts.",
            open_source="Component code is source-available, with no commercial use. Conduit (early access) "
                        "can be self-hosted.",
            price_before=NOT_PUBLISHED,
            caps=NOT_PUBLISHED,
            logs="An audit log of tool calls in Conduit.",
            team="Unlimited team members; SOC 2 Type 2; SSO and SCIM on Business.",
        ),
        "better": [
            ("Breadth of actions.", "10,000+ prebuilt tools and triggers across 3,000+ APIs."),
            ("Built for your users.", "Per-user accounts, Connect Link, your own OAuth clients and 1M+ "
             "connected accounts."),
            ("A raw API proxy.", "Any endpoint of a connected app, with credentials managed for you."),
            ("Enterprise path.", "HIPAA on Business, SSO, SCIM, static IPs, and Conduit for governed access "
             "across a company."),
        ],
        "different": [
            ("Paid data with no accounts.", "{ENDPOINTS} tools across {PROVIDERS} providers: SEO and SERP "
             "data, enrichment, social, ad libraries and generation, on treg's own accounts."),
            ("Billed per call in dollars.", "0% markup, a commitment in the terms. No per-user fee, no "
             "compute credits and no top-up fee."),
            ("Your own keys win.", "Register a key and treg uses it instead of its own, never metered."),
            ("Spend controls.", "A team daily spend cap, a per-call cost ceiling and per-member daily call "
             "caps, with each tool's price shown before the call."),
            ("Open code.", "treg's code is on GitHub under Apache 2.0 with added terms, and you can run it "
             "for your team."),
        ],
        "both": "A product can use Pipedream Connect for actions in its users' apps and treg for the data "
                "its agent needs from vendors nobody has an account with.",
        "faq": [
            ("Is there a Pipedream alternative for AI agents?",
                 "Yes, for paid data. treg is a Pipedream alternative when an agent needs SEO and SERP "
                 "data, enrichment, social profiles or ad libraries from vendors you have no account "
                 "with. Pipedream Connect gives the agents you build 10,000+ tools that act in each "
                 "end user's own accounts."),
            ("How much does Pipedream Connect cost?",
                 "Pipedream Connect pricing is free for development; Startup costs $99 a month "
                 "(annual) with 100 external users and 10,000 credits, then $2 per user and $0.012 per "
                 "credit. One credit is 30 seconds of compute. treg bills each catalog call at the "
                 "provider's own rate, with 0% markup and no per-user fee."),
            ("Did the Workday acquisition change Pipedream Connect?",
                 "No, according to Pipedream's docs. Pipedream says Pipedream Connect, its API, MCP "
                 "servers and API proxy are not affected, while Pipedream Workflows and String shut "
                 "down on 31 March 2027. treg's code is on GitHub under Apache 2.0 with added terms, "
                 "and you can self-host treg for your own team."),
            ("Pipedream vs treg: what is the difference?",
                 "Pipedream Connect runs tools in the accounts your product's users connect, while "
                 "treg runs catalog calls on treg's own accounts for your agent. Pipedream adds "
                 "triggers, a raw API proxy and per-user managed OAuth. treg adds a team daily spend "
                 "cap, a per-call cost ceiling and each tool's price before the call."),
            ("Can I use Pipedream and treg together?",
                 "Yes. A product can use Pipedream Connect for actions in its users' apps and treg for "
                 "the data its agent needs from vendors nobody has an account with. Both Pipedream and "
                 "treg reach an agent over remote MCP."),
            ("Can I use my own API keys with treg?",
                 "Yes. treg accepts your own keys on every account, and your team's key always wins "
                 "over treg's and is never metered. On Pipedream Connect, your users connect their own "
                 "keys or OAuth accounts."),
            ("Does treg have an MCP server?",
                 "Yes. treg is a remote MCP server with OAuth or a token header, beside an HTTP API, a "
                 "CLI and an agent skill. Pipedream runs remote MCP servers at mcp.pipedream.com, one "
                 "per app or one endpoint for all tools."),
        ],
        "sources": [
            ("Pipedream home page", "https://pipedream.com"),
            ("Pipedream pricing", "https://pipedream.com/pricing"),
            ("Pipedream Connect", "https://pipedream.com/docs/connect"),
            ("Pipedream MCP", "https://pipedream.com/docs/connect/mcp"),
            ("Workflows shutdown notice", "https://pipedream.com/docs/workflows"),
            ("Workday announcement", "https://newsroom.workday.com/2025-11-19-Workday-Signs-Definitive-Agreement-to-Acquire-Pipedream"),
        ],
    },
    "smithery": {
        "name": "Smithery",
        "site": "https://smithery.ai",
        "group": "apps",
        "mono": "#EA580C",
        "logo": "/media/compare/logos/smithery.png",
        "title": "treg vs Smithery: an MCP directory vs a hosted MCP server",
        "description": "Smithery is now a free MCP directory; its hosted proxy shut down on 31 October "
                       "2026. treg is one token for paid tools, at 0% markup.",
        "sub": "One token for paid tools from many vendors. Smithery is now a free directory of MCP "
               "servers that links to each vendor.",
        "their_words": "Connect agents to thousands of tools and services.",
        "tldr": "Both help an agent reach many tools. treg is like OpenRouter for tools: one API key "
                "and one balance for many paid APIs. treg is one token for {ENDPOINTS} tools across "
                "{PROVIDERS} providers, with treg holding the provider accounts, each call billed at "
                "0% markup from one prepaid balance, and your own keys always winning. Since 31 "
                "October 2026, Smithery is a free public directory of MCP servers, run by Arcade.dev, "
                "where each server is reached directly at its vendor with your own account there. Use "
                "Smithery to find a vendor's official server, and treg for paid data through one "
                "connection.",
        "note": "Arcade.dev acquired Smithery in August 2026. Smithery's hosted URLs, proxy, managed "
                "identity, CLI, SDKs, accounts and API keys stopped working on 31 October 2026.",
        "spec": {"billing": "Free directory", "start": "Free, no account",
                 "keys": "Your account at each vendor", "code": "No public repos"},
        "fit_them": [
            "You want to browse MCP servers for free, with no account.",
            "You want documentation search, developer tools and niche public-data servers.",
            "You want each vendor's official server, with its full set of actions.",
            "You want nothing between your agent and the vendor.",
        ],
        "fit_treg": [
            "You want one token for keyword volume, backlinks, work emails, social profiles, ad libraries "
            "and generation, with no account at each vendor.",
            "You want one prepaid balance, billed per call at each provider's own rate with 0% markup.",
            "You want your own keys and your team's own APIs, CLIs and skills behind that token.",
            "You want Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn and Slack on the "
            "same token, never metered.",
            "You used Smithery-hosted URLs and want one hosted connection for many tools.",
        ],
        "cells": _c(
            how_pay="Free to browse. Paid plans ended, and paying users were refunded charges since "
                    "5 August 2026.",
            free_start="Free, with no account needed to browse.",
            fees="None. Smithery no longer proxies or bills calls; you pay each vendor.",
            catalog="214 servers on the directory today.",
            data="No. You connect to each vendor's server with your own account.",
            own_accounts="Through each vendor's own server.",
            actions="Whatever each vendor's server offers.",
            answer="Direct from each vendor's server.",
            mcp="Hosted Smithery URLs stopped working on 31 October 2026. Verified entries link to the "
                "vendor's own MCP URL.",
            api_cli="The CLI, SDKs and API keys stopped working on 31 October 2026.",
            own_keys="Yes: you need your own account at each vendor.",
            open_source="No public repositories.",
            price_before="Not applicable.",
            caps="Not applicable.",
            logs=NOT_PUBLISHED,
            team=NOT_PUBLISHED,
        ),
        "better": [
            ("Free discovery.", "Browsing costs nothing and needs no account."),
            ("Breadth of kinds.", "Documentation search, developer tools and niche public-data servers."),
            ("Official servers.", "Verified entries point to the vendor's own MCP endpoint, often with its "
             "full write surface."),
            ("No middle hop.", "Your agent talks to the vendor directly."),
        ],
        "different": [
            ("One token, many vendors.", "{ENDPOINTS} tools across {PROVIDERS} providers on treg's accounts. "
             "Your agent does not sign up at each vendor."),
            ("One bill at 0% markup.", "Each catalog call is billed at the provider's own rate from one "
             "prepaid balance, with its price, source and checked date shown before the call."),
            ("Your keys and tools.", "Your own keys win and are never metered, and your team's own APIs, CLIs "
             "and skills sit behind the same token."),
            ("Spend controls.", "A team daily spend cap, a per-call cost ceiling and per-member daily call "
             "caps."),
            ("Hosted and running.", "One remote MCP server with OAuth, an HTTP API, a CLI and an agent "
             "skill, with the code on GitHub."),
        ],
        "both": "Smithery's directory is a good place to find a vendor's official MCP server. When your "
                "agent needs paid data from many vendors through one connection, treg does that job.",
        "faq": [
            ("What is a good Smithery alternative for hosted MCP tools?",
                 "It depends on the job. treg is a Smithery alternative when an agent needs paid data "
                 "from many providers through one token, with treg holding the provider accounts. "
                 "Smithery points its users to Arcade.dev for hosted tools, or to each vendor's own "
                 "MCP URL."),
            ("How much does Smithery cost?",
                 "Smithery pricing is free: Smithery is free to browse, with no account needed. "
                 "Smithery's paid plans ended, and paying users were refunded charges since 5 August "
                 "2026. treg bills each catalog call at the provider's own rate from a prepaid "
                 "balance, with 0% markup."),
            ("Is Smithery shutting down?",
                 "Partly. Smithery's MCP proxy, managed identity, hosted URLs, CLI, SDKs, accounts and "
                 "API keys stopped working on 31 October 2026, after Arcade.dev acquired Smithery in "
                 "August 2026. Smithery remains a public MCP directory that does not proxy calls. treg "
                 "is a hosted option: a remote MCP server with OAuth, an HTTP API and a CLI."),
            ("Smithery vs treg: what is the difference?",
                 "They do different jobs. Smithery is a free directory of 214 MCP servers, where "
                 "verified entries link to the vendor and you use your own account there. treg is one "
                 "token for a catalog of paid tools plus your team's own keys, APIs, CLIs and skills, "
                 "with a team daily spend cap and a per-call cost ceiling."),
            ("Can I use Smithery and treg together?",
                 "Yes. Smithery's directory is a good place to find a vendor's official MCP server. "
                 "When your agent needs paid data from many vendors through one connection, treg does "
                 "that job."),
            ("Can I self-host treg?",
                 "Yes. treg's code is on GitHub under Apache 2.0 with added terms, and you can "
                 "self-host treg for your own team. Smithery does not publish a self-host option, and "
                 "Smithery's GitHub organization has no public repositories."),
        ],
        "sources": [
            ("Smithery directory", "https://smithery.ai/servers"),
            ("The future of Smithery", "https://smithery.ai/future"),
            ("Smithery joins Arcade", "https://www.arcade.dev/blog/smithery-joins-arcade"),
            ("Arcade pricing", "https://www.arcade.dev/pricing"),
        ],
    },
    "arcade": {
        "checked": "2026-11-05",
        "name": "Arcade.dev",
        "short": "Arcade.dev",
        "site": "https://www.arcade.dev",
        "group": "apps",
        "mono": "#C3FF00",
        "logo": "/media/compare/logos/arcade.svg",
        "logo_fills": True,
        "title": "treg vs Arcade.dev: an Arcade.dev alternative for AI agents",
        "description": "Arcade.dev gives the agents you build 8,000+ tools that act in each user's own "
                       "apps. treg gives agents paid data per call at 0% markup.",
        "sub": "Spend limits in dollars and paid data with no vendor accounts. Arcade.dev lets your "
               "agents act in each user's apps.",
        "their_words": "Arcade is the actions runtime between your agents and every system they reach.",
        "tldr": "Both reach an agent over MCP, and both accept your own keys. treg is like OpenRouter "
                "for tools: one API key and one balance for many paid APIs. treg gives your agent paid "
                "data from many providers (SEO, enrichment, social, ads and generation) on treg's "
                "accounts, at 0% markup, and your own keys and connected accounts are never metered. "
                "Arcade gives the agents you build 8,000+ tools that act in each user's own Gmail, "
                "Slack, Salesforce or GitHub, with sign-in through your identity provider and policies "
                "checked before each action. Many agents use both.",
        "spec": {"billing": "$25 a month, then $0.01 a call", "start": "2,000 calls a month free",
                 "keys": "Yes", "code": "Framework MIT"},
        "fit_them": [
            "Your agents act inside your users' apps, each user with their own delegated access.",
            "Your security team needs sign-in through your identity provider and policies on every action.",
            "You want to run the platform in your own VPC, air-gapped, or on your own Kubernetes.",
            "You build your own tools with an MIT framework and SDKs in Python, TypeScript, Go and Java.",
            "You want 2,000 free tool calls a month to start, with no card.",
        ],
        "fit_treg": [
            "Your agent needs data you have no account for: SEO and SERP, people and company enrichment, "
            "social, ad libraries, or image, video and voice generation.",
            "You want each call billed at the provider's own rate, with 0% markup, no top-up fee and no "
            "monthly fee.",
            "You want dollar limits: a team daily spend cap, a per-call cost ceiling and per-member call caps.",
            "You want your own provider keys to win, and your Google Analytics, Search Console, Google Ads "
            "and Meta Ads calls never metered.",
            "You want each tool's price, with its source and checked date, before the call.",
        ],
        "cells": _c(
            how_pay="Plans: Free $0, Team $25 a month plus $0.01 per tool call, Enterprise custom with "
                    "annual bundles of tool calls.",
            free_start="Free plan, no card, with 2,000 tool calls a month.",
            fees="One rate for every tool call on Team: $0.01. Discounted rates on Enterprise.",
            catalog="8,000+ tools (home page); 7,500+ tools across 81 MCP servers (docs).",
            data="Not published. Its SerpApi and Firecrawl toolkits ask for your own API key.",
            own_accounts="Yes, the core of the product: each user's own accounts, with delegated "
                         "authorization.",
            actions="Yes: actions in Gmail, Slack, Salesforce, GitHub and many other apps. App "
                    "triggers are not published.",
            answer="Arcade's own tool results. It can also give you the user's token to call the API "
                   "yourself.",
            mcp="MCP gateways for ChatGPT, Claude, Claude Code, Cursor, VS Code and Copilot, plus a "
                "hosted MCP server and agent plugins.",
            api_cli="A REST API, clients for Python, TypeScript, Go and Java, and a CLI to build and "
                    "deploy tools.",
            own_keys="Yes: your own OAuth apps, which Arcade recommends for production, and your own "
                     "API keys as secrets.",
            open_source="The MCP framework and Python client are MIT. The platform self-hosts with "
                        "Helm; VPC and air-gapped deployment are on Enterprise.",
            price_before="A flat $0.01 per tool call on Team.",
            caps="Rate limits by call count per tool, toolkit or all tools, per second up to per "
                 "month. Dollar caps are not published.",
            logs="A record of every tool run (inputs and outputs held to project admins) and an "
                 "audit log of admin changes, with a REST API.",
            team="Shared project keys; SSO and RBAC on Enterprise; SOC 2 compliant.",
        ),
        "better": [
            ("App actions for each user.", "Thousands of tools that act in each user's own Gmail, Slack, "
             "Salesforce or GitHub, with delegated access."),
            ("Governance before each action.", "Sign-in through your identity provider, your policies "
             "checked before a tool runs, and rate limits per tool."),
            ("Deployment choice.", "Arcade Cloud, your VPC, air-gapped, Helm on your own Kubernetes, or "
             "a managed app in your own Azure account."),
            ("Building your own tools.", "An MIT framework for MCP servers and clients for Python, "
             "TypeScript, Go and Java."),
            ("A free plan with volume.", "2,000 tool calls a month with no card."),
        ],
        "different": [
            ("Paid data, no vendor accounts.", "{ENDPOINTS} tools across {PROVIDERS} providers: SEO and "
             "SERP data, backlinks, people and company enrichment, social, ad libraries and generation "
             "models, all on treg's accounts."),
            ("0% markup, no plan.", "A prepaid balance billed at each provider's own rate. 0% markup is a "
             "commitment in the terms, with no top-up fee and no monthly fee."),
            ("Your keys and accounts, never metered.", "Your team's own key always wins over treg's. Calls "
             "on your connected Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn, Slack "
             "and YouTube accounts are never metered."),
            ("Dollar limits.", "A team daily spend cap, a per-call cost ceiling and per-member daily call "
             "caps keep an agent's spend in bounds."),
            ("Price before the call.", "Each tool's rate, with its source and the date it was checked, "
             "before the call runs."),
        ],
        "both": "Many agents need both jobs. Arcade can send the email, update the CRM and open the "
                "ticket as the user; treg can find the keyword volume, the work email and the "
                "competitor's ads. Both are MCP servers, so one agent can hold both.",
        "faq": [
            ("Is there an Arcade.dev alternative for AI agents?",
                 "Yes, for paid data. treg is an Arcade.dev alternative when an agent needs data you "
                 "have no account for: SEO and SERP data, enrichment, social, ad libraries, or image, "
                 "video and voice generation. Arcade.dev gives the agents you build 8,000+ tools that "
                 "act in each user's own Gmail, Slack, Salesforce or GitHub."),
            ("How much does Arcade.dev cost?",
                 "Arcade.dev pricing starts with a Free plan of 2,000 tool calls a month with no card. "
                 "The Arcade.dev Team plan is $25 a month plus $0.01 per tool call, and Enterprise is "
                 "custom, with annual bundles of tool calls. treg bills each catalog call at the "
                 "provider's own rate, with 0% markup and no monthly fee."),
            ("Can I self-host Arcade.dev?",
                 "Yes. Arcade self-hosts with a Helm chart on your own Kubernetes or runs as a managed "
                 "app in your Azure account, and Arcade's pricing lists VPC and air-gapped deployment "
                 "on Enterprise. treg's code is on GitHub, and you can self-host treg for your own "
                 "team."),
            ("Arcade.dev vs treg: what is the difference?",
                 "Arcade.dev acts in each user's own apps, with sign-in through your identity provider "
                 "and policies checked before each action, while treg runs paid data calls on treg's "
                 "own accounts. Arcade's SerpApi and Firecrawl toolkits ask for your own key with that "
                 "vendor. treg runs search and scraping calls on treg's accounts."),
            ("Can I use Arcade.dev and treg together?",
                 "Yes. Arcade.dev can send the email, update the CRM and open the ticket as the user, "
                 "and treg can find the keyword volume, the work email and the competitor's ads. "
                 "Arcade.dev and treg are both MCP servers, so one agent can hold both."),
            ("Can I set a dollar spending limit with treg?",
                 "Yes. treg has a team daily spend cap, a per-call cost ceiling and per-member daily "
                 "call caps, and shows each tool's rate before the call. Arcade.dev does not publish "
                 "dollar spend caps; its rate limits cap the number of calls per tool, toolkit or all "
                 "tools."),
            ("Can I use my own API keys with treg?",
                 "Yes. treg accepts your own keys on every account, your team's key always wins over "
                 "treg's, and calls on your own keys and connected accounts are never metered. "
                 "Arcade.dev accepts your own OAuth apps, which Arcade recommends for production, and "
                 "your own API keys as secrets."),
        ],
        "sources": [
            ("Arcade home page", "https://www.arcade.dev"),
            ("Arcade pricing", "https://www.arcade.dev/pricing"),
            ("Auth providers", "https://docs.arcade.dev/en/references/auth-providers"),
            ("Hosting options", "https://docs.arcade.dev/en/operate/deploy"),
            ("Rate limiting", "https://docs.arcade.dev/en/operate/governance/contextual-access/rate-limiting"),
            ("Tool executions", "https://docs.arcade.dev/en/operate/governance/tool-executions"),
            ("Firecrawl toolkit", "https://docs.arcade.dev/en/resources/integrations/development/firecrawl"),
            ("Arcade MCP on GitHub", "https://github.com/ArcadeAI/arcade-mcp"),
        ],
    },
    "nango": {
        "checked": "2026-11-05",
        "name": "Nango",
        "site": "https://www.nango.dev",
        "group": "apps",
        "mono": "#29ABE2",
        "logo": "/media/compare/logos/nango.svg",
        "logo_fills": True,
        "title": "treg vs Nango: a Nango alternative for AI agents",
        "description": "Need a Nango alternative? Nango connects your product to APIs in each user's "
                       "own accounts; treg gives agents paid data at 0% markup.",
        "sub": "Per-call data with no plan and no vendor accounts. Nango connects your product to APIs "
               "in your users' own accounts.",
        "their_words": "Integrations for your products & agents.",
        "tldr": "Both reach an agent over MCP. treg is like OpenRouter for tools: one API key and one "
                "balance for many paid APIs. treg gives your agent paid data from many providers on "
                "treg's accounts, billed per call at 0% markup, and your own keys and connected "
                "accounts are never metered. Nango connects the product and agents you build to 1,000+ "
                "APIs in each user's own accounts, with managed OAuth, TypeScript functions, syncs and "
                "webhooks. A product can use both: Nango for its users' apps, treg for the data its "
                "agent needs.",
        "spec": {"billing": "Connections, compute and data", "start": "Free plan, hard-capped",
                 "keys": "Your users' accounts", "code": "Elastic License 2.0"},
        "fit_them": [
            "You build a product whose users connect their own apps, and your agent acts in them.",
            "You want integrations as code you can generate, review, test and deploy.",
            "You need syncs, webhooks and triggers, not only on-demand calls.",
            "You want a raw proxy to any endpoint of a connected API.",
            "You need SOC 2 Type II, HIPAA with a BAA, or Nango in your own cloud.",
        ],
        "fit_treg": [
            "Your agent needs data you have no account for: SEO and SERP, people and company enrichment, "
            "social, ad libraries, or image, video and voice generation.",
            "You want each call billed in dollars at the provider's own rate, with 0% markup.",
            "You want a team daily spend cap, a per-call cost ceiling and per-member call caps.",
            "You want your own provider keys to win, and your Google Analytics, Search Console, Google Ads "
            "and Meta Ads calls never metered.",
            "You want to self-host for your own team, with the code on GitHub.",
        ],
        "cells": _c(
            how_pay="Plans: Free $0, Pay-as-you-go $50 a month with $50 of credit, Enterprise custom. "
                    "Usage is $0.29 per connection a month, $0.72 per compute hour and $0.50 per GB.",
            free_start="Free plan with hard caps each month: 10 connections, 10 compute hours and 10 GB.",
            fees="Billed by connection, compute time (each run rounded up to 1 second) and data sent. "
                 "No per-call fee.",
            catalog="1,000+ APIs, with 10,000+ pre-built tools, triggers and syncs.",
            data="Not published. Calls run on the accounts your users connect.",
            own_accounts="Yes, the core of the product: each end user's own accounts, through "
                         "Nango-managed OAuth apps or your own.",
            actions="Yes: action functions, plus syncs, webhooks and triggers.",
            answer="Your action function's output (2 MB at most), or the provider's response through "
                   "the proxy.",
            mcp="An MCP server per agent session, scoped to chosen connections and actions, plus a "
                "management MCP.",
            api_cli="A REST API with a proxy, backend SDKs for Node, Python, Java, Ruby, Go, Rust and "
                    "PHP, and a CLI to build and deploy functions.",
            own_keys="Your users connect their own keys or OAuth accounts. You can use your own OAuth "
                     "app instead of Nango's.",
            open_source="Code on GitHub under the Elastic License 2.0. Free self-host is limited to auth "
                        "and proxy; full self-host and BYOC are on Enterprise.",
            price_before="Rates are published. A call's cost depends on how long it runs.",
            caps="The Free plan is hard-capped. Spend alerts on paid plans. Dollar caps are not "
                 "published.",
            logs="Built-in logs, metrics and alerts, kept 15 days, with OpenTelemetry export.",
            team="RBAC, SAML SSO, SCIM and an audit trail; SOC 2 Type II; HIPAA with a BAA.",
        ),
        "better": [
            ("Breadth of app integrations.", "1,000+ APIs and 10,000+ pre-built tools, triggers and syncs, "
             "in each user's own account."),
            ("Syncs and webhooks.", "Keep external data fresh with polling or real-time syncs, and react "
             "to provider webhooks."),
            ("Integrations as code.", "TypeScript functions you can generate with a coding agent, test "
             "locally and deploy through CI/CD."),
            ("A raw proxy.", "Authenticated requests to any endpoint of a connected API."),
            ("Compliance.", "SOC 2 Type II, HIPAA with a BAA, SSO and SCIM, and BYOC in your own AWS, GCP "
             "or Azure account."),
        ],
        "different": [
            ("Paid data, no vendor accounts.", "{ENDPOINTS} tools across {PROVIDERS} providers: SEO and "
             "SERP data, backlinks, people and company enrichment, social, ad libraries and generation "
             "models, all on treg's accounts."),
            ("Billed per call, 0% markup.", "Each call at the provider's own rate, with no connection fee, "
             "no compute time and no plan. 0% markup is a commitment in the terms."),
            ("Your keys and accounts, never metered.", "Your team's own key always wins over treg's. Calls "
             "on your connected Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn, Slack "
             "and YouTube accounts are never metered."),
            ("Dollar limits, prices first.", "A team daily spend cap, a per-call cost ceiling and "
             "per-member call caps, and each tool's rate with its source and checked date before the call."),
            ("Self-host for your team.", "treg's code is on GitHub under Apache 2.0 with added terms, and "
             "you can run it for your own team."),
        ],
        "both": "A product can use Nango for auth, syncs and actions in its users' apps, and treg for the "
                "data its agent needs from vendors nobody has an account with. Both reach an agent over "
                "MCP.",
        "faq": [
            ("Is there a Nango alternative for AI agents?",
                 "It depends on the job: treg is a Nango alternative when your agent needs paid data, "
                 "and Nango fits when your product acts in each user's own apps. treg gives an agent "
                 "SEO and SERP data, people and company enrichment, social, ad libraries and image, "
                 "video and voice generation, with no vendor accounts."),
            ("How much does Nango cost?",
                 "Nango pricing has a hard-capped Free plan and a Pay-as-you-go plan at $50 a month "
                 "with $50 of credit, while treg has no plan and bills each catalog call at the "
                 "provider's own rate with 0% markup. Nango charges $0.29 per connection a month, "
                 "$0.72 per compute hour and $0.50 per GB sent."),
            ("Is Nango open source, and can I self-host Nango for free?",
                 "Partly: free self-hosted Nango is for hobby projects and is limited to auth and "
                 "proxy, while treg's code is on GitHub and you can self-host treg for your own team. "
                 "Nango's code is under the Elastic License 2.0, and full self-hosting and BYOC are on "
                 "the Nango Enterprise plan."),
            ("Nango vs treg: what is the difference?",
                 "Nango connects the product and agents you build to 1,000+ APIs in each user's own "
                 "accounts, while treg gives your agent paid tools from many providers on treg's "
                 "accounts. Nango adds managed OAuth, TypeScript functions, syncs and webhooks. treg "
                 "adds a team daily spend cap, a per-call cost ceiling, per-member call caps and each "
                 "tool's rate before the call."),
            ("Can I use Nango and treg together?",
                 "Yes. A product can use Nango for auth, syncs and actions in its users' apps, and "
                 "treg for the data its agent needs from vendors nobody has an account with. Nango and "
                 "treg both reach an agent over MCP."),
            ("Can I use my own API keys with treg?",
                 "Yes. treg accepts your own keys on every account, your team's key always wins over "
                 "treg's, and calls on your connected Google Analytics, Search Console, Google Ads and "
                 "Meta Ads accounts are never metered. On Nango, your users connect their own keys or "
                 "OAuth accounts."),
            ("Does treg have an MCP server?",
                 "Yes. treg is a remote MCP server with OAuth or a token header, for Claude, ChatGPT, "
                 "Claude Code, Codex, Cursor and others. Nango gives each agent session its own MCP "
                 "URL and token, scoped to the connections and actions your backend names."),
        ],
        "sources": [
            ("Nango home page", "https://www.nango.dev"),
            ("Nango pricing", "https://www.nango.dev/pricing"),
            ("Introduction", "https://nango.dev/docs/getting-started/intro-to-nango"),
            ("Auth guide", "https://nango.dev/docs/guides/auth/auth-guide"),
            ("Agent sessions", "https://nango.dev/docs/guides/agent-sessions"),
            ("Self-host Nango", "https://nango.dev/docs/guides/platform/self-hosting/self-hosting"),
            ("Limits", "https://nango.dev/docs/guides/platform/limits"),
            ("Nango on GitHub", "https://github.com/NangoHQ/nango"),
        ],
    },
    "clay": {
        "name": "Clay",
        "site": "https://www.clay.com",
        "group": "gtm",
        "mono": "#5B4CF0",
        "logo": "/media/compare/logos/clay.png",
        "title": "treg vs Clay: a Clay alternative with no plan or seats",
        "description": "Looking for a Clay alternative for AI agents? treg gives your agent enrichment "
                       "and other data APIs per call at 0% markup, no seats.",
        "sub": "Enrichment for your agent per call, with no plan and no seats. Clay gives GTM teams "
               "tables, waterfalls and CRM sync.",
        "their_words": "Build systems to grow revenue.",
        "tldr": "Both give you people and company data from many vendors under one account, and both "
                "accept your own keys. treg is like OpenRouter for tools: one API key and one balance "
                "for many paid APIs. treg gives your agent that data beside SEO, social and ad tools, "
                "by MCP, API or CLI, billed per call at the provider's rate with 0% markup and no "
                "seats, and a call on your own key is never metered. Clay gives a GTM team tables with "
                "waterfall enrichment, AI research agents, CRM sync and a sequencer, billed in Actions "
                "and Data Credits on a plan. A RevOps team can keep Clay's tables and give its agents "
                "treg.",
        "spec": {"billing": "Plans, Actions and Data Credits", "start": "500 Actions, 100 credits",
                 "keys": "Yes, still uses Actions", "code": "Hosted only"},
        "fit_them": [
            "Your team builds and fixes enrichment in a spreadsheet interface, row by row.",
            "You want waterfalls built in, with no charge when nothing is found.",
            "You want Claygent and Sculptor for AI research and no-code workflows.",
            "You want CRM and data warehouse sync, a sequencer and ad audiences in one product.",
        ],
        "fit_treg": [
            "Your agent in Claude Code, Codex or Cursor calls enrichment and research APIs itself, by MCP, "
            "API or CLI.",
            "You want no plan and no seats: each call billed at the provider's rate, with 0% markup.",
            "You want people and company data beside SEO, social, ad library and generation tools, behind "
            "one token.",
            "You want your own provider keys to win and cost nothing extra, not even an Action.",
            "You want a team daily spend cap, a per-call cost ceiling and per-member call caps.",
        ],
        "cells": _c(
            how_pay="Plans billed in two meters: Actions (under $0.01 each) and Data Credits (from $0.05 "
                    "each). Launch from $167 a month and Growth from $446 a month on the plan cards.",
            free_start="Free plan: 500 Actions and 100 Data Credits a month, up to 200 rows a table.",
            fees="Top-ups on Launch and Growth cost 30% more. No charge when an enrichment finds nothing.",
            catalog="150+ to 200+ data and AI vendors, depending on the page.",
            data="Yes: people and company enrichment, emails, phones and AI research, on Clay's accounts.",
            own_accounts="CRM and data warehouse sync, an email sequencer and ad audiences.",
            actions="Sequences, CRM updates, audiences, and HTTP API calls on Growth and up.",
            answer="Rows and columns in a Clay table.",
            mcp="Clay MCP for ChatGPT, Claude and Codex, on Launch and up; admins publish Functions.",
            api_cli="HTTP API calls and webhooks from a table on Growth and up; Clay's own API on Enterprise.",
            own_keys="Yes. Your key removes the Data Credit cost; an Action is still used.",
            open_source="No. Hosted service.",
            price_before=NOT_PUBLISHED,
            caps="Per-rep credit limits for MCP; per-workbook spend limits on Enterprise.",
            logs="Not published as a user-facing feature.",
            team="Unlimited seats on every plan. Admin, Editor and Viewer roles; SSO on Enterprise.",
        ),
        "better": [
            ("The table.", "A spreadsheet where people who do not code build, inspect and fix enrichment."),
            ("Waterfalls built in.", "It tries provider after provider until one finds a match, and charges "
             "nothing when none does."),
            ("AI research.", "Claygent and Sculptor run research and workflows over many rows without code."),
            ("The whole GTM loop.", "CRM and warehouse sync, a sequencer and ad audiences in one product."),
            ("Unlimited seats.", "Every plan includes unlimited users."),
        ],
        "different": [
            ("Built for agents.", "Your agent searches the catalog, reads each tool's price with its source "
             "and checked date, and calls the tool, from Claude Code, Codex, Cursor or any MCP client."),
            ("0% markup, no plan, no seats.", "A prepaid balance billed at the provider's own rate. 0% markup "
             "is a commitment in the terms, with no top-up fee."),
            ("More than enrichment.", "{ENDPOINTS} tools across {PROVIDERS} providers: SEO and SERP data, "
             "backlinks, social, ad libraries and generation models sit beside the people data."),
            ("Your keys, never metered.", "Your team's own key always wins over treg's, and treg never "
             "meters a call on it."),
            ("Spreadsheets too.", "The treg add-on for Google Sheets fills cells from the catalog, and shows "
             "each run's cost first."),
        ],
        "both": "A RevOps team can keep Clay for its tables, sequences and CRM sync, and give its agents treg "
                "for ad hoc research, SEO and social data, and quick enrichment outside a table.",
        "faq": [
            ("Is there a Clay alternative for AI agents?",
                 "Yes. treg is a Clay alternative when your agent does the enrichment itself, and Clay "
                 "fits a GTM team that works in tables. treg gives an agent in Claude Code, Codex or "
                 "Cursor enrichment and research APIs beside SEO, social and ad library tools. Clay "
                 "builds waterfalls, Claygent, CRM sync and a sequencer around its tables."),
            ("How much does Clay cost?",
                 "Clay pricing starts with a Free plan of 500 Actions and 100 Data Credits a month; "
                 "Launch is from $167 a month and Growth from $446 a month on Clay's plan cards. The "
                 "FAQ on Clay's pricing page lists $185 and $495, so check Clay's page for the current "
                 "price. treg has no plan and no seats."),
            ("Does Clay have an API?",
                 "Partly. Clay tables can make HTTP API calls and take webhooks on Growth and up, and "
                 "Clay's own API is on Enterprise; Clay does not publish a CLI or SDKs. treg has an "
                 "HTTP API that passes the method, path, query and body to the provider unchanged, "
                 "beside a CLI and an agent skill."),
            ("Clay vs treg: which is cheaper for an AI agent?",
                 "It depends on the job: treg bills one price per call at the provider's own rate with "
                 "0% markup, while Clay bills two meters on a plan. Clay Actions cost under $0.01 "
                 "each, Data Credits start at $0.05 each, and top-ups on Launch and Growth cost 30% "
                 "more. Clay charges nothing when an enrichment finds nothing."),
            ("Can I use Clay and treg together?",
                 "Yes. A RevOps team can keep Clay for its tables, sequences and CRM sync, and give "
                 "its agents treg for ad hoc research, SEO and social data, and quick enrichment "
                 "outside a table."),
            ("Does treg work in Google Sheets?",
                 "Yes. The treg add-on for Google Sheets fills cells from the catalog and shows each "
                 "run's cost first. Clay's table is a spreadsheet of its own, where people who do not "
                 "code build, inspect and fix enrichment."),
            ("Does treg have an MCP server?",
                 "Yes. treg is a remote MCP server with OAuth or a token header, for Claude, ChatGPT, "
                 "Claude Code, Codex, Cursor and others, beside an HTTP API and a CLI. Clay MCP works "
                 "with ChatGPT, Claude and Codex on Launch and up, where admins publish Functions for "
                 "reps."),
        ],
        "sources": [
            ("Clay home page", "https://www.clay.com"),
            ("Clay pricing", "https://www.clay.com/pricing"),
            ("Actions and Data Credits", "https://university.clay.com/docs/credits"),
            ("Clay MCP", "https://www.clay.com/guides/clay-mcp"),
            ("Waterfall enrichment", "https://www.clay.com/waterfall-enrichment"),
        ],
    },
    "apollo": {
        "name": "Apollo.io",
        "short": "Apollo",
        "site": "https://www.apollo.io",
        "group": "gtm",
        "mono": "#1E3A8A",
        "logo": "/logos/apollo.svg",
        "logo_fills": True,
        "title": "treg vs Apollo.io: Apollo MCP, API and pricing compared",
        "description": "Apollo MCP and API reach Apollo's own 240M+ contacts. treg gives an agent "
                       "Apollo and other people data providers per call, no seats.",
        "sub": "Apollo and many other data providers on one token, with no seats. Apollo.io adds its "
               "own database, outreach and a CRM.",
        "their_words": "Grow revenue with one connected GTM system.",
        "tldr": "Both give you people and company data, and Apollo's API is in the treg catalog. treg "
                "is like OpenRouter for tools: one API key and one balance for many paid APIs. treg "
                "lets your agent call Apollo beside many other people and company data providers, plus "
                "SEO, social and ad tools, through one token, per call at the provider's rate with 0% "
                "markup and no seats. Apollo is a full sales platform: its own contact database, "
                "sequences, a dialer and a CRM, on per-seat plans with credits. Keep Apollo for your "
                "reps, connect your Apollo key to treg, and treg never meters your agent's calls on it.",
        "spec": {"billing": "Per seat, with credits", "start": "Free plan, no time limit",
                 "keys": "Your Apollo account", "code": "Hosted only"},
        "fit_them": [
            "Your reps need sequences, a dialer, call recording and a CRM.",
            "You want 240M+ contacts and 30M+ companies with rich filters in one product.",
            "You want a first-party MCP server with about 60 actions, including writes.",
            "You want a free plan with no time limit, and many API endpoints that use no credits.",
        ],
        "fit_treg": [
            "Your agent checks several people and company data providers through one token.",
            "You want no seats: each call billed at the provider's rate, with 0% markup.",
            "You want SEO, social, ad library and generation tools beside the people data.",
            "You already have an Apollo key and want your agent to use it, never metered, beside every "
            "other provider.",
            "You want a team daily spend cap and a per-call cost ceiling on your agent's calls.",
        ],
        "cells": _c(
            how_pay="Per-seat plans with credits that reset each cycle. Apollo's own article (17 September "
                    "2026) lists Basic $49, Professional $79 and Organization $119 per user a month, billed "
                    "annually.",
            free_start="A free plan with no time limit. Search and enrichment by MCP or CLI need a work email.",
            fees="Enrichment costs 1 to 9 credits a person (+8 with a mobile number). Many other API endpoints "
                 "use no credits.",
            catalog="240M+ contacts and 30M+ companies in its own database.",
            data="Its own people and company data, plus waterfall email and phone from other vendors, in "
                 "credits.",
            own_accounts="Mailboxes for sequences; free plans connect Gmail only.",
            actions="About 60 MCP actions, including CRM records, lists, sequences and one-off email.",
            answer="Apollo's own API response.",
            mcp="A first-party remote MCP server with OAuth or an API key.",
            api_cli="A REST API with an OpenAPI spec, and a CLI.",
            own_keys="Not applicable: you use your Apollo account.",
            open_source="No. The CLI's source is on GitHub.",
            price_before="Credit costs per endpoint are published in the API docs.",
            caps="Per-user credit caps set by admins.",
            logs="API usage and credit usage stats.",
            team="Seat-based teams; SSO on Custom plans.",
        ),
        "treg_cells": {
            "catalog": "{ENDPOINTS} tools across {PROVIDERS} providers, including Apollo's people search and "
                       "enrichment and many other people and company data providers.",
        },
        "better": [
            ("Its own database.", "240M+ contacts and 30M+ companies, searchable with rich filters."),
            ("Outreach built in.", "Sequences, a dialer, call recording, tasks and deals, in the same system "
             "as the data."),
            ("Writes by MCP.", "About 60 actions, including CRM records, lists, sequences and one-off email."),
            ("Free endpoints.", "Many Apollo API endpoints use no credits."),
            ("An interface for reps.", "A polished app for people who never touch an agent."),
        ],
        "different": [
            ("Many providers, one token.", "Apollo is one of many people and company data providers among "
             "{ENDPOINTS} tools across {PROVIDERS} providers. Routed tools try the next provider when one "
             "misses an email or phone, and say so."),
            ("No seats, 0% markup.", "A prepaid balance billed per call at the provider's rate. 0% markup is "
             "a commitment in the terms, with no top-up fee."),
            ("More than sales data.", "SEO, social, ad libraries, web scraping and generation sit beside the "
             "people data."),
            ("Your Apollo key, never metered.", "Connect your own Apollo key, and your agent's Apollo calls use "
             "your Apollo credits, with no charge from treg."),
            ("Dollar limits, prices first.", "A team daily spend cap, a per-call cost ceiling and per-member "
             "call caps, and each tool's price with its source and checked date before the call."),
        ],
        "both": "Apollo's API is in the treg catalog. Keep Apollo for your reps' sequences and CRM, connect your "
                "Apollo key to treg, and let your agent use it beside the other data providers.",
        "faq": [
            ("Is there an Apollo.io alternative for AI agents?",
                 "It depends: treg gives your agent Apollo.io's people search and enrichment beside "
                 "many other people and company data providers through one token, while Apollo.io is a "
                 "full sales platform. Apollo.io has its own database of 240M+ contacts and 30M+ "
                 "companies, sequences, a dialer and a CRM for reps."),
            ("How much does Apollo.io cost?",
                 "Apollo.io pricing is a free plan plus per-seat paid plans, while treg has no seats "
                 "and bills each call at the provider's rate with 0% markup. Apollo's own article of "
                 "17 September 2026 lists Basic at $49, Professional at $79 and Organization at $119 "
                 "per user a month, billed annually."),
            ("Does Apollo.io have an MCP server?",
                 "Yes. Apollo.io has a first-party remote MCP server with OAuth or an API key, and "
                 "treg is a remote MCP server with OAuth or a token header. The Apollo.io server lists "
                 "about 60 actions, including CRM records, lists, sequences and one-off email. Free "
                 "Apollo.io accounts on a personal email cannot search or enrich by MCP."),
            ("Does Apollo.io have an API?",
                 "Yes. Apollo.io has a REST API with an OpenAPI spec and a CLI, and Apollo.io's API is "
                 "also in the treg catalog. On 3 November 2026 the Apollo.io pricing page said API "
                 "access is on Custom plans, while Apollo's docs published rate limits for Free "
                 "through Organization, so check with Apollo.io."),
            ("Apollo.io vs treg: which is cheaper for an AI agent?",
                 "It depends on the call: Apollo.io charges 1 to 9 credits to enrich a person, plus 8 "
                 "when a mobile number comes back, while treg bills each call at the provider's rate "
                 "and shows the price before you run it. Many other Apollo.io API endpoints use no "
                 "credits, and Apollo.io credits do not roll over."),
            ("Can I use Apollo.io and treg together?",
                 "Yes. Apollo.io's API is in the treg catalog: keep Apollo.io for your reps' sequences "
                 "and CRM, and connect your Apollo key to treg. Your agent's Apollo calls then use "
                 "your Apollo credits, with no charge from treg, beside the other data providers."),
            ("Does treg try another provider when Apollo.io misses an email?",
                 "Yes, on routed tools. treg's routed tools try the next people data provider when one "
                 "misses an email or phone, and say so in the answer. Apollo.io adds waterfall email "
                 "and phone from other vendors, billed in Apollo credits."),
        ],
        "sources": [
            ("Apollo home page", "https://www.apollo.io"),
            ("Apollo pricing", "https://www.apollo.io/pricing"),
            ("Apollo plan details, by Apollo", "https://www.apollo.io/insights/more-information-on-what-they-are-paying-for-in-apollo-plan-detailsusage-walkthrough"),
            ("API pricing", "https://docs.apollo.io/docs/api-pricing.md"),
            ("Apollo MCP", "https://docs.apollo.io/docs/apollo-mcp.md"),
            ("Rate limits", "https://docs.apollo.io/reference/rate-limits.md"),
        ],
    },
    "openrouter": {
        "name": "OpenRouter",
        "site": "https://openrouter.ai",
        "group": "models",
        "mono": "#6366F1",
        "logo": "/logos/openrouter.svg",
        "logo_fills": True,
        "title": "treg vs OpenRouter API: one routes models, one routes tools",
        "description": "OpenRouter pricing and API: 600+ language models, on credits with a 5.5% "
                       "purchase fee. treg is one token for paid tools at 0% markup.",
        "sub": "OpenRouter's idea, for tools: one key and one balance at 0% markup. OpenRouter does "
               "the same for language models.",
        "their_words": "A unified API for every major LLM.",
        "tldr": "OpenRouter and treg share one idea: one key and one balance in front of many "
                "providers. treg applies it to the tools an agent calls: paid data and generation APIs "
                "and your own connected accounts, with 0% markup and no top-up fee. OpenRouter applies "
                "it to language models, with 600+ models from 80+ providers behind one "
                "OpenAI-compatible API. Put simply, treg is like OpenRouter for external APIs: one API "
                "key and one balance for many paid tools. Use OpenRouter for the model and treg for "
                "the tools the model calls.",
        "spec": {"billing": "Credits, 5.5% purchase fee", "start": "Free models, daily limits",
                 "keys": "Yes, free to $25K a month", "code": NOT_PUBLISHED},
        "fit_them": [
            "You call language models from many providers through one OpenAI-compatible API.",
            "You want automatic routing and fallback across providers for the same model.",
            "You bring your own model keys, with a large fee-free allowance.",
            "You need guardrails, zero data retention, regional routing, SSO or SLAs.",
        ],
        "fit_treg": [
            "Your agent needs data: SEO and SERP, enrichment, social, ad libraries, web scraping.",
            "Your agent makes images, video or voice through the same token.",
            "Your agent works in your own Google, Meta or LinkedIn accounts, never metered.",
            "You want no fee on top-ups and 0% markup on every call.",
            "You want your team's own APIs and CLIs behind one token.",
        ],
        "cells": _c(
            how_pay="Prepaid credits. Inference at each provider's list price.",
            free_start="25+ free models with daily limits, and a small free allowance for new users.",
            fees="A fee on credit purchases: 5.5% on Standard, 8% on Business. Your own keys are free up to "
                 "$25,000 a month, then 5%.",
            x402="Not published. Crypto top-ups in USDC are accepted.",
            catalog="600+ models from 80+ providers.",
            data="No: language-model inference only.",
            own_accounts="Not applicable.",
            actions="Not applicable.",
            answer="OpenAI-compatible responses.",
            mcp="An MCP server that gives coding assistants OpenRouter data and docs; models are not tools "
                "there.",
            api_cli="An OpenAI-compatible REST API, and TypeScript and Python SDKs.",
            own_keys="Yes, with key ordering and fallback to shared capacity.",
            open_source=NOT_PUBLISHED,
            price_before="Yes: per-model prices, and a strict max_price setting.",
            caps="Budgets per key and per member, daily, weekly or monthly.",
            logs="An activity page by model, provider and key.",
            team="Workspaces; SSO and SAML on Enterprise.",
        ),
        "better": [
            ("Language models on one key.", "600+ models from 80+ providers behind one OpenAI-compatible "
             "API."),
            ("Routing for the same model.", "It weighs price, speed and reliability, and falls back when a "
             "provider fails."),
            ("Your own model keys.", "Free up to $25,000 a month of inference, with ordering and fallback."),
            ("Governance.", "Guardrails, zero data retention, regional routing, SSO and SLAs."),
            ("Free models.", "A free tier of models for testing."),
        ],
        "different": [
            ("The same idea, for tools.", "One token for SEO and SERP data, enrichment, social, ad "
             "libraries, scraping, and image, video and voice generation."),
            ("No purchase fee.", "No fee on top-ups and 0% markup on calls, a commitment in treg's terms."),
            ("Your accounts.", "Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn and Slack, "
             "connected by OAuth and never metered."),
            ("Your team's own tools.", "Register an API, a CLI or a skill once, and every agent on the team "
             "calls it with the credential held on the server."),
            ("Public code.", "treg's code is on GitHub, and you can self-host for your team."),
        ],
        "both": "Use OpenRouter for the model and treg for the tools the model calls. treg's catalog also "
                "includes OpenRouter endpoints, such as video generation, through treg's own OpenRouter "
                "account.",
        "faq": [
            ("What is a good OpenRouter alternative for tools and data APIs?",
                 "treg applies OpenRouter's idea to tools: one key and one balance for SEO and SERP "
                 "data, enrichment, social, ad libraries, scraping and image, video and voice "
                 "generation. OpenRouter routes language-model calls to 600+ models from 80+ providers."),
            ("How does OpenRouter pricing work?",
                 "OpenRouter pricing is prepaid credits, with inference billed at each provider's list "
                 "price and a fee when you buy credits: 5.5% on Standard and 8% on Business. "
                 "OpenRouter does not charge model tokens for failed attempts. treg charges no top-up "
                 "fee and 0% markup on calls."),
            ("Can I use my own API keys with OpenRouter?",
                 "Yes. OpenRouter is free on your own keys up to $25,000 a month of inference and then "
                 "charges 5%, and treg never meters a call on your team's own key. OpenRouter tries "
                 "your keys in order and can fall back to shared capacity. On treg, your team's key "
                 "always wins over treg's."),
            ("Can I set a spending limit on OpenRouter and treg?",
                 "Yes, on both. OpenRouter has budgets per key and per member that reset daily, weekly "
                 "or monthly, plus a strict max_price setting. treg has a team daily spend cap, a "
                 "per-call cost ceiling and per-member daily call caps."),
            ("Can I use OpenRouter and treg together?",
                 "Yes. Use OpenRouter for the model and treg for the tools the model calls. treg's "
                 "catalog also includes OpenRouter endpoints, such as video generation, through treg's "
                 "own OpenRouter account."),
            ("Does treg have an MCP server?",
                 "Yes. treg is a remote MCP server whose catalog tools your agent calls, with OAuth or "
                 "a token header, from Claude, ChatGPT, Claude Code, Codex, Cursor and others. The "
                 "OpenRouter MCP server gives coding assistants OpenRouter data and docs, not model "
                 "calls as tools."),
            ("Is treg free to start?",
                 "Yes. treg gives $1.00 of credit once per new verified account, with no card, then "
                 "runs on a prepaid team balance. OpenRouter has 25+ free models with limits of 20 "
                 "requests a minute and 50 a day."),
        ],
        "sources": [
            ("OpenRouter home page", "https://openrouter.ai"),
            ("OpenRouter pricing", "https://openrouter.ai/pricing"),
            ("Bring your own key", "https://openrouter.ai/docs/guides/overview/auth/byok"),
            ("Provider routing", "https://openrouter.ai/docs/guides/routing/provider-selection"),
            ("Guardrails", "https://openrouter.ai/docs/guides/features/guardrails"),
        ],
    },
}


# The day the pages' copy last changed (titles, FAQ, layout). Move it with every copy change and
# set it to the launch day when a change ships; facts keep their own `checked` day.
UPDATED = "2026-11-05"

# The comparisons a site footer names, by search demand; the hand-written footers in landing.html,
# rankings.html and showcase.html list the same ones (test_footers_name_the_same_comparisons).
FOOTER = ("clay", "apollo", "composio", "zapier", "apify", "openrouter")


def modified(key: str | None = None) -> str:
    """The day a page last changed: its facts' `checked` day or the copy's UPDATED, the later."""
    return max(checked(key), UPDATED)


def checked(key: str | None = None) -> str:
    """The day a page's facts were read; for the hub, the latest of them."""
    if key is None:
        return max(checked(k) for k in COMPETITORS)
    return COMPETITORS[key].get("checked", CHECKED)


def checked_label(key: str | None = None) -> str:
    d = date.fromisoformat(checked(key))
    return f"{d.day} {d:%B %Y}"


def rows_for(key: str) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """The table rows this page shows: every row, less an optional row its record leaves out."""
    cells = COMPETITORS[key]["cells"]
    return tuple((g, tuple((r, lab) for r, lab in items if r not in OPTIONAL_ROWS or r in cells))
                 for g, items in ROWS)


def catalog_provider(key: str) -> str:
    """The provider id this product's tools carry in treg's catalog, when the catalog has them."""
    return COMPETITORS[key].get("provider", key)


def for_provider(service: str) -> str | None:
    """The comparison page for a catalog provider, if there is one."""
    return next((k for k in COMPETITORS if catalog_provider(k) == service), None)


def page_slug(key: str) -> str:
    return f"treg-vs-{key}"


def short(key: str) -> str:
    c = COMPETITORS[key]
    return c.get("short", c["name"])
