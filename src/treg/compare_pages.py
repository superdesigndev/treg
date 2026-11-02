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

CHECKED = "2026-11-03"
CHECKED_LABEL = "3 November 2026"
CORRECTIONS = "jason@superdesign.dev"

NOT_PUBLISHED = "Not published"

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
    "x402": "No. Top-ups are by card.",
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
        "title": "treg vs Agentik: two tool gateways for AI agents",
        "description": "treg and Agentik both give an AI agent paid tools from one prepaid balance. "
                       "An honest look at fees, catalogs, own keys, spend caps and open source.",
        "sub": "Both give your agent paid tools through one connection and one balance. They differ "
               "on fees, keys and spend controls.",
        "their_words": "The tool gateway for your agents.",
        "tldr": "Agentik and treg do the same job: one connection, one prepaid balance, many paid "
                "tools and no provider signup. Agentik adds a 5.5% fee to each top-up (minimum "
                "$0.80) and has finer spending caps on each key. treg adds no fee, accepts your "
                "own keys, and its code is on GitHub.",
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
            "You want no fee on top of provider prices, on top-ups or on calls.",
            "You already hold keys for some providers and want those calls free, beside the shared "
            "catalog.",
            "You want your team's own APIs, CLIs and skills behind the same token.",
            "You want to read the code, or run your own registry for your team.",
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
             "caps and an allowlist of tools. treg's caps are per team, per member, per call and "
             "per tagged customer, and the per-customer budgets are advisory."),
            ("More credit to start.", "Eligible new accounts get $5 for 14 days. treg gives $1.00 once."),
            ("Ad networks.", "Its Get MCP Ads flow connects Amazon Ads and Reddit, which treg does not "
             "connect today."),
            ("Less kept about each run.", "Agentik keeps execution and billing metadata, not the "
             "payloads of tool calls."),
        ],
        "different": [
            ("No fee at all.", "treg adds no markup and no top-up fee. On Agentik the provider price "
             "has no markup, but each top-up after the first small one carries 5.5%."),
            ("Your own keys win.", "Register your key for a provider and treg uses it instead of its "
             "own, without metering the call. Agentik holds every provider account."),
            ("Your accounts are free.", "Calls on your connected Google Analytics, Search Console, Google "
             "Ads and Meta Ads accounts are never metered by treg. Agentik prices ad-account reads at "
             "$0.005 and heavy reports or changes at $0.05."),
            ("Your team's own tools.", "A teammate can register an API, a CLI or a skill, and every "
             "agent on the team calls it through the same token."),
            ("The code is public.", "treg's code is on GitHub, and you can run your own registry for "
             "your team."),
        ],
        "both": "Both speak MCP, so one agent can hold both connections. A team could keep Agentik "
                "for capped keys and the ad networks only it connects, and use treg for its own keys, "
                "its team's tools and the wider catalog.",
        "faq": [
            ("Is treg cheaper than Agentik?",
             "On fees, yes: treg adds no fee, and Agentik adds 5.5% to each top-up after a first top-up "
             "of $10 or less. Both bill tools at the provider's price, but the providers and the tools "
             "differ, so compare the tools you will call. To start, Agentik gives eligible new accounts "
             "$5 and treg gives $1.00."),
            ("Can I use my own API keys with Agentik or treg?",
             "Agentik says no: it holds the provider accounts. treg says yes: your team's key always wins "
             "over treg's, and those calls are never metered."),
            ("Which one has better spending caps?",
             "Agentik, for caps on a single key: hourly, daily, lifetime and per tool. treg has a team "
             "daily spend cap, a per-call cost ceiling, per-member daily call caps and advisory daily "
             "budgets per tagged customer."),
            ("Can I self-host Agentik or treg?",
             "Agentik is a hosted service. treg's code is on GitHub, and you can run your own registry "
             "for your team."),
            ("Does Agentik compare itself with treg?",
             "Yes. Agentik publishes its own comparison with treg, linked in the sources below. We read "
             "it, and its description of treg was mostly accurate on the date it states."),
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
        "site": "https://firecrawl.dev/alexandria",
        "group": "per-call",
        "mono": "#FF5A1F",
        "logo": "/logos/firecrawl.svg",
        "title": "treg vs Firecrawl Alexandria: data for AI agents",
        "description": "Firecrawl Alexandria and treg both give AI agents data from many providers. "
                       "Compare coverage, billing, own keys and access, read from public pages.",
        "sub": "Both give your agent data from many sources through one key. Alexandria leans on "
               "public records and its own indexes; treg leans on commercial data APIs.",
        "their_words": "The knowledge library for superintelligence.",
        "tldr": "Alexandria is part of Firecrawl. One search mixes the live web, Firecrawl's own "
                "indexes and structured tools from official providers, with deep public-records "
                "coverage, paid in Firecrawl credits from a plan. treg is a catalog of commercial data "
                "APIs (SEO, enrichment, social, ads, generation) billed per call from a prepaid "
                "balance, with your own keys and accounts beside it.",
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
            "You want to pay per call from a balance, with no monthly plan.",
            "You want your own provider keys and your own Google, Meta or LinkedIn accounts behind the "
            "same token.",
            "You want the provider's own response, with every field it returns.",
        ],
        "cells": _c(
            how_pay="Firecrawl credits from a monthly plan: free, $19, $99, $399 or $749 a month, or "
                    "Enterprise. Pay-as-you-go top-ups on paid plans.",
            free_start="Free plan: 1,000 credits a month, no card. Alexandria needs an account.",
            fees="Tools run at their listed price in credits. A markup is not published. Credits roll "
                 "over only on Scale and Enterprise.",
            x402=NOT_PUBLISHED,
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
             "plus free Research and Government indexes. treg has little of this."),
            ("Web and tools in one query.", "A search can return web pages and provider tools together, "
             "and falls back to search and scraping when no provider fits."),
            ("Its own indexes.", "113M+ indexed papers, READMEs, issues and docs, useful for coding agents."),
            ("Licensed sources.", "Providers are paid under agreement, with terms an admin accepts for "
             "the whole organization."),
            ("A mature platform.", "SOC 2 Type II, SDKs, a CLI, local and remote MCP, and a ChatGPT and "
             "Codex plugin."),
        ],
        "different": [
            ("No plan.", "treg bills each call from a prepaid balance at the provider's rate. Alexandria "
             "uses Firecrawl credits from a monthly plan."),
            ("Commercial GTM and SEO data.", "Keyword volume, backlinks, SERPs, work emails and phones, "
             "social profiles and ad libraries, from many vendors."),
            ("Your keys and accounts.", "Your own provider keys win and are never metered, and your "
             "connected Google, Meta, LinkedIn and other accounts sit behind the same token."),
            ("The raw answer.", "treg relays the provider's own response unchanged, so no field is lost "
             "in translation."),
        ],
        "both": "Firecrawl's scrape, crawl and search tools are in the treg catalog too, billed per call "
                "on treg's Firecrawl account. A team can use Alexandria for public records and research, "
                "and treg for SEO, enrichment, social and ads.",
        "faq": [
            ("Does Firecrawl Alexandria cover the same data as treg?",
             "Partly. Alexandria's largest groups are public records, company, retail and finance data, "
             "plus Firecrawl's own indexes. treg's catalog leans on SEO, people and company enrichment, "
             "social and ad data, and generation models. Search both for the job you need."),
            ("Do I need a Firecrawl plan to use Alexandria?",
             "You need a Firecrawl account. The free plan gives 1,000 credits a month; paid plans start "
             "at $19 a month, and pay-as-you-go top-ups need a paid plan. treg has no plan: you top up a "
             "balance and pay per call."),
            ("Can I use my own provider keys with Alexandria?",
             "Firecrawl does not publish that. With treg, your own key for a provider always wins and "
             "those calls are never metered."),
            ("What happens when no provider fits my question?",
             "Alexandria falls back to web search and scraping. treg's routed tools try the next provider "
             "when one fails or misses; when the catalog has no tool for a job, the agent can file a "
             "request."),
            ("Is Alexandria part of open-source Firecrawl?",
             "Firecrawl's core is open source under AGPL-3.0. Firecrawl does not say whether Alexandria "
             "runs on a self-hosted install, and its README says the cloud version has more features."),
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
        "title": "treg vs AIsa: paid data APIs for AI agents compared",
        "description": "AIsa and treg both sell data to AI agents per call. Compare schemas, billing, "
                       "x402, own keys and team seats, read from each product's public pages.",
        "sub": "Both sell data to agents per call. AIsa reshapes every answer into one schema; treg "
               "relays each provider's answer as it is.",
        "their_words": "The resource layer for AI agents.",
        "tldr": "AIsa returns data in one normalized schema, labels the source of every value, and "
                "bills per result, with x402 for agents that have no account. treg relays each "
                "provider's own answer unchanged and bills per call at the provider's rate, with your "
                "own keys, your connected accounts and several team members on every account.",
        "spec": {"billing": "Prepaid, per result", "start": "$1 every month",
                 "keys": "Enterprise only", "code": "CLI and skills"},
        "fit_them": [
            "You want one response shape across platforms, so your agent never parses vendor formats.",
            "You want the source, method and time of every value, with published calibration.",
            "You want empty responses and failed prechecks to cost nothing.",
            "Your agents pay per call with x402 or MPP, with no account.",
            "You want language models on the same key and balance.",
        ],
        "fit_treg": [
            "You want every field the provider returns, in the provider's own format.",
            "You want to use your own provider keys without an Enterprise contract.",
            "Several people on your team share one account.",
            "Your agent works in your own Google Analytics, Search Console or ad accounts.",
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
             "the same way. treg passes each provider's own format through."),
            ("Provenance on every value.", "Each value says where it came from and how, and AIsa publishes "
             "its calibration."),
            ("Pay for results.", "Empty responses and failed prechecks are free, and verified fields are "
             "billed only when verification passes."),
            ("x402 and MPP.", "An agent with no account can pay per call. treg has no such option."),
            ("Models on the same key.", "110+ language models on the same balance. treg's catalog has only "
             "a few model endpoints."),
            ("A free $1 every month.", "treg's $1.00 comes once per new account."),
        ],
        "different": [
            ("The provider's own answer.", "treg relays the response unchanged, so nothing is dropped when "
             "a vendor adds a field."),
            ("Your own keys on every account.", "Your team's key always wins and is never metered, with no "
             "Enterprise contract."),
            ("Teams on every account.", "Owner, admin, member and viewer roles, and tool access per member."),
            ("Your connected accounts.", "Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn, "
             "Slack, never metered."),
            ("The code is public.", "treg's code is on GitHub, and you can run your own registry."),
        ],
        "both": "If your agent needs one schema for commerce or social data, AIsa does that work for you. "
                "If it needs a vendor's full response, your own keys, or your own accounts, treg fits. Both "
                "are remote MCP servers, so one agent can use each for its own jobs.",
        "faq": [
            ("Does AIsa return the vendor's raw response?",
             "No. AIsa returns its own normalized schema, with each value labeled by source. treg returns "
             "the provider's own response unchanged."),
            ("Can I use my own API keys with AIsa?",
             "AIsa lists your own vendor keys on its Enterprise plan. treg accepts your keys on every "
             "account, and those calls are never metered."),
            ("Can my whole team share one account?",
             "AIsa's Starter and Developer plans have 1 seat; Enterprise has unlimited seats. treg teams "
             "have owner, admin, member and viewer roles on every account."),
            ("Can an agent pay per call with x402?",
             "With AIsa, yes: x402 and MPP, with no account. treg does not offer x402; top-ups are by card."),
            ("Which is cheaper?",
             "It depends on the tool. AIsa prices per result times a plan multiplier and does not charge "
             "for empty results. treg bills the provider's own rate with no markup. Compare the calls you "
             "will make."),
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
        "title": "treg vs Locus: paid APIs for AI agents compared",
        "description": "Locus and treg both give AI agents paid APIs from a prepaid balance. Compare "
                       "plans, fees, own keys, reselling and spend controls, read from public pages.",
        "sub": "Both give agents paid APIs from one balance. Locus is built to resell tools to your "
               "customers; treg is built around your team's own keys and accounts.",
        "their_words": "Agent tools, billed per user.",
        "tldr": "Locus Pro gives agents paid APIs from a prepaid balance, with monthly plans, and it is "
                "built to resell: per-customer balances, your own markup and an embedded billing "
                "widget. It also offers a USDC wallet and x402. treg has no plans or fees, accepts "
                "your own keys on every account, and adds your connected accounts and your team's "
                "own tools.",
        "spec": {"billing": "Plans from $0 to $1,000 a month", "start": "Free plan, no credit",
                 "keys": "Enterprise only", "code": NOT_PUBLISHED},
        "fit_them": [
            "You resell tools to your own customers, with your markup, per-customer balances and a "
            "billing widget.",
            "Your agents pay in USDC or with x402, with no account.",
            "You want each agent scoped: allowed tools, a maximum per call, a budget per run and an expiry.",
            "You want signed webhooks for every charge, top-up and low balance.",
        ],
        "fit_treg": [
            "You want no subscription: top up and pay per call.",
            "You want your own provider keys without an Enterprise plan.",
            "Your agent works in your own Google, Meta, LinkedIn or Slack accounts.",
            "You need SEO, enrichment, social and ad-library data.",
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
             "payouts. treg lets you tag calls per customer and invoice from its ledger, but it has no "
             "billing widget or markup."),
            ("Agent payments.", "x402, MPP and a USDC wallet let an agent pay with no account."),
            ("Agent scoping.", "A maximum per call and a shared budget per run, set on each agent "
             "connection, checked before the provider is called."),
            ("Webhooks.", "Signed, retried webhooks for usage events."),
            ("Language models.", "OpenAI, Anthropic, Gemini and others on the same balance."),
        ],
        "different": [
            ("No plans.", "treg has no subscription and no fee; Locus Pro's paid plans run from $20 to "
             "$1,000 a month."),
            ("Your keys on every account.", "Your team's key always wins and is never metered. On Locus, "
             "your own keys need Enterprise."),
            ("Your accounts.", "Google Analytics, Search Console, Ads, Meta Ads, LinkedIn and Slack, "
             "connected by OAuth and never metered."),
            ("GTM and SEO data.", "Keyword volume, backlinks, work emails and phones, social profiles and "
             "ad libraries."),
            ("The code is public.", "treg's code is on GitHub, and you can run your own registry."),
        ],
        "both": "A platform that resells tools to its customers may want Locus's billing layer. A team "
                "whose agent needs GTM data and its own accounts may want treg. Both are remote MCP "
                "servers.",
        "faq": [
            ("Do I need a subscription for Locus or treg?",
             "Locus Pro has a free plan and paid plans from $20 a month; Pay With Locus is a USDC wallet "
             "with no subscription. treg has no subscription: you top up a balance and pay per call."),
            ("Can my agent connect my own Google Analytics or ad accounts?",
             "Locus does not publish that. treg connects Google Analytics, Search Console, Google Ads, "
             "Meta Ads and LinkedIn, and never meters those calls."),
            ("Is there a markup on the provider's price?",
             "Locus does not publish one, and charges a card processing fee on top-ups. treg adds no markup "
             "and no top-up fee."),
            ("Can I use my own provider keys?",
             "On Locus, with Enterprise and Custom APIs. On treg, on every account."),
            ("Do I need a crypto wallet?",
             "No, for either. Locus Pro takes cards, and Locus also offers USDC and x402. treg takes cards "
             "only."),
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
        "description": "Monid and treg both give AI agents a catalog of paid tools from a prepaid "
                       "balance. Compare x402, open source, own keys and controls, from public pages.",
        "sub": "Both give agents a catalog of paid tools from a prepaid balance, with $1 to start. They "
               "differ on payments, keys and accounts.",
        "their_words": "Connect your agent to every tool it needs.",
        "tldr": "Monid and treg are close. Both give agents a catalog of paid tools from a prepaid "
                "balance, with $1 to start and no subscription. Monid adds x402 payments and an "
                "MIT-licensed connector layer. treg adds your own keys, your connected accounts, your "
                "team's own tools and spend caps.",
        "spec": {"billing": "Prepaid, per call or result", "start": "$1",
                 "keys": NOT_PUBLISHED, "code": "Connectors MIT"},
        "fit_them": [
            "You want to read or contribute to MIT-licensed connector definitions.",
            "Your agents pay with x402 in USDC on Base or Monad.",
            "You want agent email and agent phone tools.",
            "You embed it with a \"Sign in with Monid\" OAuth flow.",
        ],
        "fit_treg": [
            "You want your own provider keys on the hosted service, never metered.",
            "Your agent works in your own Google Analytics, Search Console or ad accounts.",
            "You want your team's own APIs, CLIs and skills behind the same token.",
            "You want team spend caps and roles.",
        ],
        "cells": _c(
            how_pay="A prepaid wallet, pay as you go. No subscription.",
            free_start="$1 to start.",
            fees="Per-call or per-result prices. A markup is not published. x402 runs have a $0.01 minimum.",
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
            ("x402.", "An agent can pay a run in USDC on Base or Monad. treg has no x402."),
            ("Agent email and phone.", "Tools that let an agent send email or make a phone call."),
            ("Embedding.", "\"Sign in with Monid\" for your users' own workspaces, or your own proxy for "
             "billing control."),
        ],
        "different": [
            ("Your own keys.", "Your team's key always wins over treg's and is never metered."),
            ("Your accounts.", "Google Analytics, Search Console, Google Ads, Meta Ads, LinkedIn and "
             "Slack, connected by OAuth and never metered."),
            ("Your team's tools.", "Register an API, a CLI or a skill once, and every agent on the team calls "
             "it with the credential held on the server."),
            ("Spend controls.", "A team daily cap, a per-call ceiling and per-member call caps."),
            ("Published 0% markup.", "No markup on provider prices is a commitment in treg's terms."),
        ],
        "both": "Both are remote MCP servers with similar catalogs. Search both for the tools you need, and "
                "compare the price of the calls you will make.",
        "faq": [
            ("Can I bring my own provider keys to Monid?",
             "Monid does not publish this for its hosted service; its open-source engine can run a connector "
             "locally with your own key. treg uses your team's key whenever you register one, and does not "
             "meter those calls."),
            ("Can my agent use my own Google Analytics or ad accounts?",
             "Monid does not publish that. treg connects them by OAuth and never meters those calls."),
            ("What is the markup on each platform?",
             "Monid does not publish one. treg adds no markup, and says so in its terms."),
            ("Can I set spend caps?",
             "Monid does not publish spend caps. treg has a team daily spend cap, a per-call ceiling and "
             "per-member daily call caps."),
            ("Which is open source?",
             "Both, in part. Monid's connector layer and engine are MIT. treg's whole registry is on GitHub "
             "under Apache 2.0 with added terms."),
        ],
        "sources": [
            ("Monid home page", "https://monid.ai"),
            ("How it works", "https://monid.ai/docs/guide/how-it-works.md"),
            ("Pay with x402", "https://monid.ai/docs/guide/pay-with-x402.md"),
            ("API overview", "https://monid.ai/docs/api/overview.md"),
            ("Monid on GitHub", "https://github.com/monid-ai/monid"),
        ],
    },
    "composio": {
        "name": "Composio",
        "site": "https://composio.dev",
        "group": "apps",
        "mono": "#1F2937",
        "logo": "/media/compare/logos/composio.png",
        "logo_fills": True,
        "title": "treg vs Composio: a Composio alternative for AI agents",
        "description": "Composio connects agents to 1,000+ apps in each user's own accounts. treg gives "
                       "agents paid data per call. An honest comparison of what each one is for.",
        "sub": "Composio lets your agent act inside each user's apps. treg gives your agent paid data "
               "it has no account for. They overlap only a little.",
        "their_words": "Give your AI secure access to 1,500+ apps in minutes.",
        "tldr": "Composio connects your agent to 1,000+ apps (Gmail, Slack, Salesforce, HubSpot) so it "
                "can act inside each user's own accounts, with managed OAuth and triggers. treg is "
                "mostly a catalog of paid data APIs on treg's own accounts, plus your connected "
                "accounts. They overlap on a few search and scraping tools; for most teams they do "
                "different jobs.",
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
            "Your agent needs data you have no account for: SEO, enrichment, social, ads or generation.",
            "You want each call billed at the provider's rate, with no plan.",
            "You want your own provider keys to win and never be metered.",
            "You want to self-host for your team without an Enterprise plan.",
        ],
        "cells": _c(
            how_pay="Plans: Hobby $0 with 100,000 tool calls a month, Pro $29 a month with usage after "
                    "that, Enterprise custom.",
            free_start="Hobby plan, no card, with $2 of Instant Tools credit.",
            fees="Own-app calls $0.0003 each after 100K; managed-app calls $0.0005 to $0.01. Instant Tools "
                 "at 0% markup plus the tool-call charge.",
            x402=NOT_PUBLISHED,
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
             "each user's own account. treg connects a short list of accounts."),
            ("Triggers.", "Realtime and polling triggers let agents react to events. treg has none."),
            ("Multi-user products.", "Per-user connected accounts, a hosted Connect Link and white-labeled "
             "OAuth."),
            ("Cheap volume.", "100,000 free calls a month, then $0.0003 per call on your own apps."),
            ("Compliance.", "SOC 2 Type II, with BAA, zero data retention and IP allowlists as options."),
        ],
        "different": [
            ("Paid data, no vendor accounts.", "SEO and SERP data, backlinks, people and company enrichment, "
             "social, ad libraries and generation models, on treg's accounts."),
            ("No plan.", "A prepaid balance billed at each provider's rate, with no markup and no seats."),
            ("Your own keys win.", "Register a key and treg uses it instead of its own, never metered."),
            ("Self-host for your team.", "treg's code is on GitHub; Composio's platform self-host is on "
             "Enterprise."),
        ],
        "both": "Many agents need both jobs. Composio can send the email, update the CRM and listen for "
                "events; treg can find the keyword volume, the work email and the competitor's ads. Both "
                "are MCP servers, so one agent can hold both.",
        "faq": [
            ("Can I get SEO or enrichment data from Composio without a vendor account?",
             "Composio's Instant Tools run some search, scraping and maps tools on Composio's own accounts. "
             "Its pages do not list SEO metrics, enrichment or ad data there. treg's catalog covers those, "
             "billed per call."),
            ("Can I cap spend on Composio?",
             "Composio lists spend caps on its Pro plan, but its Instant Tools docs say that spend cannot be "
             "capped by amount and calls fail with 402 when the balance runs out. treg has a team daily cap, "
             "a per-call ceiling and per-member call caps."),
            ("Does a Composio call return the raw API response?",
             "Composio returns its own tool results, and Proxy Execute calls a provider's API directly. "
             "treg returns the provider's own response unchanged."),
            ("Can I self-host Composio or treg?",
             "Composio's SDKs are MIT, and the platform self-hosts on Enterprise. treg's code is on GitHub, "
             "and you can run it for your own team."),
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
                       "paid data per call with no vendor accounts. Which one fits, and when to use both.",
        "sub": "Zapier MCP lets your agent act in the apps you connect. treg gives your agent paid data "
               "from vendors you have no account with.",
        "their_words": "Go from AI chat to AI action.",
        "tldr": "Zapier MCP lets your agent take actions in the 9,000+ apps you connect to Zapier, with "
                "each successful call costing 2 tasks from your plan. treg gives your agent paid data "
                "from providers you have no account with, billed per call from a prepaid balance. "
                "Teams that need both jobs often use both.",
        "spec": {"billing": "Tasks from a plan", "start": "100 tasks a month",
                 "keys": "Your connected apps", "code": "Hosted only"},
        "fit_them": [
            "Your agent acts in long-tail SaaS apps that only Zapier connects.",
            "You already use Zapier: MCP draws on the same task pool, with no new contract.",
            "You want Zaps and triggers that run without the agent.",
            "You need SAML SSO, SCIM and audit logs kept up to a year.",
        ],
        "fit_treg": [
            "Your agent needs SEO, enrichment, social or ad data without your own vendor accounts.",
            "You want a price in dollars for each call, not in tasks.",
            "You want your own keys and your team's own tools behind one token.",
            "You want to read the code or self-host.",
        ],
        "cells": _c(
            how_pay="Plans billed in tasks: Free, Pro from $19.99 a month and Team from $69 a month "
                    "(annual). Each successful MCP tool call is 2 tasks.",
            free_start="Free plan: 100 tasks a month, about 50 MCP tool calls.",
            fees="Failed calls use no tasks. Pay-per-task overage at 1.25x to 2.5x the base rate.",
            x402=NOT_PUBLISHED,
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
            ("The largest app catalog.", "9,000+ apps with tens of thousands of actions. treg connects a "
             "short list of accounts."),
            ("Actions in your apps.", "Send the email, update the CRM, post in Slack. That is a different "
             "job from fetching data."),
            ("Automations that run alone.", "Zaps and triggers run without the agent."),
            ("Governance.", "SOC 2 Type II, SAML SSO, SCIM, audit logs up to a year, and access control per "
             "app and action."),
            ("One bill for Zapier customers.", "MCP uses the task pool you already pay for."),
        ],
        "different": [
            ("Paid data with no accounts.", "Keyword volume, backlinks, SERPs, work emails, social profiles, "
             "ad libraries and generation models, on treg's own accounts."),
            ("Dollars, not tasks.", "Each call shows its price before it runs, and you pay only what it "
             "costs."),
            ("Your own keys and tools.", "Your key always wins and is never metered; your team's own APIs, "
             "CLIs and skills sit behind the same token."),
            ("Public code.", "treg's code is on GitHub, and you can self-host for your team."),
        ],
        "both": "Use Zapier MCP for the actions in your apps and treg for the data. For example, treg finds "
                "the decision maker's work email, and Zapier adds the lead to your CRM.",
        "faq": [
            ("How much does a Zapier MCP call cost?",
             "Each successful tool call uses 2 tasks from your plan, and failed calls use none. The dollar "
             "price depends on your plan's task tier. On treg each tool shows its own price in dollars."),
            ("Can Zapier get SEO or enrichment data without my own vendor account?",
             "Zapier's pages describe actions in apps you connect with your own accounts, not paid data on "
             "Zapier's accounts. treg's catalog runs those calls on treg's own accounts."),
            ("What happens when my Zapier tasks run out?",
             "With pay-per-task off, usage stops at your limit; with it on, overage is billed at 1.25x to "
             "2.5x the base rate, up to 3x your plan. On treg, metered calls stop when the balance runs "
             "out, and calls on your own keys keep working."),
            ("Can my team share one MCP server?",
             "On Zapier, on Team and Enterprise plans. On treg, everyone on a team uses the team's tools "
             "and connections, with owner, admin, member and viewer roles."),
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
        "title": "treg vs Pipedream Connect: tools for AI agents compared",
        "description": "Pipedream Connect gives the agents you build 10,000+ tools in your users' own "
                       "accounts. treg gives agents paid data per call. An honest comparison.",
        "sub": "Pipedream Connect lets the agents you build act in each user's own accounts. treg gives "
               "your agent paid data it has no account for.",
        "their_words": "The integration layer for AI agents.",
        "tldr": "Pipedream Connect gives the agents you build 10,000+ tools across 3,000+ APIs, acting "
                "in each end user's own accounts with managed OAuth. Pipedream is now part of Workday. "
                "treg gives your agent paid data from providers you have no account with, billed per "
                "call.",
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
            "Your agent needs paid data with no vendor accounts: SEO, enrichment, social, ads.",
            "You want no per-user fee and no plan.",
            "You want your team's own keys and tools behind one token.",
            "You want open code you can self-host.",
        ],
        "cells": _c(
            how_pay="Free for development. Startup $99 a month (annual) with 100 external users and 10,000 "
                    "credits, then $2 per user and $0.012 per credit. One credit is 30 seconds of compute.",
            free_start="Free for development and personal MCP use, no card. Production needs a paid plan.",
            fees="MCP tool calls use credits; there is no separate MCP fee.",
            x402=NOT_PUBLISHED,
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
            ("Paid data with no accounts.", "SEO and SERP data, enrichment, social, ad libraries and "
             "generation, on treg's own accounts."),
            ("Billed per call in dollars.", "No per-user fee and no compute credits."),
            ("Your own keys win.", "Register a key and treg uses it instead of its own, never metered."),
            ("Open code.", "treg's code is on GitHub under Apache 2.0 with added terms, and you can run it "
             "for your team."),
        ],
        "both": "A product can use Pipedream Connect for actions in its users' apps and treg for the data "
                "its agent needs from vendors nobody has an account with.",
        "faq": [
            ("Did the Workday acquisition change Pipedream Connect?",
             "Pipedream's docs say Connect, its API, MCP servers and API proxy are not affected. Pipedream "
             "Workflows and String shut down on 31 March 2027."),
            ("What does one Pipedream MCP call cost?",
             "MCP tool calls use credits, at 1 credit per 30 seconds of compute. On Startup, 10,000 credits "
             "are included, then $0.012 each, plus $2 per external user after 100."),
            ("Can Pipedream get paid data without my own vendor account?",
             "Pipedream does not publish that. Its docs describe users connecting their own accounts or keys. "
             "treg runs catalog calls on its own accounts."),
            ("Can I use Pipedream in production for free?",
             "No. The free plan covers development; production needs Startup or Business. treg has no plan: "
             "new accounts get $1.00 of credit, then you top up."),
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
        "title": "treg vs Smithery: an MCP directory and a tool catalog",
        "description": "Smithery is now a free MCP directory; its hosted proxy shut down on 31 October "
                       "2026. What changed, and how treg's tool catalog differs.",
        "sub": "Smithery is now a free directory of MCP servers. treg is one token for a catalog of paid "
               "tools. Here is what changed, and how they differ.",
        "their_words": "Connect agents to thousands of tools and services.",
        "tldr": "Since 31 October 2026, Smithery is a free public directory of MCP servers, run by "
                "Arcade.dev. Its hosted proxy, managed identity, CLI and SDKs have shut down, so each "
                "server is now reached directly at its vendor, with your own account there. treg is a "
                "different thing: one token for a catalog of paid tools, with treg holding the provider "
                "accounts.",
        "note": "Arcade.dev acquired Smithery in August 2026. Smithery's hosted URLs, proxy, managed "
                "identity, CLI, SDKs, accounts and API keys stopped working on 31 October 2026.",
        "spec": {"billing": "Free directory", "start": "Free, no account",
                 "keys": "Your account at each vendor", "code": "No public repos"},
        "fit_them": [
            "You want to browse MCP servers for free, with no account.",
            "You need developer and documentation tools that treg does not carry.",
            "You want each vendor's official server, with its full set of actions.",
            "You want nothing between your agent and the vendor.",
        ],
        "fit_treg": [
            "You want one token for many paid data APIs, not one account per vendor.",
            "You want one prepaid balance, billed per call at each provider's rate.",
            "You want your own keys and your team's own tools behind that token.",
            "You used Smithery-hosted URLs and need one connection for many tools.",
        ],
        "cells": _c(
            how_pay="Free to browse. Paid plans ended, and paying users were refunded charges since "
                    "5 August 2026.",
            free_start="Free, with no account needed to browse.",
            fees="None. Smithery no longer proxies or bills calls; you pay each vendor.",
            x402="Not applicable.",
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
            ("Breadth of kinds.", "Documentation search, developer tools and niche servers that treg does "
             "not carry."),
            ("Official servers.", "Verified entries point to the vendor's own MCP endpoint, often with its "
             "full write surface."),
            ("No middle hop.", "Your agent talks to the vendor directly."),
        ],
        "different": [
            ("One token, many vendors.", "treg holds the provider accounts. Your agent does not sign up at "
             "each vendor."),
            ("One bill.", "Each catalog call is billed at the provider's rate from one prepaid balance."),
            ("Your keys and tools.", "Your own keys win and are never metered, and your team's own tools sit "
             "behind the same token."),
            ("Still running.", "treg's hosted service and CLI are live, and its code is on GitHub."),
        ],
        "both": "Smithery's directory is a good place to find a vendor's official MCP server. When your "
                "agent needs paid data from many vendors through one connection, treg is a different tool "
                "for that job.",
        "faq": [
            ("Is Smithery still a hosted MCP gateway?",
             "No. Since 31 October 2026, Smithery is a public directory that does not proxy calls. Hosted "
             "URLs, the CLI, the SDKs and Smithery API keys stopped working on that date."),
            ("What replaces Smithery-hosted servers?",
             "Smithery points users to Arcade.dev, which acquired it. On 3 November 2026 Arcade's pricing "
             "listed a free plan with 2,000 tool calls a month, and a Team plan at $25 a month plus $0.01 a "
             "call. You can also connect to each vendor's own MCP URL."),
            ("Do I need an account at every vendor with Smithery?",
             "Yes, now that it does not proxy. With treg, catalog calls run on treg's accounts and are billed "
             "to your balance."),
            ("Can one token reach many paid data APIs?",
             "Not through Smithery, which lists one server per vendor. treg gives one token for its whole "
             "catalog."),
        ],
        "sources": [
            ("Smithery directory", "https://smithery.ai/servers"),
            ("The future of Smithery", "https://smithery.ai/future"),
            ("Smithery joins Arcade", "https://www.arcade.dev/blog/smithery-joins-arcade"),
            ("Arcade pricing", "https://www.arcade.dev/pricing"),
        ],
    },
    "clay": {
        "name": "Clay",
        "site": "https://www.clay.com",
        "group": "gtm",
        "mono": "#5B4CF0",
        "logo": "/media/compare/logos/clay.png",
        "title": "treg vs Clay: a Clay alternative for AI agents",
        "description": "Clay is a GTM data platform with tables and waterfalls. treg lets a coding agent "
                       "call data APIs per call, with no plan. An honest look at which one fits.",
        "sub": "Clay is built for GTM teams that work in tables. treg is built for agents that call data "
               "APIs directly.",
        "their_words": "Build systems to grow revenue.",
        "tldr": "Clay is the better tool for a GTM team that works in tables: waterfall enrichment, AI "
                "research agents, CRM sync and a sequencer, billed in Actions and Data Credits on a "
                "plan. treg is for a coding agent that calls data APIs itself: SEO, people and company "
                "enrichment, social and ad data, per call at the provider's rate, with no plan.",
        "spec": {"billing": "Plans, Actions and Data Credits", "start": "500 Actions, 100 credits",
                 "keys": "Yes, still uses Actions", "code": "Hosted only"},
        "fit_them": [
            "Your team builds and fixes enrichment in a spreadsheet interface, row by row.",
            "You want waterfalls built in, with no charge when nothing is found.",
            "You want Claygent and Sculptor for AI research and no-code workflows.",
            "You want CRM and data warehouse sync, a sequencer and ad audiences in one product.",
        ],
        "fit_treg": [
            "Your agent in Claude Code, Codex or Cursor calls the data APIs itself.",
            "You want no plan: each call billed at the provider's rate.",
            "You want enrichment beside SEO, social, ad and generation tools behind one token.",
            "You want your own provider keys to cost nothing extra, not even an Action.",
        ],
        "cells": _c(
            how_pay="Plans billed in two meters: Actions (under $0.01 each) and Data Credits (from $0.05 "
                    "each). Launch from $167 a month and Growth from $446 a month on the plan cards.",
            free_start="Free plan: 500 Actions and 100 Data Credits a month, up to 200 rows a table.",
            fees="Top-ups on Launch and Growth cost 30% more. No charge when an enrichment finds nothing.",
            x402=NOT_PUBLISHED,
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
            ("Built for agents.", "Your agent searches the catalog, reads the price and calls the tool, from "
             "Claude Code, Codex, Cursor or any MCP client."),
            ("No plan.", "A prepaid balance billed at the provider's rate, with no markup and no seats."),
            ("More than enrichment.", "SEO and SERP data, backlinks, social, ad libraries and generation "
             "models sit beside the people data."),
            ("Your keys cost nothing extra.", "A call on your own key is never metered by treg."),
            ("Spreadsheets too.", "The treg add-on for Google Sheets fills cells from the catalog, and shows "
             "each run's cost first."),
        ],
        "both": "A RevOps team can keep Clay for its tables, sequences and CRM sync, and give its agents treg "
                "for ad hoc research, SEO and social data, and quick enrichment outside a table.",
        "faq": [
            ("Is treg a Clay alternative?",
             "For some jobs. If your agent calls enrichment and research APIs itself, treg does that per call "
             "with no plan. If your team works in tables with waterfalls, CRM sync and sequences, Clay does "
             "far more of that work."),
            ("Do I pay Clay twice, in Actions and Data Credits?",
             "Clay bills orchestration in Actions and third-party data in Data Credits. With your own provider "
             "key there is no Data Credit charge, but an Action is still used. treg bills one price per call, "
             "and nothing on your own key."),
            ("Can my coding agent call Clay's providers directly?",
             "Clay offers an MCP server on Launch and up, HTTP API calls from a table on Growth and up, and its "
             "own API on Enterprise. treg gives every account an HTTP API, a CLI and an MCP server."),
            ("Can I cap spend per user or agent?",
             "Clay has per-rep credit limits for MCP and per-workbook limits on Enterprise. treg has a team "
             "daily cap, a per-call ceiling and per-member call caps."),
            ("Why do Clay's prices differ on its pricing page?",
             "On 3 November 2026 the plan cards showed Launch from $167 and Growth from $446 a month, while the "
             "FAQ on the same page said $185 and $495. Check Clay's page for the current price."),
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
        "title": "treg vs Apollo.io: an Apollo alternative for AI agents",
        "description": "Apollo is a sales platform with its own 240M+ contact database. treg lets an "
                       "agent call Apollo and other data providers per call. Which one fits.",
        "sub": "Apollo is a sales platform with its own database. treg lets your agent call Apollo and "
               "many other data providers through one token.",
        "their_words": "Grow revenue with one connected GTM system.",
        "tldr": "Apollo is a full sales platform: its own contact database, sequences, a dialer and a "
                "CRM, on per-seat plans with credits. treg is not a sales platform. It lets an agent call "
                "Apollo's API and many other people and company data providers through one token, per "
                "call, and your own Apollo key works through treg without treg metering it.",
        "spec": {"billing": "Per seat, with credits", "start": "Free plan, no time limit",
                 "keys": "Your Apollo account", "code": "Hosted only"},
        "fit_them": [
            "Your reps need sequences, a dialer, call recording and a CRM.",
            "You want 240M+ contacts and 30M+ companies with rich filters in one product.",
            "You want a first-party MCP server with about 60 actions, including writes.",
            "You want a free plan with no time limit, and many API endpoints that use no credits.",
        ],
        "fit_treg": [
            "Your agent compares several people and company data providers, not one.",
            "You want no seats: each call billed at the provider's rate.",
            "You want SEO, social and ad data beside the people data.",
            "You already have an Apollo key and want your agent to use it with every other provider.",
        ],
        "cells": _c(
            how_pay="Per-seat plans with credits that reset each cycle. Apollo's own article (17 September "
                    "2026) lists Basic $49, Professional $79 and Organization $119 per user a month, billed "
                    "annually.",
            free_start="A free plan with no time limit. Search and enrichment by MCP or CLI need a work email.",
            fees="Enrichment costs 1 to 9 credits a person (+8 with a mobile number). Many other API endpoints "
                 "use no credits.",
            x402=NOT_PUBLISHED,
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
            ("Writes by MCP.", "About 60 actions, including CRM records, lists and sequences. treg's Apollo "
             "tools are data calls."),
            ("Free endpoints.", "Many Apollo API endpoints use no credits."),
            ("An interface for reps.", "A polished app for people who never touch an agent."),
        ],
        "different": [
            ("Many providers, one token.", "Apollo is one of many people and company data providers in the "
             "catalog. Routed tools try the next provider when one misses an email or phone."),
            ("No seats.", "A prepaid balance billed per call at the provider's rate, with no markup."),
            ("More than sales data.", "SEO, social, ad libraries, web scraping and generation sit beside the "
             "people data."),
            ("Your Apollo key, never metered.", "Connect your own Apollo key, and your agent's Apollo calls use "
             "your Apollo credits, with no charge from treg."),
        ],
        "both": "Apollo's API is in the treg catalog. Keep Apollo for your reps' sequences and CRM, connect your "
                "Apollo key to treg, and let your agent use it beside the other data providers.",
        "faq": [
            ("If treg has Apollo endpoints, why use Apollo directly?",
             "For everything that is not a data call: sequences, the dialer, the CRM, and the app your reps use "
             "every day. treg's Apollo tools are people search and enrichment calls for an agent."),
            ("Which Apollo plan do I need for API access?",
             "Apollo's sources differ. On 3 November 2026 its pricing page said API access is on Custom plans, "
             "while its docs published rate limits for Free through Organization. Check with Apollo."),
            ("What does one enriched contact cost?",
             "On Apollo, 1 to 9 credits a person, plus 8 with a mobile number. On treg, each tool shows its "
             "price per call before you run it, at the provider's rate."),
            ("Do unused Apollo credits roll over?",
             "No. Apollo's pricing page says credits reset each billing cycle. treg's balance has no monthly "
             "reset."),
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
        "title": "treg vs OpenRouter: one routes models, one routes tools",
        "description": "OpenRouter gives one API for 600+ language models. treg gives one token for paid "
                       "tools and data. How they differ, and why many agents use both.",
        "sub": "OpenRouter gives one API for many language models. treg is the same idea for the tools an "
               "agent calls.",
        "their_words": "A unified API for every major LLM.",
        "tldr": "OpenRouter routes your language-model calls to 600+ models from 80+ providers through one "
                "API. treg does the same kind of job for tools: one token for paid data and generation "
                "APIs. They overlap very little, so an agent can use both.",
        "spec": {"billing": "Credits, 5.5% purchase fee", "start": "Free models, daily limits",
                 "keys": "Yes, free to $25K a month", "code": NOT_PUBLISHED},
        "fit_them": [
            "You call language models from many providers through one OpenAI-compatible API.",
            "You want automatic routing and fallback across providers for the same model.",
            "You bring your own model keys, with a large fee-free allowance.",
            "You need guardrails, zero data retention, regional routing, SSO or SLAs.",
        ],
        "fit_treg": [
            "Your agent needs data: SEO, enrichment, social, ads, web scraping.",
            "Your agent works in your own Google, Meta or LinkedIn accounts.",
            "You want no fee on top-ups.",
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
            ("Models.", "600+ models from 80+ providers behind one OpenAI-compatible API. treg's catalog has "
             "only a few model endpoints."),
            ("Routing for the same model.", "It weighs price, speed and reliability, and falls back when a "
             "provider fails."),
            ("Your own model keys.", "Free up to $25,000 a month of inference, with ordering and fallback."),
            ("Governance.", "Guardrails, zero data retention, regional routing, SSO and SLAs."),
            ("Free models.", "A free tier of models for testing."),
        ],
        "different": [
            ("Tools, not models.", "SEO and SERP data, enrichment, social, ad libraries, scraping, and image, "
             "video and voice generation."),
            ("No purchase fee.", "treg adds no fee on top-ups and no markup on calls."),
            ("Your accounts.", "Google Analytics, Search Console, Ads, Meta Ads, LinkedIn and Slack, never "
             "metered."),
            ("Public code.", "treg's code is on GitHub, and you can self-host for your team."),
        ],
        "both": "Use OpenRouter for the model and treg for the tools the model calls. treg itself reaches a "
                "few OpenRouter endpoints, such as video generation, through its own OpenRouter account.",
        "faq": [
            ("Is treg a replacement for OpenRouter?",
             "No. OpenRouter routes language-model calls. treg gives your agent paid tools and data. Most "
             "agents that use one can use both."),
            ("What fees do they charge?",
             "OpenRouter charges 5.5% on credit purchases on Standard and 8% on Business, and bills inference "
             "at list price. treg charges no fee on top-ups and no markup on calls."),
            ("Can I use my own keys?",
             "Both, yes. OpenRouter is free up to $25,000 a month on your own keys, then 5%. treg never meters "
             "a call on your own key."),
            ("Can I cap spend?",
             "OpenRouter has budgets per key and per member. treg has a team daily cap, a per-call ceiling "
             "and per-member call caps."),
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


def page_slug(key: str) -> str:
    return f"treg-vs-{key}"


def short(key: str) -> str:
    c = COMPETITORS[key]
    return c.get("short", c["name"])
