"""The hand-written half of the per-agent pages (`/agents/<agent>`).

Everything else on those pages is projected from the catalog at request time; this module holds the
only copy a person writes — who the page is for (the rotating roles), how to install treg in that
client, and the use-case menu: plain-words jobs under buyer categories, each mapped to the catalog
capabilities that do it. Kept out of `api.py` so the editorial text can be reviewed without reading
routing code, and kept free of heavy imports so nothing here costs the light CLI install anything.

Two rules the tests enforce:
  - every capability id named in USE_CASES must exist in the catalog — a job the catalog cannot do
    must not be advertised ("do not document what is not built"), and a renamed capability must
    fail the build rather than silently drop a row;
  - the install copy describes the HOSTED treg.to (the ChatGPT Connectors listing, the $1.00 grant),
    so `api.py` serves these pages only on the reference hosts (`PUBLIC_HOST_ALIASES`).
"""

from __future__ import annotations

# The rotating word in the hero: "ChatGPT for <role>". The first is what crawlers and no-JS
# readers see, so it is the broadest.
# One line per category for the overview cards on the agent page. "{agent}" is the client's name.
# The rotating word in the hero: "ChatGPT for <role>". The first is what crawlers and no-JS
# readers see, so it is the broadest.
ROLES: tuple[str, ...] = (
    "SEO experts",
    "social media managers",
    "SDRs",
    "YouTubers & creators",
    "indie hackers",
    "growth marketers",
    "e-commerce sellers",
    "market researchers",
    "media buyers",
)

# One axis, and only one: what the job is ABOUT. Never how you authenticate for it, never which
# platform serves it. The earlier cut had nine categories on the domain axis and one ("Connect your
# own accounts") on the access axis, and the mixed axis leaked: reviews appeared in two categories
# split by whose account they were, Search Console was filed away from SEO, and one job existed
# twice under two names with identical capabilities. Running on the team's own key is a PROPERTY,
# rendered as the FREE badge, and gathered into a cross-cutting list on the agent pages.
#
# Enrichment is the killer use case and keeps ONE category with sub-headings (CATEGORY_GROUPS)
# rather than five. Find / contacts / enrich are stages of one motion: as five cards they read as
# the same thing, and they would have committed four more URL segments to a distinction that only
# exists in a practitioner's head. Signals are separate because they answer a different question:
# not "who is this" but "when should I act".
CATEGORY_GROUPS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "Data enrichment": (
        ("Find companies", (
            "Build a company list by industry, size or tech",
            "Find companies similar to your best customers",
            "Find companies that use a given technology",
            "Browse a VC or accelerator's portfolio",
            "Count the matches before you pay for rows",
        )),
        ("Find people", (
            "Find people by role, company or location",
            "Rank a company's decision makers",
            "List a company's employees",
            "Find contacts similar to your best ones",
        )),
        ("Contact details", (
            "Find professional emails",
            "Verify an email before you send",
            "Find phone numbers",
            "Check a phone number is real",
            "A company's email format",
            "Every work email on a company domain",
            "Where on the web an email was seen",
        )),
        ("Enrich a record", (
            "Enrich a person from an email or LinkedIn URL",
            "Enrich a company from its domain",
            "Resolve an email or profile to the same person elsewhere",
            "Get a LinkedIn profile",
            "Get a company's LinkedIn page",
            "What technology a company runs",
            "A company's products, plans and prices",
            "A company's logo, colours and fonts",
            "Extract named fields from a company's website",
        )),
    ),
}

CATEGORY_BLURBS: dict[str, str] = {
    "Data enrichment": "Work emails, phone numbers, people search, company lists, technographics and full records.",
    "Buying signals": "Job changes, funding, hiring, news and intent: the moments worth acting on.",
    "Search & rankings": "Keyword volume, SERPs, rankings, backlinks, audits, AI-answer visibility and your own Search Console.",
    "Analytics & campaigns": "GA4, Google Ads and Meta Ads performance on the accounts you already own. Free.",
    "Advertising intelligence": "What competitors are running, and what a domain bids on.",
    "Social listening": "Creators, trends, posts, hashtags and comments across LinkedIn, Instagram, TikTok, X, Reddit.",
    "Publishing": "Post to the accounts you own, on every platform, from one prompt.",
    "YouTube & video": "Transcripts, channel stats, search, trending and comments.",
    "E-commerce": "Amazon, TikTok Shop and app-store product data and reviews.",
    "Local businesses & reviews": "Find businesses by keyword and location, read their reviews, and manage your own listing.",
    "Finance & markets": "Quotes, price history, fundamentals, dividends and crypto.",
    "Web & scraping": "Fetch any page as data, and audit one you own.",
    "Workspace": "Read and post in the team tools you already use.",
    "Market research": "Job postings, employee reviews and what developers are starring.",
}

CATEGORY_PROMPTS: dict[str, str] = {
    "Data enrichment": "Using treg, find the work email of the VP of Marketing at stripe.com and tell me what the call cost.",
    "Buying signals": "Using treg, which of these 30 companies raised money or started hiring salespeople in the last 90 days?",
    "Search & rankings": "Using treg, which queries is treg.to ranking 8 to 15 for in Search Console, and who outranks us?",
    "Analytics & campaigns": "Using treg, compare last week's Google Ads and Meta Ads spend and conversions against the week before.",
    "Advertising intelligence": "Using treg, show me every ad Notion is running on Meta right now.",
    "Social listening": "Using treg, find 20 TikTok creators posting about home espresso with 50k to 500k followers.",
    "Publishing": "Using treg, post this to my LinkedIn and X accounts, and tell me what each one returned.",
    "YouTube & video": "Using treg, get the transcript of this YouTube video and pull the 10 most-liked comments.",
    "E-commerce": "Using treg, pull these 15 ASINs into a table with title, price and rating.",
    "Local businesses & reviews": "Using treg, find 20 plumbers in Austin with a 4-plus rating, and read the latest reviews for the top 3.",
    "Finance & markets": "Using treg, get the current price of AAPL, its last 30 days of closes, and any dividends this year.",
    "Web & scraping": "Using treg, fetch these 10 URLs as clean text and tell me which ones mention pricing.",
    "Workspace": "Using treg, summarise what happened in our #support Slack channel this week.",
    "Market research": "Using treg, who is hiring salespeople this month, and what do their employees say about working there?",
}

USE_CASES: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = (
    ("Data enrichment", (
        # Find companies
        ("Build a company list by industry, size or tech", ("companies.search",)),
        ("Find companies similar to your best customers", ("companies.similar", "companies.lookalike")),
        ("Find companies that use a given technology", ("companies.tech_stack.users",)),
        ("Browse a VC or accelerator's portfolio", ("companies.investors.portfolio",)),
        ("Count the matches before you pay for rows", ("companies.search.count", "people.search.count")),
        # Find people
        ("Find people by role, company or location", ("people.search", "linkedin.search.people")),
        ("Rank a company's decision makers", ("people.decision_makers",)),
        ("List a company's employees", ("linkedin.company.people",)),
        ("Find contacts similar to your best ones", ("people.lookalike",)),
        # Contact details
        ("Find professional emails", ("people.email.find",)),
        ("Verify an email before you send", ("people.email.verify",)),
        ("Find phone numbers", ("people.phone.find",)),
        ("Check a phone number is real", ("people.phone.verify",)),
        ("A company's email format", ("companies.email_pattern",)),
        ("Every work email on a company domain", ("companies.emails.list", "companies.emails.role")),
        ("Where on the web an email was seen", ("people.email.sources",)),
        # Enrich a record
        ("Enrich a person from an email or LinkedIn URL", ("people.enrich",)),
        ("Enrich a company from its domain", ("companies.enrich",)),
        ("Resolve an email or profile to the same person elsewhere", ("people.identity.resolve",)),
        ("Get a LinkedIn profile", ("linkedin.user.profile",)),
        ("Get a company's LinkedIn page", ("linkedin.company.profile",)),
        ("What technology a company runs", ("companies.tech_stack",)),
        ("A company's products, plans and prices", ("companies.products",)),
        ("A company's logo, colours and fonts", ("companies.brand.assets",)),
        ("Extract named fields from a company's website", ("companies.website.extract",)),
    )),
    ("Buying signals", (
        ("Job changes and promotions", ("people.signals",)),
        ("A company's funding rounds", ("companies.funding",)),
        ("Hiring, headcount and news signals", ("companies.jobs", "companies.headcount_trend",
                                                "companies.news")),
        ("Buying and intent signals", ("companies.signals",)),
        ("Partners, customers, vendors and investors", ("companies.connections",)),
        ("A company's SEC filings", ("companies.sec_filings",)),
        ("New startup launches and their hiring posts", ("companies.launch_posts",)),
        ("Pages appearing and disappearing on a website", ("companies.website_evolution",)),
    )),
    ("Search & rankings", (
        ("Keyword volume, CPC and competition", ("google.keywords.volume",)),
        ("Keyword ideas from a seed", ("google.keywords.ideas",)),
        ("Google results for a keyword", ("google.serp.organic",)),
        ("Keywords a domain ranks for", ("google.domain.ranked_keywords",)),
        ("Backlink profile of a domain", ("web.backlinks.summary",)),
        ("List backlinks and find link gaps", ("web.backlinks.list", "web.backlinks.intersect")),
        ("How AI answers mention your brand", ("ai-search.mentions.summary",
                                                "ai-search.chatgpt.answer",
                                                "ai-search.perplexity.answer")),
        ("Search Console: clicks, impressions and top queries", ("search-console.performance",)),
        ("Is this page indexed, and why not", ("search-console.url_inspection",)),
    )),
    ("Analytics & campaigns", (
        ("Google Analytics: traffic and behaviour reports", ("google-analytics.report",)),
        ("Realtime visitors on your site", ("google-analytics.realtime",)),
        ("Your own campaign performance", ("google-ads.campaigns.performance", "meta-ads.insights")),
        ("Google Ads: the search terms triggering your ads", ("google-ads.search_terms",)),
        ("Your Instagram and Facebook page insights", ("instagram.account.insights",
                                                       "facebook.page.insights")),
    )),
    ("Advertising intelligence", (
        ("Ads a competitor is running now", ("meta-ads.library.search", "meta-ads.library.advertiser",
                                             "google.ads.transparency", "linkedin.search.ads")),
        ("Keywords a domain bids on", ("google.domain.paid_keywords",)),
    )),
    ("Social listening", (
        ("Find creators by keyword", ("instagram.search.users", "tiktok.search.users",
                                     "youtube.search.channels", "x.search.users")),
        ("A creator's profile and stats", ("instagram.user.profile", "tiktok.user.profile",
                                           "youtube.channel.profile", "x.user.profile")),
        ("What's trending right now", ("x.trending.topics", "tiktok.trends.searches",
                                       "youtube.trending.videos", "reddit.search.trending")),
        ("Search posts by keyword", ("x.search.posts", "reddit.search.posts",
                                     "linkedin.search.posts", "tiktok.search.videos")),
        ("Posts under a hashtag", ("instagram.hashtag.posts", "tiktok.hashtag.videos")),
        ("Mine the comments", ("instagram.post.comments", "youtube.video.comments",
                               "reddit.post.comments", "linkedin.post.comments")),
        ("A competitor's recent posts", ("x.user.posts", "linkedin.company.posts",
                                         "threads.user.posts", "linkedin.user.posts")),
        ("Podcast episodes and shows", ("spotify.search", "spotify.podcast.episodes")),
    )),
    ("Publishing", (
        ("Publish to your own accounts", ("instagram.post.create", "linkedin.user.post.create",
                                          "x.post.create", "tiktok.video.publish",
                                          "youtube.video.upload")),
        ("Post to your Google Business Profile", ("google-business.posts.create",)),
    )),
    ("YouTube & video", (
        ("Get a video's transcript", ("youtube.video.captions",)),
        ("Video details, views and stats", ("youtube.video.detail",)),
        ("A channel's profile and lifetime stats", ("youtube.channel.profile",)),
        ("Search videos and channels by keyword", ("youtube.search.videos", "youtube.search.channels")),
        ("Trending videos", ("youtube.trending.videos",)),
        ("A video's comments", ("youtube.video.comments",)),
        ("Transcripts of X and Facebook video posts", ("x.post.transcript", "facebook.post.transcript")),
    )),
    ("E-commerce", (
        ("Amazon product detail by ASIN", ("amazon.product.detail",)),
        ("Amazon search and best sellers", ("amazon.search.products", "amazon.bestsellers.list")),
        ("TikTok Shop products and reviews", ("tiktok-shop.search.products",
                                              "tiktok-shop.product.reviews")),
        ("App store search", ("app-store.search.apps", "google-play.search.apps")),
        ("Product reviews", ("walmart.product.reviews", "tiktok-shop.product.reviews")),
    )),
    ("Local businesses & reviews", (
        ("Find local businesses by keyword and location", ("yelp.business.search",
                                                           "tripadvisor.search.businesses")),
        ("A business's reviews", ("yelp.business.reviews", "tripadvisor.business.reviews",
                                  "trustpilot.business.reviews")),
        ("Your Google Business Profile reviews, and reply to them", ("google-business.reviews",
                                                                     "google-business.review.reply")),
        ("Search terms that surfaced your listing on Maps", ("google-business.insights.keywords",)),
        ("Hotel listing details", ("tripadvisor.hotel.detail",)),
    )),
    ("Finance & markets", (
        ("Current quote for a ticker", ("stocks.quote.live",)),
        ("Daily price history", ("stocks.eod.history",)),
        ("Company profile and fundamentals behind a ticker", ("stocks.company.profile",
                                                              "stocks.fundamentals.metrics")),
        ("Dividends and splits", ("stocks.actions.dividends", "stocks.actions.splits")),
        ("News for a ticker", ("stocks.company.news",)),
        ("Live crypto prices and history", ("crypto.price.current", "crypto.price.history")),
        ("Coins trending right now", ("crypto.market.trending",)),
    )),
    ("Web & scraping", (
        ("Fetch any page as clean data", ("web.scrape.job.start",)),
        ("On-page audit of a URL", ("web.page.audit",)),
    )),
    ("Workspace", (
        ("Read and post in your Slack channels", ("slack.messages.history", "slack.message.send")),
        ("Read a Telegram channel", ("telegram.channel.posts", "telegram.channel.search")),
    )),
    ("Market research", (
        ("Job postings across companies", ("companies.jobs.search", "linkedin.search.jobs")),
        ("Employee reviews of a company", ("companies.reviews",)),
        ("GitHub: trending repositories and a repo's profile", ("github.trending.repositories",
                                                                 "github.repo.profile")),
    )),
)

AGENT_ICONS: tuple[tuple[str, str, str], ...] = (
    ("chatgpt", "ChatGPT", "openai"),
    ("claude", "Claude", "claude-color"),
    ("claude-code", "Claude Code", "claudecode-color"),
    ("codex", "Codex", "codex-color"),
    ("cursor", "Cursor", "cursor"),
    ("gemini-cli", "Gemini CLI", "gemini-color"),
    ("grok-bot", "Grok Bot", "grok"),
    ("openclaw", "OpenClaw", "openclaw-color"),
    ("hermes", "Hermes Agent", "hermesagent"),
    ("opencode", "opencode", "opencode"),
    ("pi", "pi", "pi"),
)

# Logo domains for the favicon service the landing already uses
# (https://www.google.com/s2/favicons?domain=…). Hand-kept; a platform or provider not listed gets
# the treg glyph instead of a wrong logo. Abstract shelves (people, companies, web, stocks) have no
# single brand and are deliberately absent.
PLATFORM_DOMAINS: dict[str, str] = {
    "search-console": "search.google.com", "google-analytics": "analytics.google.com",
    "google-ads": "ads.google.com", "google-business": "business.google.com", "google": "google.com",
    "meta-ads": "facebook.com", "facebook": "facebook.com", "instagram": "instagram.com",
    "linkedin": "linkedin.com", "x": "x.com", "reddit": "reddit.com", "youtube": "youtube.com",
    "tiktok": "tiktok.com", "tiktok-shop": "tiktok.com", "threads": "threads.net",
    "slack": "slack.com", "telegram": "telegram.org", "github": "github.com", "spotify": "spotify.com",
    "amazon": "amazon.com", "app-store": "apple.com", "google-play": "play.google.com",
    "yelp": "yelp.com", "tripadvisor": "tripadvisor.com", "trustpilot": "trustpilot.com",
    "walmart": "walmart.com", "ai-search": "chatgpt.com", "crypto": "coingecko.com",
    "stocks": "finance.yahoo.com", "web": "cloudflare.com",
}
PROVIDER_DOMAINS: dict[str, str] = {
    "hunter": "hunter.io", "tomba": "tomba.io", "leadmagic": "leadmagic.io", "icypeas": "icypeas.com",
    "fiber-ai": "fiber.ai", "findymail": "findymail.com", "leadsforge": "leadsforge.ai",
    "oceanio": "ocean.io", "companyenrich": "companyenrich.com", "apollo": "apollo.io",
    "dataforseo": "dataforseo.com", "serpapi": "serpapi.com", "semrush": "semrush.com", "moz": "moz.com",
    "majestic": "majestic.com", "seranking": "seranking.com", "serpstat": "serpstat.com",
    "spyfu": "spyfu.com", "apify": "apify.com", "brightdata": "brightdata.com",
    "scrapecreators": "scrapecreators.com", "tikhub": "tikhub.io", "justoneapi": "justoneapi.com",
    "predictleads": "predictleads.com", "finnhub": "finnhub.io", "twelvedata": "twelvedata.com",
    "eodhd": "eodhd.com", "lusha": "lusha.com", "crunchbase": "crunchbase.com",
    "youtube": "youtube.com", "akta": "akta.pro",
}

# Why go through treg.to at all: (lead, one short line). Same on every use-case page, except for
# free own-key jobs where the metering/multi-account cards are misleading.
WHY_TREG: tuple[tuple[str, str], ...] = (
    ("One key, not 9 accounts", "treg.to holds the provider keys. Neither you nor the agent sees them."),
    ("Price before the call", "The provider's own rate, $0.000 markup, from a prepaid balance."),
    ("No subscription, no seats", "Charged per call. $1.00 free per new team, no card to start."),
    ("Your own keys are free", "Already pay Hunter? Register it and those calls are never metered."),
    ("Switch by changing a word", "Another provider is a different word in the prompt, not a new integration."),
    ("Nothing to integrate", "No SDK, no OAuth dance per vendor, no seats."),
)

# The subset of WHY_TREG that applies to free own-key jobs (Google Search Console, GA4, YouTube Data
# API on your own account, etc). "9 accounts" and "Hunter" are false when the call runs free on the
# user's own connected account, so we keep only the cards that stay true.
WHY_TREG_OWN_KEY: tuple[tuple[str, str], ...] = (
    ("No code to write", "Connect once, keep the token server side, and call from any agent."),
    ("Nothing to integrate", "No SDK, no OAuth dance per vendor, no seats."),
)

# 301 redirects from old possessive slugs to clean slugs. These pages shipped before GSC indexing,
# so redirect now while they are still largely unindexed. The old slugs are not in the sitemap
# (they are not keys of USE_CASE_PAGES) and the canonical is on the new slug.
USE_CASE_REDIRECTS: dict[str, str] = {
    "get-a-video-s-transcript": "youtube-transcript-api",
    "a-channel-s-profile-and-lifetime-stats": "youtube-channel-stats",
    "a-video-s-comments": "youtube-video-comments",
    "a-business-s-reviews": "business-reviews",
    "a-company-s-email-format": "company-email-format",
}

# The client used as the example throughout the use-case pages. One constant, so the pages are a
# template rather than ChatGPT-specific prose.
DEFAULT_AGENT = "chatgpt"

# Subscription list prices, for the "instead of" anchor on a use-case page. Sourced from
# marketing/landing/_facts.md F-20..F-23, which record where each figure came from and when. A page
# names only providers whose plan price is listed here; anything else shows the catalog spread
# instead, because an invented anchor is worse than no anchor.
PLAN_PRICES: dict[str, int] = {
    "hunter": 34, "lusha": 49, "apollo": 59, "crunchbase": 99, "diffbot": 299,
    "semrush": 139, "serpstat": 69, "spyfu": 39, "serpapi": 75, "seranking": 65,
    "moz": 99, "majestic": 50,
}

# The universal setup line: paste it into any agent's chat and it reads llms.txt and sets itself up.
SETUP_LINE = "set up treg — {base}/llms.txt"

AGENTS: dict[str, dict] = {
    "chatgpt": {
        "name": "ChatGPT",
        "h1_noun": "Connector",
        "title": "ChatGPT Connector: call {n} APIs without keys | treg.to",
        "description": (
            "treg.to is a ChatGPT Connector that lets ChatGPT call {n} APIs across {p} platforms: "
            "find work emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor "
            "ads. Priced per call at the provider's own rate, with no markup and no provider signup."),
        # The one quotable sentence an answer engine should lift. Server-rendered first, under the H1.
        "definition": (
            "treg.to is a ChatGPT Connector (and MCP server) that gives ChatGPT {n} ready-to-call "
            "APIs across {p} platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, "
            "ads and e-commerce. Calls run on treg.to's own keys and are metered from a prepaid "
            "balance at the provider's rate with $0.000 markup. Every new team starts with $1.00 "
            "free, and there are no provider accounts to open."),
        # Steps shown as numbered HTML list items. The setup line is the universal install.
        # {n} is interpolated from the catalog count at render time.
        "install_steps": [
            "Give ChatGPT this line: <b>set up treg — https://treg.to/llms.txt</b>",
            "ChatGPT reads the skill, signs you in, and is ready to call {n} APIs.",
            "Ask for what you want done. ChatGPT searches the catalog, tells you the price, and "
            "calls the endpoint. You never hold a provider key.",
        ],
        # No install screenshot: the setup-line flow has none yet, and a page ships without the
        # slot rather than with a broken image (the old Plugins-directory PNG shows a dead UI).
        "faq": [
            ("Is treg.to free to use in ChatGPT?",
             "Installing is free and every new team starts with $1.00 of calls. After that, each call "
             "is metered from the team's prepaid balance at the provider's own rate, with no markup "
             "and no subscription. Calls on your team's own keys are free."),
            ("Do I need API keys from the providers?",
             "No. treg.to makes the upstream request on its own key and relays the answer. If your "
             "team already has a key for a provider, you can register it and those calls are never "
             "metered."),
            ("What does a call cost?",
             "It depends on the job and the provider: from well under a cent for a keyword lookup "
             "to a few cents for a verified work email. treg.to adds $0.000 on top of the provider's "
             "rate. Every row on this page shows the lowest price for that job, and ChatGPT tells "
             "you the price before it spends it."),
            ("Does treg.to pick the provider for me?",
             "No. Where several providers do the same job they are shown side by side with prices "
             "and measured reliability, and ChatGPT (or you) chooses. treg.to does not route or "
             "fail over between them automatically."),
        ],
    },
}


def category_slug(category: str) -> str:
    """`Data enrichment & sales` → `data-enrichment-sales`. The URL segment for a category, and the
    anchor id of its section on the agent pages: one function so they can never disagree."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")


# The use-case pages (the spokes): one per job from USE_CASES that has been reviewed and written.
# Keyed by (category slug, job slug). A job without an entry here has no page — the agent page
# links it into the catalog instead — so a page cannot exist without a person having written its
# sentence and prompt. `label` must match the row in USE_CASES exactly; a test enforces it.
USE_CASE_PAGES: dict[str, dict] = {
    "find-professional-emails": {
        "label": "Find professional emails",
        # The H1, in the buyer's words; the title is built from it plus live catalog numbers.
        # H1 and title carry the words people type ("email finder", "linkedin email finder", "api");
        # the buyer's-words label stays on the menu.
        "sentence": "Email finder API: a work email from a name, company or LinkedIn URL",
        "title": "Email finder API: {n} providers compared | treg.to",
        "lede": (
            "Give your agent a name and a company domain, or a LinkedIn URL, and get back a "
            "verified work address. {n} providers do this job. They differ in what they need as "
            "input, what they charge for a miss, and how they bill. Every row below is callable "
            "right now through one treg.to key, at the provider's rate with no markup."),
        # What to type, per client. One URL, tabs on the page.
        # One prompt, the same in every client. Copy button on the page.
        "prompt": "Using treg, find the work email of the VP of Marketing at stripe.com. Show me "
                  "the price first, then call the cheapest verified provider and tell me the confidence.",
        # Why the prompt works: what to give the agent and what to ask it for. Two or three points.
        # (lead, one short line). Rendered as small cards, not a bullet wall.
        "prompt_why": [
            ("Give it what you have",
             "Name plus company domain works everywhere. A LinkedIn URL works with 3 of the 9."),
            ("Ask for the price first",
             "treg.to returns the cost before the call, so ChatGPT can say what it will spend."),
            ("Name your preference, the agent picks",
             "“cheapest” · “most reliable” · “only bill me when you find one” · “takes a LinkedIn URL”."),
            ("Say what to do on a miss",
             "“If the first returns nothing, try the next cheapest.” That is a waterfall in one line."),
        ],
        # Real questions from Reddit and X, quoted verbatim with a link. Gathered 2026-08-21 via
        # agent-reach over ~180 posts; vendor-written posts were excluded.
        "voices_intro": (
            "The hard part is not finding an address, it is finding one that will not bounce. From "
            "180 posts on Reddit and X in August 2026, these five come up more than anything else."),
        "voices": [
            ("A bounce hurts the sending domain, not just the campaign",
             "A 3% bounce rate from Apollo can torch your sending domain and trigger weeks of warmup.",
             "@dan__rosenthal on X", "https://x.com/i/status/2053188346629820721",
             "Verify before you send. The verification job is one more call at $0.0019 and returns "
             "deliverability plus the mail provider, so you can drop the risky rows instead of "
             "gambling the domain."),
            ("Find rate and validity are different numbers, and vendors advertise the first",
             "low coverage means most of the list never even gets contacted. high bounces hurt deliverability.",
             "r/GrowthHacking", "https://www.reddit.com/r/GrowthHacking/comments/1rle23d/which_email_finder_actually_scales/",
             "This page does not reprint anyone's accuracy claim. It shows the price, what the "
             "provider bills for a miss, and the success rate treg.to measured on live calls."),
            ("Nobody trusts one provider, so everyone builds a waterfall",
             "Most agencies use one email finder, get 45-50% coverage, and immediately lose half their list before even emailing.",
             "@itsalexvacca on X", "https://x.com/i/status/1976301889634566420",
             "All nine are callable through one key, so a miss costs one more call to try the next "
             "one. Tell the agent the order you want; treg.to compares, it does not fail over on "
             "its own."),
            ("The lead is a LinkedIn URL and nothing else",
             "I tried GetProspect and Apollo.io to extract the email address but most of them were wrong.",
             "r/businessemail", "https://www.reddit.com/r/businessemail/comments/1ttn2fd/how_to_search_an_email_address_on_linkedin_easily/",
             "Three providers here take a LinkedIn URL directly (Tomba, Fiber AI, LeadMagic) and "
             "skip name matching entirely. The comparison below marks what each one accepts."),
            ("Coverage claims are indistinguishable, especially outside the US",
             "everyone's marketing site claims continental coverage so i can't tell which one holds at the smaller-domain end",
             "r/SalesOperations", "https://www.reddit.com/r/SalesOperations/comments/1uen0g6/best_email_finder_for_smaller_eu_companies/",
             "No comparison table can answer this honestly. Per-call pricing makes the real test "
             "cheap: run 100 of your own contacts through three providers for a few dollars and "
             "keep the one that holds up."),
        ],
        # Optional: a screenshot of the real answer in the client. Omitted until one exists.
        "result_image": None,
        # The keyword phrasing of the three behind-the-scenes questions, in the page's own words.
        "q_cheapest": "Which email finder API is cheapest?",
        "q_reliable": "Which one is the most reliable?",
        "q_compare": "How do the providers compare?",
        "what_is": (
            "An email finder API takes a person's name and their company's domain (or a LinkedIn "
            "URL) and returns the work email address, usually with a confidence score and the "
            "sources it was seen on. Providers differ in coverage, in what they accept as input, "
            "and in whether they bill per attempt or only per address found."),
        # Hand-written: what actually differs between the providers for THIS job. Reviewed when the
        # catalog changes; kept short on purpose.
        "notes": [
            "Per-success pricing (Hunter, LeadMagic, Tomba, CompanyEnrich) bills only when an address "
            "is found; per-call pricing bills the attempt. On lists with many misses that difference "
            "dominates the bill.",
            "Inputs differ: most take name + company domain. Tomba and Fiber AI also resolve from a "
            "LinkedIn URL, and CompanyEnrich needs its own person id from a prior search, so that "
            "one is a two-step flow.",
            "Confidence scores are not comparable across providers. Treat a 'verified' grade from one "
            "and a 95 from another as different scales, and verify before you send "
            "(see the email verification job).",
        ],
        "faq": [
            ("How much does it cost to find a work email?",
             "Between a fraction of a cent and a few cents per found address, depending on the "
             "provider. The lowest live price is shown at the top of this page, and treg.to adds "
             "no markup. Most providers here charge only on success, so a miss costs nothing."),
            ("Do I need a Hunter or Apollo account?",
             "No. treg.to calls the provider on its own key and bills your team's prepaid balance per "
             "call. If you already have a key for one of them, register it and those calls are "
             "never metered."),
            ("Which provider should I use?",
             "It depends on what you have. Name plus domain: start with the cheapest verified "
             "per-success provider. A LinkedIn URL: use one that accepts it directly. treg.to shows "
             "them side by side but does not choose or fail over for you."),
            ("Is the data legal to use?",
             "These providers return business contact data under their own terms; you are "
             "responsible for how you use it, including consent and anti-spam law in your "
             "jurisdiction. treg.to relays the provider's answer and stores no copy."),
        ],
        "related": ("Verify an email before you send", "Enrich a person from an email or LinkedIn URL",
                    "Find people by role, company or location", "A company's email format"),
    },
}

USE_CASE_PAGES["search-console-queries"] = {
    "label": "Search Console: clicks, impressions and top queries",
    "sentence": "Google Search Console API: clicks, impressions and top queries, read by your agent",
    "title": "Search Console API for {agent}: queries and clicks | treg.to",
    "lede": (
        "Connect the Search Console property you already own and your agent can read the same "
        "numbers the UI shows: every query, its clicks, impressions, CTR and average position, for "
        "any date range. It runs on your own Google account, so treg.to never meters it."),
    "prompt": "Using treg, show me the queries treg.to ranked 8 to 20 for in the last 28 days with "
              "more than 50 impressions, sorted by impressions, and tell me which ones have the worst CTR.",
    "prompt_why": [
        ("Connect once", "One OAuth click for the property you own. treg.to holds the token, not you."),
        ("Ask in plain words", "Date ranges, filters and sorting are the agent's job, not yours."),
        ("End the window 3 days back", "Search Console lags 2 to 3 days; yesterday is preliminary data."),
        ("It costs nothing", "Your own account, so the call is never metered."),
    ],
    "result_image": None,
    "what_is_heading": "What is the Search Console API?",
    "what_is": (
        "The Search Console API returns your site's Google Search performance as data: clicks, "
        "impressions, CTR and average position, broken down by query, page, country, device and "
        "date. It is the same data as the Performance report, without the UI's row limits, and it "
        "is the only first-party source for what people actually searched before they reached you."),
    "notes": [
        "Ending a window on yesterday silently ends it on preliminary data. Score a 28-day window "
        "that ends 3 days back.",
        "The UI caps the query table at 1,000 rows; the API paginates past that, which is where the "
        "long tail lives.",
        "Queries with very low volume are withheld for privacy, so the totals in a query breakdown "
        "will not add up to the site total. That gap is expected.",
    ],
    "faq": [
        ("Does this cost anything?",
         "No. Search Console runs on your own Google account, so treg.to relays the call and meters "
         "nothing. Only calls on treg.to's own provider keys are billed."),
        ("What do I have to connect?",
         "The Google account that already has access to the property, once, through an OAuth "
         "screen. treg.to stores the token server side; your agent never sees it."),
        ("Can my agent see other people's sites?",
         "No. The API returns only the properties your connected account can access."),
        ("Which agents can do this?",
         "Any client that can reach treg.to: ChatGPT, Claude, Claude Code, Cursor and the rest of "
         "the supported list."),
    ],
    "related": ("Is this page indexed, and why not", "Google Analytics: traffic and behaviour reports",
                "Keyword volume, CPC and competition", "Google results for a keyword"),
}

USE_CASE_PAGES["find-creators-by-keyword"] = {
    "label": "Find creators by keyword",
    "sentence": "Find creators by keyword on Instagram, TikTok, YouTube and X",
    "title": "Creator search API: 4 platforms compared | treg.to",
    "lede": (
        "Search each platform's own user index by keyword and get back profiles with follower "
        "counts, bios and links, so your agent can shortlist creators instead of you scrolling. "
        "Each platform is served by its own providers; the comparison below is per platform, "
        "because an Instagram search and a YouTube search are different jobs, not alternatives."),
    "prompt": "Using treg, find 20 TikTok creators posting about home espresso with between 50k and "
              "500k followers. Show me the price first, then give me handles, follower counts and bios.",
    "prompt_why": [
        ("Name the platform", "Each platform has its own index. \"Creators\" alone makes the agent guess."),
        ("Give a keyword, not a topic", "These are keyword searches over profiles, not semantic search."),
        ("Ask for the fields you want", "Follower count, bio and link come back; say so and you get a table."),
        ("Filter after, not during", "Follower ranges are your filter on the results, not a search parameter."),
    ],
    "result_noun": "profile",
    "result_image": None,
    "what_is_heading": "What does a creator search API return?",
    "what_is": (
        "A creator search endpoint queries a platform's own user directory for a keyword and "
        "returns matching profiles: handle, display name, follower count, bio, verification and "
        "profile link. It is the discovery half of influencer research. Engagement rates, contact "
        "details and audience demographics are separate jobs on separate endpoints."),
    "notes": [
        "Follower ranges are not a search filter on any of these platforms. Providers return "
        "keyword matches and your agent filters them, so ask for more results than you need.",
        "A keyword search matches handles, display names and bios, not video or caption text. To "
        "find creators by what they posted, search posts or hashtags instead and take the authors.",
        "Coverage differs per platform, not per provider: TikTok and Instagram indexes are deep, "
        "X's user search is shallow, and YouTube's channel search is keyword-literal.",
    ],
    "faq": [
        ("Can I find creators by follower count?",
         "Not directly. These endpoints search by keyword; ask your agent for a larger result set "
         "and let it filter on the follower counts that come back."),
        ("Does this give me their email?",
         "No. Contact details are a separate job. Some creator bios carry a business email, and the "
         "people enrichment endpoints can resolve a work address from a name and company."),
        ("Which platform should I search?",
         "The one your audience uses. The comparison below shows who serves each platform and what "
         "a search costs there; treg.to does not pick a platform for you."),
        ("Is this the official API?",
         "For most of these platforms it is a data provider reading the public index, not the "
         "platform's own API. Each row names the provider and links its documentation."),
    ],
    "related": ("A creator's profile and stats", "Search posts by keyword", "Posts under a hashtag",
                "Mine the comments"),
}
AGENTS["claude"] = {
    "name": "Claude",
    "h1_noun": "MCP server",
    "title": "Claude MCP server: {n} APIs without keys | treg.to",
    "description": (
        "treg.to gives Claude {n} ready-to-call APIs across {p} platforms: work emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor ads. Priced per call at the provider's own rate with no markup and no provider signup."),
    "definition": (
        "treg.to is an MCP server for Claude that gives it {n} ready-to-call APIs across {p} "
        "platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, ads and e-commerce. "
        "Calls run on treg.to's own keys and are metered from a prepaid balance at the provider's "
        "rate with $0.000 markup. Every new team starts with $1.00 free, and there are no provider "
        "accounts to open."),
    "install_steps": [
        "In Claude, send this in the chat: <code>set up treg &mdash; https://treg.to/llms.txt</code>",
        "It reads that page and sets itself up. If it asks for a key, give it your team token from "
        "the treg.to dashboard (header <code>X-Treg-Token</code>).",
        "Your first team starts with $1.00 of free calls. No card, no subscription, no seats.",
        "Ask for what you want done. Claude searches the catalog, tells you the price, and calls the "
        "endpoint. You never hold a provider key.",
    ],
    "install_image": None,
        "faq": [
            ("Is treg.to free to use in Claude?",
             "Installing is free and every new team starts with $1.00 of calls. After that each call "
             "is metered from the team's prepaid balance at the provider's own rate, with no markup, "
             "no subscription and no seats. Calls on your team's own keys are free."),
            ("Do I need API keys from the providers?",
             "No. treg.to makes the upstream request on its own key and relays the answer, so Claude "
             "never holds a provider credential. If your team already pays for a provider, register "
             "that key and those calls are never metered."),
            ("What does a call cost?",
             "It depends on the job and the provider: from well under a cent for a keyword lookup to "
             "a few cents for a verified work email. treg.to adds $0.000 on top of the provider's "
             "rate, and Claude tells you the price before it spends it."),
            ("Does treg.to pick the provider for me?",
             "No. Where several providers do one job they are shown side by side with prices and "
             "measured reliability, and Claude picks, or you tell it how. treg.to does not route or "
             "fail over between providers automatically."),
        ],
}

AGENTS["claude-code"] = {
    "name": "Claude Code",
    "h1_noun": "MCP server",
    "title": "Claude Code MCP server: {n} APIs, no keys | treg.to",
    "description": (
        "treg.to gives Claude Code {n} ready-to-call APIs across {p} platforms: work emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor ads. Priced per call at the provider's own rate with no markup and no provider signup."),
    "definition": (
        "treg.to is an MCP server for Claude Code that gives it {n} ready-to-call APIs across {p} "
        "platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, ads and e-commerce. "
        "One command registers it, calls run on treg.to's keys at the provider's rate with $0.000 "
        "markup, and your own keys are never metered."),
    "install_steps": [
        "In Claude Code, send this in the chat: <code>set up treg &mdash; https://treg.to/llms.txt</code>",
        "It reads that page and registers treg.to as an MCP server for you. Prefer to do it "
        "yourself? <code>curl -fsSL https://treg.to/install.sh | sh</code> then "
        "<code>treg login</code> and <code>treg mcp install</code>.",
        "Your first team starts with $1.00 of free calls. No card, no subscription, no seats.",
        "Ask for what you want done, or call an endpoint directly with "
        "<code>treg call &lt;endpoint-id&gt;</code>. The price comes back before the spend.",
    ],
    "install_image": None,
        "faq": [
            ("Is treg.to free to use in Claude Code?",
             "Installing is free and every new team starts with $1.00 of calls. After that each call "
             "is metered from the team's prepaid balance at the provider's own rate, with no markup, "
             "no subscription and no seats. Calls on your team's own keys are free."),
            ("Do I need API keys from the providers?",
             "No. treg.to makes the upstream request on its own key and relays the answer, so Claude Code "
             "never holds a provider credential. If your team already pays for a provider, register "
             "that key and those calls are never metered."),
            ("What does a call cost?",
             "It depends on the job and the provider: from well under a cent for a keyword lookup to "
             "a few cents for a verified work email. treg.to adds $0.000 on top of the provider's "
             "rate, and Claude Code tells you the price before it spends it."),
            ("Does treg.to pick the provider for me?",
             "No. Where several providers do one job they are shown side by side with prices and "
             "measured reliability, and Claude Code picks, or you tell it how. treg.to does not route or "
             "fail over between providers automatically."),
        ],
}

AGENTS["cursor"] = {
    "name": "Cursor",
    "h1_noun": "MCP server",
    "title": "Cursor MCP server: {n} APIs, no keys | treg.to",
    "description": (
        "treg.to gives Cursor {n} ready-to-call APIs across {p} platforms: work emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor ads. Priced per call at the provider's own rate with no markup and no provider signup."),
    "definition": (
        "treg.to is an MCP server for Cursor that gives its agent {n} ready-to-call APIs across {p} "
        "platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, ads and e-commerce. "
        "Calls run on treg.to's own keys at the provider's rate with $0.000 markup, from a prepaid "
        "balance that starts with $1.00 free."),
    "install_steps": [
        "In Cursor's agent chat, send: <code>set up treg &mdash; https://treg.to/llms.txt</code>",
        "It reads that page and sets itself up. If it asks for a key, give it your team token from "
        "the treg.to dashboard (header <code>X-Treg-Token</code>).",
        "Your first team starts with $1.00 of free calls. No card, no subscription, no seats.",
        "Ask for what you want done. Cursor searches the catalog, tells you the price, and calls the "
        "endpoint. You never hold a provider key.",
    ],
    "install_image": None,
        "faq": [
            ("Is treg.to free to use in Cursor?",
             "Installing is free and every new team starts with $1.00 of calls. After that each call "
             "is metered from the team's prepaid balance at the provider's own rate, with no markup, "
             "no subscription and no seats. Calls on your team's own keys are free."),
            ("Do I need API keys from the providers?",
             "No. treg.to makes the upstream request on its own key and relays the answer, so Cursor "
             "never holds a provider credential. If your team already pays for a provider, register "
             "that key and those calls are never metered."),
            ("What does a call cost?",
             "It depends on the job and the provider: from well under a cent for a keyword lookup to "
             "a few cents for a verified work email. treg.to adds $0.000 on top of the provider's "
             "rate, and Cursor tells you the price before it spends it."),
            ("Does treg.to pick the provider for me?",
             "No. Where several providers do one job they are shown side by side with prices and "
             "measured reliability, and Cursor picks, or you tell it how. treg.to does not route or "
             "fail over between providers automatically."),
        ],
}

USE_CASE_PAGES["verify-an-email"] = {
    "label": "Verify an email before you send",
    "sentence": "Email verification API: is this address deliverable, before you send",
    "title": "Email verification API: {n} verifiers compared | treg.to",
    "lede": (
        "Hand your agent an address and get back a verdict: deliverable, undeliverable, or the "
        "third bucket every verifier has and each one names differently. {n} providers do this "
        "through one treg.to key, and what separates them is not accuracy claims. It is what they "
        "charge for an answer of “unknown”, which on a real B2B list is about a fifth of it."),
    "prompt": "Using treg, verify these 40 addresses before I send. Show me the price first, then "
              "give me three lists: safe to send, do not send, and unknown with the reason.",
    "prompt_why": [
        ("Verify in one batch", "One list in, one table out. Cheaper to reason about than 40 calls."),
        ("Ask for three buckets", "Valid and invalid are easy. The third bucket is the decision you have to make."),
        ("Ask what a miss costs", "One provider charges nothing for “unknown”. The others bill it as a check."),
        ("Verify close to the send", "Data decays. A check from three weeks ago is not a check."),
    ],
    "result_noun": "check",
    "result_image": None,
    "what_is_heading": "What does an email verification API actually do?",
    "what_is": (
        "It resolves the domain's MX records and opens an SMTP conversation with the receiving "
        "server to ask whether the mailbox exists, without delivering a message. Three answers come "
        "back: the server confirms, the server denies, or the server accepts everything and tells "
        "you nothing. That last case is a catch-all domain, and no provider can resolve it, because "
        "the information does not exist on the wire."),
    "notes": [
        "Only LeadMagic bills nothing for an inconclusive result: it charges 0.25 credits per "
        "definitive verdict and lets “unknown” through free. Icypeas charges per address "
        "tested whether or not the answer is useful. On a list that is a fifth catch-all, that gap "
        "is the whole price difference.",
        "Every provider names the third bucket differently. Hunter returns accept_all, webmail, "
        "disposable and unknown as separate statuses with a score; LeadMagic returns a plain "
        "is_domain_catch_all flag plus the MX provider; Icypeas returns a certainty level. Compare "
        "the buckets, not the headline accuracy number.",
        "Icypeas is asynchronous: you submit the address and read the verdict from a second call. "
        "The other four answer in the same request, which matters when an agent is verifying a "
        "list interactively.",
    ],
    "voices_intro": (
        "Verification is the step everybody agrees on and nobody is happy with. From ~150 Reddit "
        "and X posts in August 2026, after excluding four separate vendor-astroturf clusters, "
        "these are the complaints that recur."),
    "voices": [
        ("The price looks high for what the operation is",
         "zerobounce wants $65 per 10k emails. neverbounce wants $80. hunter wants $100. i built the same thing in n8n for $0.",
         "r/n8n, 214 points", "https://www.reddit.com/r/n8n/comments/1ra50to/zerobounce_wants_65_per_10k_emails_neverbounce/",
         "Fair complaint, and the reason we publish per-check prices side by side rather than per "
         "10k tiers. Through treg.to the same checks run from a fraction of a cent, and you can "
         "compare what each one charges for an inconclusive answer."),
        ("Catch-all domains are a fifth of the list and nobody knows what to do with them",
         "Catch-all domains are about 20% of any B2B list. Most operators throw them away because the bounce risk is real",
         "@DeanFiacco on X", "https://x.com/i/status/2088600390509937006",
         "No provider resolves a true catch-all. What differs is what each one hands back: a "
         "distinct status, a probability, or a shrug. The comparison below names each provider's "
         "third bucket so you can decide once instead of per list."),
        ("An unknown result is the server refusing to answer, not the tool failing",
         "So the verifier returns unknown, or accept-all, or risky depending on the wording. That is not the tool failing.",
         "r/ColdEmailAndSales", "https://www.reddit.com/r/ColdEmailAndSales/comments/1vnjwj3/email_verification_what_catchall_domains_hide/",
         "Exactly right, and it is why “which verifier is most accurate” is the wrong "
         "question. Ask instead who charges you for the shrug: one of these five does not."),
        ("Role addresses and catch-alls get misclassified",
         "also curious if anyone has had issues with verification tools missing role accounts or catch alls. thats been my biggest frustration.",
         "r/Coldemailing", "https://www.reddit.com/r/Coldemailing/comments/1trpu7m/what_email_verifier_do_you_guys_actually_use_for/",
         "Hunter reports role and disposable addresses as their own statuses; the others fold them "
         "in. Whether a role address is worth keeping is genuinely unsettled, so the honest answer "
         "is to keep them separate and decide per campaign."),
        ("Doing it yourself gets your IP blocked",
         "SMTP probing gets your IP blocklisted and the big providers accept everything anyway.",
         "@kumard_3 on X", "https://x.com/i/status/2089979688395624832",
         "Both halves are true. Cloud providers block port 25 and repeated probes from one address "
         "get you listed, which is what you are paying a provider's IP pool for. It also explains "
         "why the unknown bucket exists at all."),
    ],
    "faq": [
        ("Does verification stop bounces?",
         "It removes the addresses that are provably dead, which is most of the risk. It cannot "
         "catch an address that goes stale between the check and the send, or one that a security "
         "gateway rejects at delivery time. Verify close to the send, not weeks before."),
        ("What is a catch-all or accept-all domain?",
         "A domain whose server accepts mail for every address without saying whether the mailbox "
         "exists. No verifier can resolve it. Expect roughly a fifth of a B2B list to land there."),
        ("How much does verification cost here?",
         "A fraction of a cent per check at the provider's own rate, with $0.000 added by treg.to. "
         "The prices and how each provider bills an inconclusive result are in the comparison below."),
        ("Can I run several verifiers over the same list?",
         "Yes, they are all callable through one key, and heavy senders do exactly that because "
         "verifiers disagree on the ambiguous rows. Your agent chains them; treg.to compares the "
         "options but does not route or fail over on its own."),
    ],
    "related": ("Find professional emails", "Enrich a person from an email or LinkedIn URL",
                "A company's email format", "Find people by role, company or location"),
}

USE_CASE_PAGES["enrich-a-person"] = {
    "label": "Enrich a person from an email or LinkedIn URL",
    "sentence": "Person enrichment API: a full profile from an email or LinkedIn URL",
    "title": "Person enrichment API: {n} providers compared | treg.to",
    "lede": (
        "Give your agent an email address or a LinkedIn URL and get back the person: current title, "
        "employer, seniority, location, work history. {n} providers do this through one treg.to "
        "key, and they are not close on price. The same match costs {cheapest} at one and about "
        "eighty times that at another, so what you are really choosing is how much a miss costs you."),
    "prompt": "Using treg, enrich these 20 LinkedIn URLs into a table: name, current title, company, "
              "seniority, location. Show me the price first, and skip anyone whose profile does not resolve.",
    "prompt_why": [
        ("Lead with your strongest key", "Email beats LinkedIn URL beats name plus company. Give the best one you have."),
        ("Ask for the fields, get a table", "Say which columns you want and the agent shapes the response."),
        ("Ask what a miss costs", "Several providers bill nothing when the person does not resolve."),
        ("Re-run the doubtful rows", "A second provider on the same person is one more call, not a second contract."),
    ],
    "result_noun": "match",
    "result_image": None,
    "what_is_heading": "What is a person enrichment API?",
    "what_is": (
        "It takes one identifier you already have, usually a work email or a LinkedIn profile URL, "
        "and returns the structured record behind it: name, current job title, employer, seniority, "
        "department, location and often the full work history. It is the step between knowing "
        "someone exists and knowing whether they are worth contacting."),
    "notes": [
        "Hunter bills conditionally: 0.2 credits only when the email, full name and position all "
        "come back, so a 404 or a partial record is free. LeadMagic and Icypeas are free on a "
        "no-match too. Apollo, by contrast, charges 8 extra credits the moment a mobile number is "
        "revealed, so a default enrichment and a phone-revealing one are different products.",
        "The price spread is the story: the same job runs from a fraction of a cent to about 38 "
        "cents a record. The dear end buys either phone numbers (Lusha's direct dials) or breadth "
        "of coverage (People Data Labs), not better titles. Decide which you are paying for.",
        "Several providers offer a bulk route at a different rate: Ocean.io's bulk lookup is half "
        "the price of its single enrichment and answers synchronously, while its batch enrichment "
        "is asynchronous and returns to a webhook. For fewer than a thousand people the cheap "
        "synchronous route is usually the right one.",
    ],
    "voices_intro": (
        "Enrichment is bought on accuracy claims and judged on what happens six weeks later. From "
        "~180 Reddit and X posts in August 2026, with two large vendor-astroturf rings excluded, "
        "these are the complaints that recur."),
    "voices": [
        ("The record is right when you buy it and wrong when you use it",
         "within weeks everything starts falling apart, emails bounce, titles are wrong, half the people moved companies",
         "r/SalesOperations, 17 points", "https://www.reddit.com/r/SalesOperations/comments/1pph89g/crm_data_enrichment_was_60_garbage_after_3_months/",
         "Nothing here stops decay. What per-call pricing changes is that re-checking a doubtful "
         "row costs a fraction of a cent instead of another annual contract, so you can enrich "
         "close to the moment you act instead of once a quarter."),
        ("Verified does not mean deliverable",
         "data accuracy is all over the place. we're seeing like 20-30% bounce rates even with their “verified” emails",
         "r/CRM", "https://www.reddit.com/r/CRM/comments/1svj17o/zoominfo_vs_cognism_vs_apollo_which_one_and_why/",
         "This page prints no vendor's accuracy claim, because none of them are measured the same "
         "way. Treat enrichment and verification as two steps: enrich, then run the address "
         "through a verifier before you send."),
        ("You pay for the blanks",
         "You pay for the data you ASK for, and a big chunk comes back empty or already dead.",
         "r/b2b_sales", "https://www.reddit.com/r/b2b_sales/comments/1uhqtas/i_compared_how_data_enrichment_tools_actually/",
         "Worth checking per provider, because they differ: Hunter, LeadMagic and Icypeas charge "
         "nothing when nothing resolves. The comparison below shows how each one bills a miss."),
        ("Nobody can check an accuracy claim before signing",
         "Every provider resolves between 94.7% and 100% of domains. That spread is not a buying signal.",
         "r/gtmengineering", "https://www.reddit.com/r/gtmengineering/comments/1vl0kht/independent_open_source_benchmark_of_company/",
         "Agreed, which is why the honest test is your own list. Running 200 of your real contacts "
         "through three providers costs a few dollars here and answers the question for your data "
         "rather than someone's benchmark."),
    ],
    "faq": [
        ("What can I use as input?",
         "A work email or a LinkedIn profile URL works everywhere. Some providers also take a name "
         "plus a company domain, and People Data Labs will accept a name with a location. The "
         "comparison below lists what each one accepts."),
        ("Do I pay when the person is not found?",
         "It depends on the provider, and it is the most useful thing on this page. Hunter charges "
         "only when a complete record comes back; LeadMagic and Icypeas are free on a no-match; "
         "others bill the attempt."),
        ("Why is one provider eighty times the price of another?",
         "The expensive end sells either phone numbers or coverage breadth. If you do not need "
         "direct dials, the cheap end returns the same title and employer."),
        ("Is enrichment the same as finding an email?",
         "No. Enrichment starts from an identifier you already have and fills in the profile; "
         "finding an email starts from a name and a company and resolves the address. They are "
         "separate jobs and separate prices."),
    ],
    "related": ("Find professional emails", "Verify an email before you send",
                "Find people by role, company or location", "Enrich a company from its domain"),
}

USE_CASE_PAGES["people-search"] = {
    "label": "Find people by role, company or location",
    "sentence": "People search API: find people by job title, company or location",
    "title": "People search API: {n} providers compared | treg.to",
    "lede": (
        "Search across companies for the people who match a role, a seniority, a location or a tech "
        "stack, and get back a list your agent can work with. {n} providers do this through one "
        "treg.to key. The trap is the billing unit: some charge per row returned, so an unbounded "
        "search is an unbounded bill."),
    "prompt": "Using treg, find 25 heads of growth at US SaaS companies with 50 to 200 employees. "
              "Show me the price first, keep the result set small, and give me name, title, company and LinkedIn URL.",
    "prompt_why": [
        ("Always cap the result set", "Several providers bill per row. “Find everyone” is a bill, not a query."),
        ("Search first, reveal second", "Some searches are free and only the contact details cost."),
        ("Filter on their fields", "Seniority, department, headcount and location are provider filters, not your post-processing."),
        ("Name the join key", "Company domain resolves cleanly. A company name does not, and returns the wrong people."),
    ],
    "result_noun": "row",
    "result_image": None,
    "what_is_heading": "What is a people search API?",
    "what_is": (
        "It queries a provider's index of working professionals by attributes rather than by name: "
        "job title, seniority, department, company, headcount, industry, location. It answers "
        "“who are the people like this”, where an enrichment API answers “who is this "
        "person”. Most of these return the person without contact details, which you then "
        "resolve separately."),
    "notes": [
        "Search and reveal are priced separately almost everywhere. Apollo's people search is free "
        "precisely because it returns no emails or phone numbers, Hunter's multi-domain search is "
        "free until you unlock an address, and Lusha's search returns masked previews. Budget for "
        "the reveal, not the search.",
        "Per-row billing is where the money goes. People Data Labs charges one credit for every "
        "record in the response, so a size=1000 call is a thousand credits; Icypeas charges 0.02 "
        "credits a row. Both are legitimate, but only one survives an agent that forgets to set a "
        "limit. Cap the result set in the prompt.",
        "The join key decides whether the results are right. LeadMagic and CompanyEnrich want a "
        "company domain; a bare company name is ambiguous and quietly returns people from the wrong "
        "company. Give the domain wherever you have it.",
    ],
    "voices_intro": (
        "The complaints here are less about accuracy than about access: the search that does not "
        "exist, the bill that scales with rows, and the seat licence. From ~180 Reddit and X posts "
        "in August 2026, vendor rings excluded."),
    "voices": [
        ("The primitive people actually want",
         "I need an open search option where I can find people across companies by job title (e.g., 'CTO' or 'Product Manager') and other filters.",
         "Reddit", "https://www.reddit.com/r/u_Icy_Data8505/comments/1n85jhw/people_search_tool_similar_to_clays_people_finder/",
         "That is this job. The comparison below marks which providers support unbounded search by "
         "title and location, and which can only list people at a company you already named."),
        ("Seat pricing locks small teams out",
         "zoominfo has the best mobile numbers by far, but at like 15k/seat minimum they're pricing out anyone who isn't enterprise",
         "r/CRM", "https://www.reddit.com/r/CRM/comments/1svj17o/zoominfo_vs_cognism_vs_apollo_which_one_and_why/",
         "There are no seats here and no minimum. Calls are metered per row or per call from a "
         "shared team balance that starts with $1.00 free, so a hundred-row test costs cents."),
        ("Doing it yourself gets the account banned",
         "scraping twitter directly got my accounts banned pretty fast. linkedin is even worse, flags you almost immediately.",
         "r/openclaw, 72 points", "https://www.reddit.com/r/openclaw/comments/1sft22e/kept_getting_my_accounts_banned_trying_to_get/",
         "The providers here run their own infrastructure, so your accounts are not in the loop. "
         "That is an operational answer, not a legal one: check each provider's terms for your use, "
         "because treg.to relays their answer and makes nothing lawful that was not."),
        ("Coverage outside the US and outside tech is a guess",
         "I've heard of ZoomInfo, Apollo, LeadSquared, IndiaMART but no idea about quality for Indian market specifically.",
         "r/b2bmarketing", "https://www.reddit.com/r/b2bmarketing/comments/1r08jcy/need_recommendations_for_b2b_contact_data/",
         "No comparison table can answer this, and anyone claiming otherwise is guessing. Per-row "
         "pricing makes the real test cheap: run the same 100-row query of your actual market "
         "through three providers and keep the one that holds up."),
    ],
    "faq": [
        ("Do these return email addresses?",
         "Usually not, and that is why the search is cheap or free. You get the person and then "
         "resolve the address with a separate call, which is a separate price."),
        ("How do I stop a search costing more than I expect?",
         "Set a limit. Providers that bill per row charge for every record they return, so a "
         "thousand-row response is a thousand charges. Ask the agent for a small result set first."),
        ("Can I search across all companies, or only within one?",
         "Both exist here, and they are different products. The comparison marks which providers "
         "support open search by title and location and which need a company first."),
        ("What is the cheapest way to build a list?",
         "Search on a free or per-row-cheap provider, filter down to the people you actually want, "
         "then spend on contact details only for those. The two-step flow is why search and reveal "
         "are priced separately."),
    ],
    "related": ("Find professional emails", "Enrich a person from an email or LinkedIn URL",
                "Build a company list by industry, size or tech", "Get a LinkedIn profile"),
}

USE_CASE_PAGES["enrich-a-company"] = {
    "label": "Enrich a company from its domain",
    "sentence": "Company enrichment API: firmographics from a domain",
    "title": "Company enrichment API: {n} providers compared | treg.to",
    "lede": (
        "Give your agent a domain and get the company behind it: industry, headcount, location, "
        "founding year, tech stack, funding, sometimes revenue. {n} providers do this through one "
        "treg.to key. Resolution is a solved problem, so the useful comparison is which fields come "
        "back filled, what a miss costs, and how fast."),
    "prompt": "Using treg, enrich these 30 domains into a table: company name, industry, headcount, "
              "country, founded year and tech stack. Show me the price first, and mark any field that came back empty.",
    "prompt_why": [
        ("Give the domain, not the name", "A domain maps to one company. A name is ambiguous and matches the wrong one."),
        ("Ask for the fields you need", "Some providers price per section requested. Asking for everything costs more."),
        ("Ask it to mark the blanks", "An empty field is information. A quietly guessed one is not."),
        ("Batch when you can", "Several providers have a bulk route at half the single-call price."),
    ],
    "result_noun": "company",
    "result_image": None,
    "what_is_heading": "What is a company enrichment API?",
    "what_is": (
        "It resolves a domain, company name or LinkedIn URL to a structured company record: legal "
        "name, industry, employee count, headquarters, founding year, and depending on the provider "
        "the technology stack, funding history, web traffic and social profiles. It is what turns a "
        "signup email domain into a qualified account."),
    "notes": [
        "Headcount, revenue and industry are inferred by most providers, not observed, and almost "
        "none of them mark which is which. A confidently wrong headcount does more damage in a "
        "scoring model than a blank one, so ask your agent to keep the empties visible.",
        "The billing units are genuinely different products. Akta prices per section of the record "
        "you request; CompanyEnrich charges one credit per call and five more for the workforce "
        "expansion; Hunter charges only when name, size and location all come back; Ocean.io's bulk "
        "lookup is half the price of its single enrichment. The word credit means something "
        "different at every vendor, which is why this page prices everything in dollars per call.",
        "Use the deterministic route when you have a domain. CompanyEnrich's by-domain lookup maps "
        "one domain to exactly one company, while its by-properties route is a fuzzy match at the "
        "same price. The Companies API returns an empty object and charges nothing when a domain "
        "has no company behind it.",
    ],
    "voices_intro": (
        "Everybody wants the same thing here, and the arguments are about what comes back and what "
        "it costs. From ~180 Reddit and X posts in August 2026, with the vendor rings removed."),
    "voices": [
        ("The ask, in the buyer's own words",
         "I'll give it a company name/website and it will return company size, industry, founded, market cap, maybe leadership info, etc.",
         "r/CRM, 17 points", "https://www.reddit.com/r/CRM/comments/1si1jbw/crm_enrichment_apis/",
         "That is exactly this job, and 19 providers do it. The comparison below is about which "
         "fields each one actually fills, because the ask is identical everywhere."),
        ("Resolution rate is not a buying signal",
         "Every provider resolves between 94.7% and 100% of domains. That spread is not a buying signal.",
         "r/gtmengineering", "https://www.reddit.com/r/gtmengineering/comments/1vl0kht/independent_open_source_benchmark_of_company/",
         "Right, so this page does not rank on it. Compare on what a filled record contains, what "
         "an empty one costs you, and the measured latency, which is where the real spread is."),
        ("A guessed field is worse than a blank one",
         "A pipeline that appends fields without validating them just produces confident-looking wrong data, which is worse than an empty field",
         "r/Data_Enrichment", "https://www.reddit.com/r/Data_Enrichment/comments/1vqoj48/what_is_data_enrichment/",
         "Most providers do not distinguish observed from inferred fields, and we will not pretend "
         "otherwise. Ask the agent to keep blanks blank, and treat headcount and revenue as "
         "estimates unless the provider says otherwise."),
        ("Credits are not comparable between vendors",
         "Every vendor on this list uses the word “credit,” and none of them mean the same thing",
         "r/Data_Enrichment", "https://www.reddit.com/r/Data_Enrichment/comments/1vrl2q4/data_enrichment_pricing_2026_august_update/",
         "Which is why every price on this page is in dollars per call, converted from each "
         "provider's own unit at their published rate, with the date we last verified it."),
    ],
    "faq": [
        ("What do I send in?",
         "A domain gives the highest match rate and is deterministic. Most providers also accept a "
         "company name, a LinkedIn URL or a work email, and a few take a stock ticker."),
        ("Which fields can I count on?",
         "Name, domain, industry, location and an employee range come back almost everywhere. Tech "
         "stack, funding, revenue and web traffic are provider-specific, and revenue in particular "
         "is usually modelled."),
        ("What happens when a domain has no company?",
         "It varies, and it is worth knowing before a batch: some return a 404, some an empty "
         "object, and the free-mail domains are refused outright. Several providers do not bill "
         "for a miss."),
        ("Is this how I qualify signups?",
         "Yes, that is the common use: turn the new user's work email domain into firmographics and "
         "route the account. One provider has a route that takes the email address directly."),
    ],
    "related": ("Build a company list by industry, size or tech", "Hiring, headcount and news signals",
                "Find people by role, company or location", "A company's funding rounds"),
}


USE_CASE_PAGES["youtube-transcript-api"] = {
    "label": "Get a video's transcript",
    "sentence": "YouTube transcript API: a video's captions as plain text",
    "title": "YouTube transcript API: {n} providers compared | treg.to",
    "lede": (
        "Give your agent a YouTube URL and get the spoken words back as text, ready to summarise, "
        "search or quote. {n} providers do this through one treg.to key, from {cheapest} a video. "
        "The official YouTube Data API cannot do it for a video you do not own, which is the whole "
        "reason this job has a price at all."),
    "prompt": "Using treg, get the transcript of https://www.youtube.com/watch?v=dQw4w9WgXcQ in "
              "English. Show me the price first, then summarise it into five bullet points.",
    "prompt_why": [
        ("Give it the URL", "Both providers take a watch or Shorts URL. No video id lookup first."),
        ("Name the language", "Ask for a language you know the video carries, or you get an empty result."),
        ("Ask for the price first", "treg.to returns the cost before the call, so the agent can say what it will spend."),
        ("Say what to do with it", "Transcript plus instruction in one turn beats fetching then pasting."),
    ],
    "result_noun": "transcript",
    "result_image": None,
    "voices_intro": (
        "This job has a large and unusually honest literature, because everyone starts by doing it "
        "themselves. From ~180 Reddit and X posts in August 2026, after excluding thirteen vendor "
        "and self-promotion clusters that were roughly half the corpus, these five recur."),
    "voices": [
        ("It works on a laptop and stops working on a server",
         "When I try to run it in the cloud, YouTube seems to block the IP.",
         "r/SaaS, 5 points", "https://www.reddit.com/r/SaaS/comments/1fgjjd1/looking_for_a_saas_tool_to_fetch_youtube_video/",
         "This is the single most repeated failure in the research, and it is structural: the "
         "unofficial route reads from your address, and datacentre ranges get blocked. A call "
         "through treg.to leaves the provider's infrastructure instead. What no table can tell you "
         "is whether a given provider's pool is clear today at your volume, which is why the "
         "observed success rates on this page are measured rather than promised."),
        ("Nobody can promise it still works next month",
         "it still feels like the whole feature could break the moment something changes on YouTube's end",
         "r/sideprojects, 5 points", "https://www.reddit.com/r/sideprojects/comments/1v4zk6t/is_there_a_stable_way_to_pull_youtube_transcripts/",
         "Correct, and no comparison table can tell you otherwise. The honest difference is who owns "
         "the repair: on the unofficial route it is you, at the moment it breaks, and here it is the "
         "provider, with the failure showing up as a billing line rather than an outage."),
        ("The proxy is the real cost of doing it yourself",
         "it needs ip address rotations (becuase youtube blocks transcript scrapers), so I set up a webshare proxy (costs like $3)",
         "r/n8n, 285 points", "https://www.reddit.com/r/n8n/comments/1pd5gbx/turn_any_youtuber_into_an_ai_agent_001run_using/",
         "A fair benchmark, and worth doing the arithmetic against: a proxy is a monthly floor you "
         "pay whether or not you pull a transcript, plus the maintenance. The prices here are per "
         "video with no floor, which wins at low volume and loses at very high volume."),
        ("An agent cannot watch a video, so the transcript is the adapter",
         "What's the best way to have an agent via API in my app auto-generate a transcript from a YouTube video",
         "@waynesutton on X", "https://x.com/i/status/2087026783615168992",
         "This framing was the largest single theme in the research, twelve posts. It is one call "
         "here: the agent turns a URL into text and then does the actual work in the same turn, "
         "which is what the prompt at the top of this page does."),
        ("The DIY integrations people build for this keep not sticking",
         "I built a couple of MCPs using APIs etc, but they didn't work out so well for pulling transcripts.",
         "r/OpenAI, 25 points", "https://www.reddit.com/r/OpenAI/comments/1uq73nr/youtube_transcript_getter_extension_for_obsidian/",
         "Worth being precise about why, because it is not the MCP part. It is that the transcript "
         "source underneath was unofficial. Nothing here changes that for the two paid providers; "
         "what changes is that maintaining it is their job, and you can compare what they charge "
         "for the video where it fails."),
    ],
    "q_cheapest": "Which YouTube transcript API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What is a YouTube transcript API?",
    "what_is": (
        "It returns the caption track YouTube already holds for a video: the auto-generated one the "
        "speech recogniser produced, or the human-uploaded one if the channel added it. You get the "
        "text, usually with timestamps, in SRT, plain text or JSON. It is not transcription; nobody "
        "here is running speech recognition on the audio, so a video with no caption track has "
        "nothing to return."),
    "notes": [
        "Google's own Data API is missing from this comparison on purpose. Its captions.download "
        "method only works on videos the connected account owns, so it cannot read anyone else's, "
        "and that is why a job the platform does for free in the player costs money here.",
        "ScrapeCreators takes a cache_max_age parameter: if a cached copy is newer than the number "
        "of days you pass, it returns that for 0 credits instead of scraping again. On a rerun over "
        "the same videos, this is the difference between paying and not.",
        "Ask for a language the video actually has. ScrapeCreators returns transcript: null when "
        "your two-letter code is missing rather than falling back; TikHub returns the list of "
        "available caption tracks if you send no language code at all, which is the safer first call.",
    ],
    "faq": [
        ("How much does a YouTube transcript cost?",
         "A fraction of a cent per video at the provider's own rate, with $0.000 added by treg.to. "
         "The live prices are in the comparison above, and one provider bills only on success while "
         "the other bills the attempt."),
        ("Why not use youtube-transcript-api myself?",
         "You can, and it works until it does not: the library reads an undocumented endpoint from "
         "your IP, and datacentre ranges get blocked, which is the failure everybody hits at the "
         "point they move off a laptop. These providers run their own IP pools, and the failure "
         "becomes a billing line instead of an outage."),
        ("Does this work on Shorts and live streams?",
         "Shorts, yes; both providers accept a Shorts URL. A live stream only has captions once the "
         "recording is processed, and a video whose channel disabled captions has no track to "
         "return at any price."),
        ("Can I get transcripts in bulk?",
         "Yes, it is one call per video and you tell your agent the list. There is no batch "
         "endpoint, so the cost is linear, and the billing unit in the comparison tells you which "
         "of the two charges for a video that turns out to have no captions."),
    ],
    "related": ("Video details, views and stats", "A video's comments",
                "Transcripts of X and Facebook video posts", "Search videos and channels by keyword"),
}

USE_CASE_PAGES["video-details-views-and-stats"] = {
    "label": "Video details, views and stats",
    "sentence": "YouTube video statistics: views, likes and metadata by video id",
    "title": "YouTube video statistics API: {n} providers | treg.to",
    "lede": (
        "Views, likes, comment count, duration, title, description, tags and publish date, for any "
        "public video. {n} providers do this through one treg.to key, and one of them is Google's "
        "own API on the account you already have, which is free but rationed."),
    "prompt": "Using treg, get the view count, like count and publish date for these 30 YouTube "
              "video ids and put them in a table sorted by views. Show me the price first.",
    "prompt_why": [
        ("Give it video ids", "The v= parameter of the watch URL. Most providers take the id, not the URL."),
        ("Ask for a batch", "Google's API takes 50 ids in one call for the price of one."),
        ("Name the fields you want", "Views, likes and duration live in different parts of the response."),
        ("Say which account to use", "Your own Google connection is free; the paid providers need no account."),
    ],
    "result_noun": "video",
    "result_image": None,
    "voices_intro": (
        "Almost nobody complains about the price of this call. They complain about the daily cap, "
        "and about whether the number they got is today's number. From ~180 Reddit and X posts in "
        "August 2026, with the vendor clusters excluded, these five recur."),
    "voices": [
        ("The free quota runs out in the middle of something",
         "i exceeded youtubes API quota. womp womp. Anyway, it should be back and updating properly at midnight pacific time.",
         "r/Destiny, 575 points", "https://www.reddit.com/r/Destiny/comments/1vafc5k/dave_comment_tracker_i_made/",
         "The daily budget resets at midnight Pacific, so a bug in the morning costs the rest of the "
         "day. Reading a video costs 1 unit of 10,000 and batches 50 ids into that one unit, so most "
         "people who hit the wall were spending it on search. The paid rows have no daily ceiling; "
         "treg.to shows you both and you pick, it does not switch over on its own."),
        ("You cannot tell whether you ran out or something else broke",
         "The strange thing is, my daily usage counter (which I'm tracking in the script) shows that I'm nowhere near the daily quota limit.",
         "r/pythontips, 2 points", "https://www.reddit.com/r/pythontips/comments/1epf9dk/youtube_api_quota_issue_despite_not_reaching_the/",
         "The recurring complaint is not running out, it is not knowing why. We cannot fix Google's "
         "quota accounting. What a metered call gives you instead is a definite price before it goes "
         "and a definite outcome after, so the ambiguous middle disappears."),
        ("Leaving your laptop changes the failure mode",
         "Apparently that IP address is for AWS.",
         "r/MailChimp, 6 points", "https://www.reddit.com/r/MailChimp/comments/1g9ft0z/youtube_api_error/",
         "The error underneath that thread is API_KEY_IP_ADDRESS_BLOCKED, and it is the same story as "
         "the transcript job: it worked until it was deployed. A call through treg.to leaves the "
         "provider's infrastructure, not your host's. Whether your own host is blocked is a property "
         "of your host, and no comparison table has that column."),
        ("The counts themselves are not stable at fine granularity",
         "during certain intervals, views increase without likes/comments scaling proportionally, while at the hourly aggregate level everything looks perfectly normal",
         "r/HiddenTrueCrimeChat, 23 points", "https://www.reddit.com/r/HiddenTrueCrimeChat/comments/1prow46/ive_been_logging_htc_youtube_views_likes_and/",
         "This is the honest limit of every row on this page. View counts are aggregated and "
         "sometimes revised downward by YouTube itself, and every provider here is downstream of "
         "that. A table can compare price and measured success rate; it cannot tell you whose number "
         "is right, because on some hours no number is."),
        ("The folk remedy is more keys, which is not a plan",
         "Google youtube API has a quota limits, so I added over 45 API",
         "r/MetaRayBanDisplay, 4 points", "https://www.reddit.com/r/MetaRayBanDisplay/comments/1tyuxrx/viewtube_v20_question_for_all_google_youtube_api/",
         "Forty-five keys is a lot of Google projects to own, and it is the shape people reach for "
         "when the only lever is a daily cap. The alternative here is a per-call price with no cap "
         "and no rotation to maintain. Spend the free quota first if you have it."),
    ],
    "q_cheapest": "Which YouTube video stats API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What does a YouTube video stats API return?",
    "what_is": (
        "One video's public record: the snippet (title, description, tags, channel, publish date), "
        "the statistics (view count, like count, comment count) and the content details (duration, "
        "definition, caption availability). It is the same data the watch page shows, as JSON. "
        "Watch time, impressions, click-through rate and audience retention are not here; those are "
        "YouTube Analytics, and only the channel owner can read them."),
    "notes": [
        "Google's Data API charges 1 quota unit whether you pass one video id or fifty, out of a "
        "default 10,000 units a day. Batching is not an optimisation here, it is a fifty-fold "
        "difference in what a day's quota buys you.",
        "The billing units are not comparable. Bright Data bills per record delivered, TikHub and "
        "Just One API bill per successful call, ScrapeCreators bills the call whether or not it "
        "found the video. A run over a list with dead ids costs differently on each.",
        "Like counts are returned as the channel chose to expose them, and a channel that hides its "
        "like count returns no field rather than a zero. Treat a missing field and a zero as "
        "different answers when you aggregate.",
    ],
    "faq": [
        ("Is the YouTube Data API free?",
         "Yes, on your own Google account, and treg.to never meters a call on your own key. What it "
         "is not is unlimited: 10,000 quota units a day by default, which this method spends 1 at a "
         "time. The paid providers exist for when that runs out."),
        ("Can I get watch time or retention?",
         "No. Those are YouTube Analytics numbers and only the channel's owner can read them, "
         "through a connected account. Everything on this page is the public record of the video."),
        ("Do I need a Google account for this?",
         "Only for the free row. The other providers are called on treg.to's keys and billed per "
         "call from your prepaid balance, so you can read a video's stats with no Google project at all."),
        ("How current are the view counts?",
         "They are what the platform is publishing at the moment of the call. YouTube itself updates "
         "public view counts on its own schedule, so two providers reading a minute apart can "
         "legitimately disagree on a fast-moving video."),
    ],
    "related": ("Get a video's transcript", "A video's comments",
                "A channel's profile and lifetime stats", "Search videos and channels by keyword"),
}

USE_CASE_PAGES["youtube-channel-stats"] = {
    "label": "A channel's profile and lifetime stats",
    "sentence": "YouTube channel stats API: subscribers, total views and profile",
    "title": "YouTube channel stats API: {n} providers | treg.to",
    "lede": (
        "Subscriber count, lifetime views, video count, description, country and links, for any "
        "public channel. {n} providers do this through one treg.to key, including Google's own API "
        "on your account, which resolves an @handle without you having to find the UC id first."),
    "prompt": "Using treg, get the subscriber count, total views and video count for @MrBeast and "
              "@mkbhd, and tell me which has more views per video. Show me the price first.",
    "prompt_why": [
        ("A handle is enough", "Google's API resolves @handle directly. The scrapers vary; some need the UC id."),
        ("Ask for the pair you need", "Profile and statistics are separate parts of the response. Name both."),
        ("Ask it to do the arithmetic", "Views per video and subscribers per video are one line, not a spreadsheet."),
        ("Say whose key to use", "Your own Google connection is free and never metered."),
    ],
    "result_noun": "channel",
    "result_image": None,
    "voices_intro": (
        "Two things stop people here, and neither is the price: how fresh the numbers are, and "
        "whether touching the API can hurt the channel they already run. From ~180 Reddit and X "
        "posts in August 2026, with the vendor clusters excluded."),
    "voices": [
        ("One response, and its fields disagree about how current they are",
         "So one field in the response was stale while the other two were fresh.",
         "r/googlecloud, 1 point", "https://www.reddit.com/r/googlecloud/comments/1tjofun/youtube_data_api_v3_channelstatisticsviewcount/",
         "Documented day by day in that thread: lifetime viewCount frozen for over a day while "
         "subscriberCount and videoCount kept moving, inside Google's own API. Every provider on "
         "this page reads from the same well, so no comparison table can rank them on whether a "
         "number is today's. Record when you pulled and treat a flat total as suspect, not as news."),
        ("People are afraid the API itself will hurt their channel",
         "Is there any risk that my main channel could be shadowbanned, restricted, or lose its algorithmic reach just by using the official API",
         "r/youtube, 3 points", "https://www.reddit.com/r/youtube/comments/1vlva1i/question_regarding_youtube_data_api_v3_readonly/",
         "A fair question that almost nobody answers in writing. Reading public channel data through "
         "one of the paid providers here happens server side on a credential that has nothing to do "
         "with your creator account, so there is no account of yours in the request at all. If you "
         "use the free Google row instead, that is your connected account, by design."),
        ("Quota is why people stop tracking channels, not why they start",
         "spending more api quota to start tracking a third channel from scratch",
         "r/HiddenTrueCrimeChat, 20 points", "https://www.reddit.com/r/HiddenTrueCrimeChat/comments/1q1eryk/are_htcs_views_real_what_im_measuring_with_public/",
         "A researcher dropped a control channel because a third one cost more quota than they had. "
         "A channel read is 1 unit, so the free cap is roughly 10,000 snapshots a day; what actually "
         "burns it is polling often. Paid rows have no daily ceiling, which is the whole difference "
         "for anything long running."),
        ("Whether you may keep and show the data is a separate question",
         "Can I legally use YouTube channel profile pictures and public stats from the YouTube API in a collectible card game?",
         "r/legaladvice", "https://www.reddit.com/r/legaladvice/comments/1v6c5ni/can_i_legally_use_youtube_channel_profile/",
         "No comparison table can answer this and we are not going to pretend otherwise. Public and "
         "reusable are different things; storing, republishing and putting someone's avatar on a "
         "product are governed by YouTube's terms and by the law where you are. Read the terms, "
         "and take advice if money is involved."),
        ("Nobody wants a channel endpoint, they want the channel",
         "I want to archive everything from the videos, to the likes, view and comments on it, lives and the same for it",
         "r/DataHoarder, 20 points", "https://www.reddit.com/r/DataHoarder/comments/1s426uf/need_help_with_youtube_channel_archivescraping/",
         "That is four jobs chained: the profile, the uploads list, per video stats, then comments. "
         "Asking Google for part=contentDetails hands you the uploads playlist id, which is the "
         "cheap way into the catalogue without spending a search. Your agent runs the chain; "
         "treg.to prices each step."),
    ],
    "q_cheapest": "Which YouTube channel stats API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What counts as a channel's lifetime stats?",
    "what_is": (
        "The three totals a channel publishes about itself: subscribers, views across every video, "
        "and how many public videos it has. Alongside them come the profile fields: title, "
        "description, custom URL, country, keywords, banner and thumbnail. These are cumulative "
        "totals, not a time series, so measuring growth means reading them repeatedly and keeping "
        "your own history."),
    "notes": [
        "The three providers disagree about what identifies a channel. Google's API takes exactly "
        "one of mine, id or forHandle, so an @handle resolves in a single call; ScrapeCreators "
        "accepts a channel id, a handle or a URL; TikHub needs the UC channel id, so a handle costs "
        "you a lookup first.",
        "Subscriber counts come back as YouTube publishes them, which is rounded once a channel is "
        "past a thousand. Two providers reading the same channel will agree on the rounded "
        "figure and neither has the exact one, because the platform does not expose it.",
        "Asking Google for part=contentDetails also returns the uploads playlist id, which is how "
        "you list a channel's videos without spending a search call. It is the cheapest path from a "
        "channel to its catalogue by a wide margin.",
    ],
    "faq": [
        ("Can I get a channel's subscriber history?",
         "Not from any of these. They return the current totals, because that is all YouTube "
         "publishes. A growth curve means calling on a schedule and storing what you get."),
        ("How do I find a channel by name?",
         "Search for it first, then read the profile. Channel search is a separate job on this menu "
         "and returns the ids these endpoints take."),
        ("Is Google's API really free here?",
         "Yes. It runs on the Google account you connect, so treg.to relays the call and meters "
         "nothing. Only calls on treg.to's own provider keys are billed."),
        ("Can I read another channel's analytics?",
         "No, and no provider can. Watch time, traffic sources and audience data are visible only to "
         "the channel's owner through a connected account. Everything here is public."),
    ],
    "related": ("Video details, views and stats", "Search videos and channels by keyword",
                "A creator's profile and stats", "Get a video's transcript"),
}

USE_CASE_PAGES["search-videos-and-channels-by-keyword"] = {
    "label": "Search videos and channels by keyword",
    "sentence": "YouTube search API: find videos and channels by keyword",
    "title": "YouTube search API: {n} providers compared | treg.to",
    "lede": (
        "Run a YouTube search from your agent and get the results as data: titles, video ids, "
        "channels, publish dates and thumbnails, with the filters the site itself offers. {n} "
        "providers do this through one treg.to key. Google's own API is free on your account and "
        "the single most quota-expensive call it has, which is why the others are here."),
    "prompt": "Using treg, search YouTube for videos about home espresso uploaded in the last month, "
              "sorted by view count. Show me the price first, then give me the top 20 with links.",
    "prompt_why": [
        ("Say what to search for", "A keyword, the way you would type it into the site's own box."),
        ("Name the filters", "Upload date, duration, type and sort order are parameters on every provider."),
        ("Ask for videos or channels", "The same query returns either. Say which, or you get a mix."),
        ("Ask for stats separately", "Search results carry no view counts. The agent fetches those next."),
    ],
    "result_noun": "result",
    "result_image": None,
    "voices_intro": (
        "This is the job where the free route runs out first, and the people who have hit it are "
        "unusually precise about why. From ~180 Reddit and X posts in August 2026, with the vendor "
        "clusters excluded."),
    "voices": [
        ("One number decides this whole page",
         "The official Data API v3 search.list costs 100 units/call against a 10k/day quota, which dies almost immediately once you're polling multiple keyword combos",
         "r/webscraping, 7 points", "https://www.reddit.com/r/webscraping/comments/1uaq3lk/keywordsearching_youtube_at_scale_official_api_vs/",
         "Exactly right, and worth stating as arithmetic: 100 units a search against a 10,000 unit "
         "day is about 100 searches for the entire project, before you spend anything hydrating the "
         "results with view counts. Every other call on this page costs 1 unit. That gap is the "
         "reason the paid rows on this page exist."),
        ("Trading a hard cap for an unknown one",
         "Roughly what request rate gets you rate-limited / soft-banned on the InnerTube route?",
         "r/webscraping, 7 points", "https://www.reddit.com/r/webscraping/comments/1uaq3lk/keywordsearching_youtube_at_scale_official_api_vs/",
         "Nobody in the research knew, including the person who asked, and we do not either. What we "
         "can say is that the request leaves the provider's infrastructure rather than yours, and "
         "that the measured success rates shown here are live traffic rather than a claim. That is the "
         "honest version of an answer to this question."),
        ("Search results are not what a person sees",
         "I want an API or scraper which helps me replicate the same results when I do a search on mobile. Is there a way?",
         "r/n8n, 1 point", "https://www.reddit.com/r/n8n/comments/1rkmsj4/youtube_scraper/",
         "No, not exactly, from anyone. On the site the ranking is personalised, regional and "
         "signed in; these calls are none of those. Pass a region and language to get nearer a "
         "market. No comparison table can tell you whose ordering matches your users, so run your "
         "own query through two providers for a cent and diff the two lists."),
        ("An agent will write the expensive version by default",
         "The problem is my playlist contains 1.2k songs but I can only transfer around 60 songs and after that its an error",
         "r/learnpython", "https://www.reddit.com/r/learnpython/comments/1hk199a/help_needed_exceeded_youtube_api_quota_while/",
         "ChatGPT wrote them a loop that runs one search per track, which at 100 units each is a dead "
         "project at song 100. This is the failure this page is really about: the code is fine and "
         "the quota model is what nobody read. Tell your agent the price first and it can tell you "
         "the run is unaffordable before it starts."),
        ("The actual job is a sweep, not a search",
         "So I built a Python script to scrape YouTube Shorts from a specific niche (AI kids bedtime stories), pull stats, transcripts",
         "r/shortsAlgorithm, 78 points", "https://www.reddit.com/r/shortsAlgorithm/comments/1rh2bek/i_scraped_53_youtube_shorts_from_a_single_niche/",
         "Search, then details, then transcripts, over a niche, on a schedule. That is three jobs on "
         "this menu chained, and it is what almost everyone doing this actually wants. Per call "
         "pricing makes the sweep something you can size in advance instead of a quota you discover "
         "the edge of."),
    ],
    "q_cheapest": "Which YouTube search API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What does a YouTube search API return?",
    "what_is": (
        "A page of search results as structured records: video id, title, description snippet, "
        "channel name and id, publish date and thumbnails, plus a token to fetch the next page. It "
        "is the site's own ranking, not a fresh index, so results move as YouTube's does and two "
        "calls minutes apart can legitimately differ."),
    "notes": [
        "Search results carry no statistics anywhere. To rank what you found by views you take the "
        "video ids and call the video details job, which on Google's own API costs 1 quota unit for "
        "50 ids. Budget for two steps, not one.",
        "Google's search method costs 100 quota units against a default 10,000 a day, so about 100 "
        "searches exhausts a day for the whole project. Every other call on this page costs 1. That "
        "single number is why paid search providers exist at all.",
        "SerpApi's parameter is search_query, not q. Sending q to its YouTube engine returns nothing "
        "rather than an error, which is the kind of failure that looks like an empty result set.",
    ],
    "faq": [
        ("Why does YouTube search cost so much quota?",
         "Google prices the search method at 100 units against a 10,000 unit daily default, roughly "
         "100 searches a day for an entire project. Reading a video, a channel or a comment thread "
         "costs 1. A quota increase means an application and a review."),
        ("Can I search a single channel's videos?",
         "Yes. Google's search takes a channelId, and one TikHub endpoint searches within a channel "
         "by id. The comparison above names which endpoint does which."),
        ("Do search results include view counts?",
         "No, on any provider. That is a second call to the video details job, which is cheap and "
         "batches, so ask your agent for both and it will chain them."),
        ("Is this the same ranking a user sees?",
         "Close, but not guaranteed. Results are personalised and regional on the site, and these "
         "calls are not signed in as you. Pass a region and language to get nearer a given market."),
    ],
    "related": ("Video details, views and stats", "A channel's profile and lifetime stats",
                "Trending videos", "Find creators by keyword"),
}

USE_CASE_PAGES["youtube-video-comments"] = {
    "label": "A video's comments",
    "sentence": "YouTube comment scraper: every comment on a video, as data",
    "title": "YouTube comment scraper API: {n} providers | treg.to",
    "lede": (
        "Pull a video's comments with authors, like counts, timestamps and replies, so your agent "
        "can read the audience instead of you scrolling. {n} providers do this through one treg.to "
        "key, and Google's own API is free on the account you already have."),
    "prompt": "Using treg, get the comments on https://www.youtube.com/watch?v=dQw4w9WgXcQ sorted by "
              "relevance. Show me the price first, then group them into the five things people complain about.",
    "prompt_why": [
        ("Give it the video", "A URL or a video id, depending on the provider. Both are one field."),
        ("Choose the sort", "Relevance surfaces the comments people upvoted. Time gets you the newest."),
        ("Ask for the analysis, not the dump", "A thousand comments is not an answer. Ask for the themes."),
        ("Say how deep to go", "Replies are nested and paginated. Top-level threads are usually enough."),
    ],
    "result_noun": "comment",
    "result_image": None,
    "voices_intro": (
        "The people doing this job are not chasing volume, they are chasing the forty comments that "
        "matter. From ~180 Reddit and X posts in August 2026, with the vendor clusters excluded, "
        "these five are what they actually complain about."),
    "voices": [
        ("The data is easy, the filtering is the job",
         "a video with 3,000 comments might have 40 that are actually useful to me and the rest is noise",
         "r/claude, 3 points", "https://www.reddit.com/r/claude/comments/1ragfjd/im_building_a_youtube_comment_filtering_tool_with/",
         "The most useful document in the whole research pass. They are on version six of that "
         "filter and get about 50% agreement with their own judgment. No provider on this page fixes that, "
         "and any that claims to is selling you something: getting the comments is the cheap half."),
        ("YouTube gives you no way to search or export a comment section",
         "I find myself wanting to search youtube comments all the time. Because youtube lacks this feature (for some reason)",
         "r/SideProject, 2 points", "https://www.reddit.com/r/SideProject/comments/1uzurwc/made_a_free_chrome_extension_that_lets_you_search/",
         "True, and it is the whole reason this job exists. Every provider here hands back the "
         "comments as records with author, likes and timestamps, so searching, sorting and grouping "
         "them becomes something your agent does rather than something the site has to offer."),
        ("Quota is what stops a long-running tracker",
         "it would also mean spending more api quota to start tracking a third channel from scratch",
         "r/HiddenTrueCrimeChat, 20 points", "https://www.reddit.com/r/HiddenTrueCrimeChat/comments/1q1eryk/are_htcs_views_real_what_im_measuring_with_public/",
         "A researcher dropping a control channel because a third one costs more quota than they have "
         "is the clearest argument on this page. Google's route is free and rationed; the paid rows "
         "have no daily ceiling, and you can mix them, spending quota first and paying past it."),
        ("What comes back is not stable between two pulls",
         "How has youtube given me the wrong comments section on a video bro 💀",
         "r/Quadeca, 19 points", "https://www.reddit.com/r/Quadeca/comments/1mhjev8/how_has_youtube_given_me_the_wrong_comments/",
         "Comment sections move: held-for-review, author-deleted and creator-hidden comments differ "
         "between two calls minutes apart, and the sort you ask for changes which arrive first. No "
         "provider here, and no comparison table, can tell you that you got all of them. Pin the "
         "sort order and record when you pulled."),
        ("People want the whole channel, not one video",
         "I want to archive everything from the videos, to the likes, view and comments on it, lives and the same for it",
         "r/DataHoarder, 20 points", "https://www.reddit.com/r/DataHoarder/comments/1s426uf/need_help_with_youtube_channel_archivescraping/",
         "That is several jobs chained, not one call: list the channel's videos, then pull comments "
         "per video, then paginate each. Your agent can run that loop and treg.to prices every "
         "step, but it does not run the loop for you, and on a large channel the bill is the sum of "
         "the parts."),
    ],
    "q_cheapest": "Which YouTube comment API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What does a YouTube comment scraper return?",
    "what_is": (
        "The public comment threads on a video: each comment's text, author name and channel, like "
        "count, published and updated times, and the replies underneath it. Comments are paginated, "
        "so a video with tens of thousands of them is many calls, and the order you ask for changes "
        "which ones arrive first."),
    "notes": [
        "Google's commentThreads method returns top-level threads with up to 5 replies inlined. "
        "Past that you fetch the rest by parent id, so a thread with hundreds of replies is a "
        "second and third call rather than a deeper response.",
        "A video with comments turned off answers 403 commentsDisabled on Google's API rather than "
        "an empty list. Treat that as a distinct outcome; the scraping providers signal it "
        "differently, and on a per-call biller you have still paid for the attempt.",
        "Bright Data bills per comment delivered, not per video. On a heavily commented video that "
        "is a materially different bill from a per-call provider, in either direction "
        "depending on how many you actually pull.",
    ],
    "faq": [
        ("Can I download all the comments on a video?",
         "Yes, by paginating. It is one call per page on every provider here, so a heavily "
         "commented video costs proportionally more. The comparison shows the per-call and "
         "per-record rates side by side."),
        ("Does this include replies?",
         "Top-level threads come back with their first replies attached. Deeper replies are a "
         "further call per thread, which is worth knowing before you ask an agent for everything."),
        ("Is scraping YouTube comments allowed?",
         "The comments are public and Google's own API serves them, which is the route this page "
         "recommends first. The third-party providers read the public page; you are responsible for "
         "what you do with the data, including the privacy law that applies to you."),
        ("Can I get comments from a whole channel?",
         "Not in one call. List the channel's videos first, then fetch comments per video. Your "
         "agent can chain that; treg.to prices each step but does not run the loop for you."),
    ],
    "related": ("Get a video's transcript", "Video details, views and stats",
                "Mine the comments", "A channel's profile and lifetime stats"),
}


USE_CASE_PAGES["google-results-for-a-keyword"] = {
    "label": "Google results for a keyword",
    "sentence": "SERP API: Google organic results for a keyword",
    "title": "SERP API: {n} providers compared, from {cheapest} | treg.to",
    "lede": (
        "Send a keyword, get Google's organic results back as data: the ranking URLs in order, with "
        "their titles and snippets. {n} providers do this through one treg.to key, from {cheapest} a "
        "call, billed from a prepaid balance instead of a monthly plan. What you are really choosing "
        "between is the billing unit, how deep each one goes, and how much of the page beyond ten "
        "blue links it can see."),
    "prompt": "Using treg, get the top 10 Google organic results for best crm for startups in the "
              "United States, in English. Show me the price first, then list the ranking domains "
              "with their titles.",
    "prompt_why": [
        ("Give it the keyword, not a URL", "These take a query the way a person types it. Keep multi-word phrases together."),
        ("Say where and in what language", "A result set is location specific. Leave it out and you get whichever default the provider picked."),
        ("Say how deep to go", "Depth is the cost dial on every row here: ten results is one unit of work, a hundred is ten."),
        ("Ask for the price first", "treg.to returns the cost before the call, so the agent can say what it will spend."),
    ],
    "result_noun": "result",
    "result_image": None,
    "voices_intro": (
        "From roughly 180 Reddit and X posts read in August 2026. The X half was almost entirely "
        "promotion; Reddit carried the signal, and thirteen posts from four confirmed vendor rings "
        "were excluded, including one account running the same question through three scraping "
        "subreddits with a different persona each time. These five are organic."),
    "voices": [
        ("An agent search API and a SERP API are not the same product",
         "My current take is that most “search APIs” are really context providers for LLM grounding.",
         "r/AI_Agents, 2 points", "https://www.reddit.com/r/AI_Agents/comments/1urn878/highrecall_web_search_apis_for_agent_data/",
         "This was the most useful distinction in the whole research pass, and it decides whether "
         "this page is for you. Tools that return cleaned passages for a model to read are solving "
         "grounding. The rows here return the result set itself, in order, with the domains that "
         "hold each position. If you need to know who ranks where, cleaned context will not tell "
         "you, and if you only need something true to quote, you are overpaying here."),
        ("There is no pay as you go, and that is the objection",
         "What bothers me mostly is that there's no pay-as-yo-go option for SerpAPI or Firecrawl as far as I can tell.",
         "r/TypingMind, 3 points", "https://www.reddit.com/r/TypingMind/comments/1qri4jd/help_needed_customization_of_typingmind_deep/",
         "The complaint that came up most often was about the shape of the bill rather than the "
         "size of it: a monthly plan is the wrong instrument for someone running twenty searches a "
         "day. Every row here is charged per call from a prepaid balance, at the provider's own "
         "rate with nothing added, so the honest counterpoint is that at that volume the price "
         "differences between providers are noise and the real cost is your setup time."),
        ("Google stopped serving a hundred results on one page",
         "The removal created a 10x increase in the workload for data collection.",
         "r/AgentsOfAI, 204 points", "https://www.reddit.com/r/AgentsOfAI/comments/1nxut66/google_trying_to_retain_its_search_engine_monopoly/",
         "This is the single best explanation of why a cheap looking per search rate can still "
         "surprise you. When one fetch stopped returning a hundred results, reading the top hundred "
         "became ten fetches, and every provider passes that through somewhere: as depth billed in "
         "tens, as rows, or as ten separate searches. Read the unit column above before the price "
         "column."),
        ("The free tier runs out before the project starts",
         "APIs like SerpAPI have pretty limited free tiers, so I'm looking into alternative ways to handle search without running into those limits.",
         "r/Playwright, 2 points", "https://www.reddit.com/r/Playwright/comments/1u8h42h/google_search_using_playwright/",
         "A new treg.to team starts with a dollar of prepaid balance and no card, which at these "
         "rates is hundreds of searches rather than a fortnight of a trial. What that does not buy "
         "you is a decision: the same dollar spread across three of these providers on your own "
         "queries is a better answer than any table, and it is the experiment this page would "
         "rather you ran."),
        ("Every thread on this question is full of vendors",
         "please be understandable and provide your take, examples and opinion, not just straight up promo bs",
         "r/ProxyEngineering, 10 points", "https://www.reddit.com/r/ProxyEngineering/comments/1vrjmy7/what_are_some_good_serpapi_alternatives/",
         "This page is one of those threads, so the only decent thing it can do is show its working. "
         "Every price above is read from the provider's own rate card or documentation on the date "
         "in the row, where a provider publishes no dollar price the row says so rather than "
         "guessing, and any reliability figure comes from live treg.to traffic rather than a "
         "benchmark we designed. What no comparison can tell you is which of these companies still "
         "exists in eighteen months. Nobody selling you a table knows that either."),
    ],
    "q_cheapest": "Which SERP API is cheapest?",
    "q_reliable": "Which SERP API is the most reliable?",
    "q_compare": "How do the SERP APIs compare?",
    "what_is_heading": "What is a SERP API?",
    "what_is": (
        "A SERP API returns what Google returns for one query, already parsed: the organic results "
        "in order, with titles, URLs and snippets. You are not renting an index, you are paying for "
        "one search run somewhere else on the parameters you send. That is why it is priced per "
        "search rather than per month, and why the parameters that change what a person would have "
        "seen, the location, the language, the device and how far down the page you go, are also "
        "the parameters that change what you are billed."),
    "notes": [
        "The billing unit is not the same across these rows, so the cheapest number is not the "
        "cheapest provider for your job. DataForSEO charges a flat rate per request but bills depth "
        "in tens of results, and multiplies the price by five if you use advanced search operators. "
        "Serpstat bills one credit per returned row and still bills the one credit minimum when the "
        "result is empty. SerpApi bills per successful search and charges nothing for a failure or "
        "for a repeat it serves from cache.",
        "Semrush shows no dollar price here on purpose. It prices in API units, ten per line "
        "returned, bought in packages up front, so a full top hundred pull is a thousand units and "
        "what that costs depends on the package you bought rather than on a public per call rate. "
        "The comparison prints a price only where the provider publishes one.",
        "SerpApi appears in the catalog twice and the difference matters. Its light endpoint returns "
        "a trimmed, faster payload for when you only want the ranking URLs, while its full endpoint "
        "accepts the whole parameter surface, including an explicit location or coordinates, device, "
        "the search vertical and a starting offset, which is what you need for city level results or "
        "for anything past the first page.",
    ],
    "faq": [
        ("How much does one Google search cost?",
         "The provider's own rate with $0.000 added by treg.to, taken from a prepaid balance rather "
         "than a subscription. The live figure per provider is in the comparison above, and the "
         "units differ: one row bills the request, one bills each returned row, one bills only the "
         "searches that succeed."),
        ("Is this the same as Tavily, Exa or Firecrawl?",
         "No, and conflating the two is the commonest mistake in the research behind this page. "
         "Those return cleaned text for a model to read. These return the Google result set itself, "
         "in position order, with the domains holding each slot. If the question is who ranks, you "
         "need this kind; if the question is what is true, you probably do not."),
        ("Why does reading the top 100 cost more than it used to?",
         "Because Google stopped serving a hundred results on a single page, so what was one fetch "
         "became ten. Providers pass that through as depth, as rows, or as separate searches, which "
         "is why the depth or size parameter is the real cost dial on every row here."),
        ("Can I get results for one city rather than the whole country?",
         "Yes, on the rows that take it. SerpApi accepts a location string or explicit coordinates "
         "with a radius, and DataForSEO accepts a location code or name. A national result set is "
         "not a local one, so if you are checking a local pack you have to say where you are "
         "standing."),
    ],
    "related": ("Keywords a domain ranks for", "Keyword volume, CPC and competition",
                "Backlink profile of a domain", "How AI answers mention your brand"),
}


USE_CASE_PAGES["your-own-campaign-performance"] = {
    "label": "Your own campaign performance",
    "sentence": "Google Ads API and Meta Ads API: your own campaign numbers",
    "title": "Google Ads API and Meta Ads API, free | treg.to",
    "lede": (
        "Spend, impressions, clicks and conversions for the campaigns you are already running, read "
        "by {agent} off your own connected accounts. Both platforms are free through treg.to: you "
        "connect once, the token stays server side, and nothing here is metered. The research behind "
        "this page says the wall was never the endpoint. It was getting a credential in front of it."),
    "prompt": "Using treg, pull last month's spend, impressions, clicks and conversions for every "
              "campaign in my Google Ads and Meta Ads accounts, then tell me which three lost the "
              "most money against a target of $40 per conversion.",
    "prompt_why": [
        ("Name the window", "Both take an explicit range. Ask for last month and you get last month, not lifetime to date."),
        ("Ask for both platforms in one turn", "They are two calls, not two conversations. One turn gets you one table you can compare."),
        ("Say which level you want", "Meta answers at account, campaign, ad set or ad. Pick one, or the totals will not mean what you think."),
        ("Say what the answer is for", "Losing money is a judgement, not a field. Give it the threshold and it does the arithmetic."),
    ],
    "result_noun": "row",
    "result_image": None,
    "voices_intro": (
        "From 82 Reddit and X posts read in August 2026, of which 17 were both on topic and "
        "organic. Forty-three were dropped, including one X account that posted the same "
        "build-it-in-Claude-Code lead magnet eighteen times. Two themes people say are the problem "
        "here, the GAQL learning curve and rate limits, produced no organic posts at all, so this "
        "page does not claim them."),
    "voices": [
        ("The wall is the credential, not the endpoint",
         "A test developer token can only access whitelisted accounts. Move to standard access before you rely on it.",
         "r/PPC, 50 points", "https://www.reddit.com/r/PPC/comments/1ubluzq/i_got_tired_of_logging_into_google_ads_every/",
         "This was the largest theme in the research and it is the one thing treg.to genuinely "
         "changes the shape of: the credential is injected server side, so it never reaches your "
         "agent, your prompt or your repository. Be clear about what that does not do. It does not "
         "grant you access you do not have, and a token that only sees whitelisted accounts still "
         "only sees whitelisted accounts when the call goes through here."),
        ("Meta can switch you off and not tell you why",
         "I've been trying to connect my Meta Ads account to Claude via the MCP connector and I'm completely stuck.",
         "r/FacebookAds, 7 points", "https://www.reddit.com/r/FacebookAds/comments/1t01u3w/meta_ads_mcp_connector_is_ads_mcp_enabled_false/",
         "Worth being precise, because the reader's problem is a specific flag on Meta's own "
         "connector rollout rather than the Marketing API. The row on this page is the ordinary "
         "insights endpoint on your connected ad account, so that flag is not in its path. What "
         "nobody outside Meta can tell you is why a permission is off on one portfolio and on for "
         "another, and no proxy makes that legible."),
        ("One account is a weekend, twenty clients is a product",
         "I need to securely pull data from multiple client accounts, each with their own credentials and permissions.",
         "r/claude, 2 points", "https://www.reddit.com/r/claude/comments/1t3ynsq/looking_for_developer_to_build_multi_client_api/",
         "That is a paid job posting, which is the strongest evidence in the research that this is "
         "real work rather than an inconvenience. Holding a credential per connection and keeping "
         "it out of the agent is what a registry is for. Deciding which client an agent may read on "
         "a given turn is still yours to design, and this page will not pretend otherwise."),
        ("An agent will make the numbers up with total confidence",
         "The only way I trust it is when it runs scripts and saves outputs.",
         "r/PPC, 83 points", "https://www.reddit.com/r/PPC/comments/1sy40pq/my_experience_using_claude_code_codex_to_actually/",
         "No good answer, and anyone claiming one is selling you something. Nothing here stops a "
         "model inventing a figure. Two things do help and both are small: the platform's own "
         "response is relayed verbatim rather than reshaped by a layer in between, and every call "
         "is on the ledger, so the number in the answer has a call you can point at. Checking the "
         "total against the platform's own dashboard is still worth the thirty seconds."),
        ("The numbers move under you, and that is not going to stop",
         "Meta deprecated Reach in the Facebook Graph API and it is breaking dashboards everywhere.",
         "r/GoogleDataStudio, 6 points", "https://www.reddit.com/r/GoogleDataStudio/comments/1uv8e3n/meta_deprecated_reach_in_the_facebook_graph_api/",
         "Honest answer: this is the opposite of a fix. treg.to never models an upstream API, it "
         "relays the response as it came, so a deprecated field reaches you as a deprecated field "
         "rather than as a mapping layer quietly returning something stale. You find out faster and "
         "you find out truthfully. What no page can tell you is what Meta deprecates next."),
    ],
    "q_compare": "How do the two platforms compare?",
    "what_is_heading": "What do these two APIs actually return?",
    "what_is": (
        "The reporting surface of each platform, not a copy of its dashboard. Google Ads answers a "
        "query you write against a customer account and returns the campaign fields and metrics you "
        "named, over the date range you named. Meta answers on an ad account and returns spend, "
        "impressions, clicks and conversion counts at whichever level you ask for, optionally split "
        "by day or by a breakdown. Both are read paths on accounts you already own, which is why "
        "they carry no price."),
    "notes": [
        "Neither call costs money and both are rationed. Google Ads adds no per call charge and "
        "counts the request against the developer token's daily operation limit instead, which is "
        "15,000 operations a day on Basic access. Meta's insights call is included with the "
        "connected account and spends that ad account's rate limit budget, and a wide breakdown "
        "spends it fast, so ask for the split you will actually read.",
        "The two platforms want the request in different shapes. Google Ads takes a query against a "
        "customer id, so the fields, the metrics and the date range all travel in one string. Meta "
        "takes the ad account in the path and everything else as parameters: fields, either a date "
        "preset or an explicit range, the level, an optional daily increment and the breakdowns. "
        "Your agent writes both, and the level is where a wrong answer looks right.",
        "These two rows read, they do not spend. The capabilities behind this page are campaign "
        "performance and insights, so an agent pointed at them can report on a budget and cannot "
        "change one. If you want an agent that also edits campaigns, that is a different set of "
        "capabilities and a separate decision to make on purpose.",
    ],
    "faq": [
        ("Does this cost anything?",
         "No. Both rows run on accounts you already own, so treg.to relays the call and meters "
         "nothing, and the price shown before the call is zero. The provider's rate is the "
         "provider's rate with $0.000 added, and here the provider's rate is nothing."),
        ("Do I still need API access from Google and Meta?",
         "Yes. treg.to holds the credential server side so it never reaches your agent, and it does "
         "not stand between you and each platform's own access rules. A test developer token still "
         "sees only the accounts it was whitelisted for, and a Meta permission you were not granted "
         "is still not granted when the call arrives through here."),
        ("Which numbers come back?",
         "Spend, impressions, clicks and conversions at minimum. Meta will split them by day and by "
         "the breakdown you ask for, at account, campaign, ad set or ad level. Google Ads returns "
         "whichever campaign fields and metrics your query names, so the shape is yours to choose "
         "rather than fixed."),
        ("Can the agent change my campaigns?",
         "Not through this page. Both capabilities behind it are read paths, so the worst an agent "
         "can do here is read your numbers and be wrong about them. Changing a budget or pausing an "
         "ad is a different capability and you would be asking for it deliberately."),
    ],
    "related": ("Ads a competitor is running now", "Keywords a domain bids on",
                "Google Ads: the search terms triggering your ads",
                "Search Console: clicks, impressions and top queries"),
}


USE_CASE_PAGES["amazon-product-detail-by-asin"] = {
    "label": "Amazon product detail by ASIN",
    "sentence": "Amazon product API: any product's detail by ASIN",
    "title": "Amazon product API: {n} providers from {cheapest} | treg.to",
    "lede": (
        "Give your agent an ASIN and get the listing back as data: title, current price, images, "
        "specifications and the review summary. {n} providers do this through one treg.to key, from "
        "{cheapest} a product, with no Amazon programme to be approved for first. That last part is "
        "most of the reason this job has a price at all."),
    "prompt": "Using treg, get the Amazon product detail for ASIN B08N5WRWNW on amazon.com. Show me "
              "the price first, then give me the title, current price, rating and review count.",
    "prompt_why": [
        ("Give it the ASIN and the marketplace", "One ASIN is a different listing and a different price on each Amazon domain. Name the one you mean."),
        ("Ask for the price first", "treg.to returns the cost before the call, so the agent can say what it will spend."),
        ("Name the fields you want back", "A product payload is large. Saying which fields matter keeps the answer short and the context small."),
        ("Say what to do on a miss", "Dead and region locked ASINs are normal in any list. Tell it to skip and report rather than retry."),
    ],
    "result_noun": "product",
    "result_image": None,
    "voices_intro": (
        "From roughly 28 on-topic Reddit posts in August 2026, of which 16 were vendor written and "
        "excluded: four in one vendor's own subreddit, two of those word for word identical, plus "
        "one account running the same buying question through three scraping subreddits. The X half "
        "of the research held nothing organic on this job at all. What survived says something "
        "different from the marketing, so this page is built on it."),
    "voices": [
        ("The official API is gated behind a business you may not be in",
         "I applied for the official Amazon Product Advertising API (PA-API), got my keys, but for some reason, they never actually granted me functional access.",
         "r/developersIndia, 103 points", "https://www.reddit.com/r/developersIndia/comments/1q4i0l8/amazon_denied_my_api_access_so_i_built_my_own/",
         "The highest scoring organic post in the research, and the theme the marketing around this "
         "job never mentions, because it is easier to sell you a fix for blocking. Amazon's product "
         "API belongs to its affiliate programme, so it is granted to people earning it commission, "
         "not to people who want product data. Every row on this page sidesteps that by inverting "
         "the relationship: you are paying for data rather than being paid for referrals."),
        ("The deadlock: no API without sales, no sales without the API",
         "How do you get approved for Amazon Affiliate marketing if you cannot use the product advertising API without being approved first?",
         "r/Affiliatemarketing, 2 points", "https://www.reddit.com/r/Affiliatemarketing/comments/1alp1an/how_do_you_get_approved_for_amazon_affiliate/",
         "A fair question with an uncomfortable answer, and worth spelling out because the same "
         "poster goes on to ask whether to host Amazon's images anyway. Buying the data from a "
         "third party breaks the deadlock and does not touch the licence: these rows return image "
         "URLs, they do not grant you Amazon's rights to those images. If your plan needs the "
         "images, the affiliate programme is still the route, and this page is not a way around it."),
        ("An agent asked to read a listing will invent one instead",
         "You cannot use Chatgpt to search Amazon products - it won't even open links",
         "r/OpenAI, 50 points", "https://www.reddit.com/r/OpenAI/comments/1ph2nul/you_cannot_use_chatgpt_to_search_amazon_products/",
         "The thread underneath is the interesting part: the model did not fail loudly, it "
         "substituted a price from elsewhere and insisted it had read the listing. That is the "
         "failure mode that matters for agents, because it looks like an answer. A priced call "
         "against a named ASIN turns it into a fetch with a cost and a result, which is checkable "
         "in a way a confident paragraph is not."),
        ("A hand rolled scraper meets the CAPTCHA within a few dozen pages",
         "I tried using random user agents, time.sleep() to avoid that darned captcha page.",
         "r/webscraping, 4 points", "https://www.reddit.com/r/webscraping/comments/yn1cvl/bot_detection_with_python_requests/",
         "This is the second act of nearly every story in the research: the official route is shut, "
         "so people write the script, and the script dies somewhere in the first hundred pages. "
         "What changes here is who owns the repair. What does not change is that every provider in "
         "this space fails some proportion of requests, so the honest question is not who never "
         "fails but who tells you when they did, and what that failure costs you."),
        ("The proxy fixes the blocking and hands you the latency",
         "Requests without the proxy had an average response time of 1.5 seconds. However, with the proxy, the response time increased to around 6-10 seconds.",
         "r/scrapy, 1 point", "https://www.reddit.com/r/scrapy/comments/187goqh/requests_through_the_rotating_residential_proxy/",
         "Worth quoting precisely because it is not the complaint the vendor content wants you to "
         "have. In the organic posts the grievance about proxies is speed and upkeep, not price; "
         "the same poster calls his provider the cheapest he found. So compare on the whole cost of "
         "the do it yourself path, the maintenance and the seconds, rather than on a monthly proxy "
         "bill, and be sceptical of any page here or elsewhere that opens on how expensive proxies "
         "are."),
    ],
    "q_cheapest": "Which Amazon product API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What is an Amazon product API?",
    "what_is": (
        "It returns one Amazon listing as structured data rather than as a page: the title, the "
        "price showing at the moment of the call, images, the specification table, the rating and "
        "the review count. It is not Amazon's own API and it is not a feed. Each call reads a "
        "public product page as an anonymous visitor would see it, which is what makes it available "
        "without an Associates account and also what sets the edges of what it can ever return."),
    "notes": [
        "The three rows do not take the same key, which is work before it is price. Bright Data "
        "takes a product URL and returns records from its Amazon dataset. JustOneAPI takes the ASIN "
        "with a country code. SerpApi takes the ASIN with an Amazon domain. If what you hold is a "
        "list of bare ASINs, two of these are a direct call and the third needs a URL built first.",
        "There is no single true price for an ASIN, and that is not a gap in this comparison. What "
        "a product page shows depends on the marketplace, the delivery address, whether the viewer "
        "has Prime and which seller holds the buy box that second, so no provider can be more "
        "accurate in the abstract. The checkable difference is which of them lets you pin the "
        "context: a country code here, an Amazon domain there, and on the URL based row whatever "
        "marketplace the URL points at.",
        "Every row bills only what succeeds, which is what makes a long unverified list safe to "
        "hand an agent. Two are priced per successful call and the third per record actually "
        "delivered, so an ASIN that is dead, region locked or withdrawn costs nothing. What you can "
        "still run up is your own retry loop, so tell the agent to skip and report rather than to "
        "try harder.",
    ],
    "faq": [
        ("How much does one product lookup cost?",
         "A fraction of a cent to about a cent and a half depending on the row, at the provider's "
         "own rate with $0.000 added by treg.to. The live figures are in the comparison above, and "
         "all three bill only when the lookup works."),
        ("Why not use Amazon's own API?",
         "Because neither of Amazon's APIs is a general read any product API. The Product "
         "Advertising API belongs to the affiliate programme and is granted to accounts earning "
         "qualifying commission, which is why people report keys that never became working access. "
         "The Selling Partner API is for managing your own selling account. If you are in neither "
         "programme, there is no official door."),
        ("Do these give me the right to use Amazon's product images?",
         "No, and it is worth being blunt because the research is full of people asking. A data API "
         "returns image URLs; it does not hand you Amazon's licence to host or republish them. "
         "Rights to product imagery come from the affiliate programme or from the brand, and no "
         "amount of paying for data changes that."),
        ("Can I look up thousands of ASINs?",
         "Yes, and the cost is linear because it is one call per ASIN with no batch endpoint. Your "
         "agent runs the list; treg.to prices each call and shows the running total, and since all "
         "three rows bill only on success, the dead entries in a scraped list do not cost you "
         "anything."),
    ],
    "related": ("Amazon search and best sellers", "TikTok Shop products and reviews",
                "App store search", "Product reviews"),
}


USE_CASE_PAGES["find-local-businesses-by-keyword-and-location"] = {
    "label": "Find local businesses by keyword and location",
    "sentence": "Yelp API and Tripadvisor API: local businesses by keyword",
    "title": "Yelp API and Tripadvisor API, from {cheapest} | treg.to",
    "lede": (
        "Ask for a kind of business and a place, and get the listings back as data: names, ratings, "
        "review counts, addresses and categories. Two sources through one treg.to key, from "
        "{cheapest} a call, with no Yelp or Tripadvisor developer programme to be admitted to "
        "first. They are different listings rather than two copies of one, so the comparison below "
        "groups them rather than ranking them against each other."),
    "prompt": "Using treg, find ramen restaurants in Austin, Texas on Yelp and on Tripadvisor. Show "
              "me the price first, then give me one list with each place's name, rating, review "
              "count and address.",
    "prompt_why": [
        ("Give the thing and the place separately", "Both sources take a description and a location as two fields. One blob of text resolves badly."),
        ("Name the city and the state", "The commonest empty result on this job is a location string neither source recognises."),
        ("Say which source you want", "Yelp and Tripadvisor hold different businesses. Ask for both and you get both, side by side."),
        ("Ask for the price first", "treg.to returns the cost before the call, so the agent can say what it will spend."),
    ],
    "result_noun": "business",
    "result_image": None,
    "voices_intro": (
        "From roughly 200 Reddit and X posts read in August 2026, with eleven vendor items "
        "excluded, among them three accounts seeding the same Google Maps scraper across three "
        "subreddits with the same story about a family member's small business. One finding is "
        "worth stating before the quotes: nobody in the organic posts says Yelp has closed its API "
        "programme, so this page does not say it either. The pain people actually report is "
        "different and more mundane."),
    "voices": [
        ("It works in Postman and dies in the application",
         "my call is successful in postman but I'm experiencing this error when making the call locally from the app",
         "r/Angular2, 2 points", "https://www.reddit.com/r/Angular2/comments/7ns9gv/experiencing_403_when_making_get_call_to_yelp_api/",
         "This shape recurred six times in the research and it is the one theme this page can "
         "honestly own. The key is fine and the request is fine; what is wrong is where the call "
         "came from, usually a browser preflight or a key that ended up inside a front end bundle. "
         "A call through treg.to leaves a server with the credential injected there, so that class "
         "of failure stops existing. It will not rescue you from a parameter the source rejects."),
        ("A thousand results is the ceiling, and no proxy raises it",
         "don't expect to pull more than 1,000 results as the Yelp Fusion API has a hard limitation in that regard",
         "r/learnpython, 7 points", "https://www.reddit.com/r/learnpython/comments/92hcf7/how_to_loop_api_call_requests_yelp_fusion/",
         "No good answer, and it would be dishonest to imply otherwise. A ceiling on a source is a "
         "property of the source; buying the call from someone else does not lift it. The only "
         "thing that actually works is fanning the question out, splitting a metro into its "
         "suburbs and a trade into its categories, and paying per call for each slice. That is a "
         "loop your agent can run and it is why the per call price matters more than it looks."),
        ("Nobody reads the content rights until something makes them",
         "I got to the Content rights part and hadn't looked into what rights I actually need for that information.",
         "r/webdev, 3 points", "https://www.reddit.com/r/webdev/comments/1jekz4a/yelp_fusion_api_as_third_party_info_rights/",
         "Also no good answer, and the place where a page like this is most tempted to be vague. "
         "Having a credential injected for you changes who holds a key. It does not change who is "
         "bound by the source's terms, which is still you, and a listing you pull here is still "
         "Yelp's or Tripadvisor's content. If the plan is to republish it, that is a question for "
         "their terms and not for a price table."),
        ("Tripadvisor will not let you build on localhost",
         "But problem is tripadvisor api does not allow to type localhost:3000.",
         "r/node, 5 points", "https://www.reddit.com/r/node/comments/14hvtrp/how_do_i_allow_localhost3000_whem_i_am_using/",
         "A narrow but real win. Tripadvisor's own programme issues keys tied to a domain, which is "
         "exactly the thing a laptop does not have, and the thread underneath is people inventing "
         "workarounds for it. The row here does not use that key at all, so there is nothing to "
         "restrict and nothing to register before you can try it from your own machine."),
        ("Most people already left for Google Maps, and it stops at the front desk",
         "The issue I’m running into is that most tools (Google Maps, etc.) only give me the public front-desk phone number.",
         "r/ClaudeCowork, 8 points", "https://www.reddit.com/r/ClaudeCowork/comments/1uxezdz/what_are_you_using_to_scrape_local_business/",
         "The loudest theme in the whole research, so it deserves a straight answer rather than a "
         "deflection. If Google Maps is the source you want, this page is not it: treg.to has no "
         "Places keyword search on the menu today, and the Google rows it does have for local are "
         "for a Business Profile you already own. On the second half, no listings source anywhere "
         "carries the owner's address, because the listing does not have one. Getting past the "
         "front desk is a second step against the business's own site, and it is the step with a "
         "hard floor."),
    ],
    # Not "which is cheapest": one provider per platform, so ranking the two rows against each
    # other would compare a Yelp search with a Tripadvisor one. The heading makes it a price list.
    "q_cheapest": "What does each source cost?",
    "q_compare": "How do the two sources compare?",
    "what_is_heading": "What does a local business search return?",
    "what_is": (
        "A page of listings for one kind of business in one place, as data: the name, the rating "
        "and how many reviews it is averaged over, the address, the categories the source files it "
        "under, and its own page on that source. It is a directory lookup, not a lead list. "
        "Nothing here is scored, deduplicated across sources or checked against a company register, "
        "and the two sources will disagree about which businesses exist because they hold different "
        "directories."),
    "notes": [
        "The two rows work differently, and one of them is not a single round trip. SerpApi reads "
        "Yelp's own results page from a description and a location and answers immediately. "
        "DataForSEO posts an asynchronous task against Tripadvisor which you collect afterwards, so "
        "that side is two calls with a wait in between. Tell the agent to expect it rather than to "
        "treat the empty first response as a failure.",
        "Location is where this job fails quietly rather than loudly. DataForSEO takes a location "
        "name or a numeric location code and will return a perfectly valid answer for the wrong "
        "market if the code is not the one you meant. SerpApi takes free text, which is forgiving "
        "until two places share a name. Give both the city and the state and check the first "
        "result's address before you loop over three hundred queries.",
        "Neither row makes you a Yelp or Tripadvisor developer, and neither changes whose content "
        "it is. There is no Fusion key to be approved for and no domain restricted Tripadvisor key "
        "to register, which removes the step people in the research got stuck on. What does not "
        "move is the licence: the listings belong to Yelp and to Tripadvisor, and their terms bind "
        "whoever republishes them, credential injection or not.",
    ],
    "faq": [
        ("How much does one local business search cost?",
         "The provider's own rate with $0.000 added by treg.to, from a prepaid balance rather than a "
         "plan. The two sources are priced differently and are billed in different units, so the "
         "comparison above gives each one separately rather than a single headline number."),
        ("Is this the Yelp Fusion API?",
         "No. The Yelp row reads Yelp's public results page for a description and a location, so "
         "there is no Fusion key, no application and no approval to wait for. It also means you get "
         "what the results page shows rather than the Fusion field set, and that Yelp's terms still "
         "govern what you may do with the listings."),
        ("Can I search Google Maps or Google Business Profile this way?",
         "Not from this page. treg.to has no Google Places keyword search in the catalog today, and "
         "the Google rows it does carry for local businesses read a Business Profile you already "
         "own, on your own connected account. If Maps is the source you need, this job cannot serve "
         "it and saying so is more useful than a near miss."),
        ("Will I get the owner's email address?",
         "No, and no listings source will. A directory entry carries the business's public contact "
         "details, which in practice means the front desk. Going further means visiting each "
         "business's own site and verifying what you find there, which is a separate job with its "
         "own failure rate rather than a field you can ask for here."),
    ],
    "related": ("A business's reviews", "Hotel listing details", "Product reviews",
                "Your Google Business Profile reviews, and reply to them"),
}


USE_CASE_PAGES["keywords-a-domain-ranks-for"] = {
    "label": "Keywords a domain ranks for",
    "sentence": "Rank tracking API: the keywords a domain ranks for",
    "title": "Rank tracking API: {n} providers from {cheapest} | treg.to",
    "lede": (
        "Ask for a domain and get the keywords it already ranks for in Google, with the position it "
        "holds and the volume behind each one. {n} providers do this through one treg.to key, from "
        "{cheapest} a call, billed per call from a prepaid balance rather than an annual seat. It "
        "works on any domain, which is exactly the question your own Search Console cannot answer."),
    "prompt": "Using treg, get the top 200 keywords stripe.com ranks for in Google in the United "
              "States, with position and monthly volume. Show me the price first, then group them "
              "by position band.",
    "prompt_why": [
        ("Name the domain and the market", "A domain ranks differently in each country's index. Say which one, or you get the provider's default."),
        ("Say how many keywords you want", "Row count is the cost dial on most of these rows. A limit you chose beats a default you did not."),
        ("Ask for position and volume together", "Both arrive in the same row. Asking for them separately is two calls for one answer."),
        ("Ask for the price first", "treg.to returns the cost before the call, so the agent can say what it will spend."),
    ],
    "result_noun": "keyword",
    "result_image": None,
    "voices_intro": (
        "From eight searches across Reddit and X in August 2026, of which only about 13 posts were "
        "both on topic and organic. Thirteen more were a single coordinated cluster promoting one "
        "vendor across three small subreddits, seeding the question in one and answering it in "
        "another, with non-breaking spaces left mid-sentence in the bodies. SEO is the most "
        "astroturfed category this loop has researched, so the surviving posts are quoted and the "
        "rest are only counted."),
    "voices": [
        ("The number people quote is the API tier, not the subscription",
         "Don't really want to drop $14K to have access to the ahrefs API, so I'm hoping there are other, high-quality options",
         "r/bigseo, 1 point", "https://www.reddit.com/r/bigseo/comments/1fyki5o/if_i_want_to_identify_ranking_keywords_for_a/",
         "Worth quoting and then correcting, because the correction is the useful part. That figure "
         "is an old enterprise tier and the research found it has since moved, so check the vendor's "
         "current page rather than a thread. Then apply the same suspicion here: every price in the "
         "comparison above carries the date it was read off the provider's own rate card or "
         "documentation, because a price without a date is the thing that misled this reader."),
        ("A subscription is the wrong instrument for uneven work",
         "Semrush starts at $120/month. Ahrefs is up there too. For a bootstrapped operation that's a real cost",
         "r/DigitalMarketing, 82 points", "https://www.reddit.com/r/DigitalMarketing/comments/1shurkl/replaced_semrush_with_the_gemini_api_and_search/",
         "The objection in the research was almost never the total, it was the variance: a flat "
         "monthly fee prices the quiet months wrong, and this work arrives in bursts. Every row "
         "here is per call from a prepaid balance with no minimum and no commitment. The honest "
         "trade is that you get an index and a raw response rather than a curated keyword universe "
         "with a difficulty score on top, and the difference is your own development time."),
        ("The databases disagree with each other and with your browser",
         "Sometimes it works perfectly, other times it gives a completely different result to what we see if we visit Google in the browser",
         "r/SEO, 7 points", "https://www.reddit.com/r/SEO/comments/1t8s9wq/rank_tracking_for_localised_results/",
         "No comparison table can tell you whose index is right, and this one will not pretend to. "
         "Nobody publishes a methodology, every accuracy claim is made by the vendor selling it, "
         "and the phrase keywords a domain ranks for means a different keyword universe at each "
         "provider before you compare a single position. The only honest offer is a procedure: run "
         "the same fifty keywords through two of these for a week, diff both against your own "
         "Search Console, and keep the one whose error is stable rather than the one whose number "
         "is highest. At these prices that experiment costs less than a coffee."),
        ("The people asking are already writing the script",
         "I wanted to do some keyword research yesterday and was surprised by how expensive Ahrefs / Semrush were.",
         "r/TechSEO, 36 points", "https://www.reddit.com/r/TechSEO/comments/1r7qifp/open_source_seo_tool_that_uses_your_own/",
         "This post ends in an open source interface over a raw data API, and it was the most "
         "common constructive answer in the research by some distance. That audience does not need "
         "a feature grid, it needs the call, the response shape and the real per call price, which "
         "is what the comparison above and the runnable call under it are for. The part nobody "
         "warns them about is the unit: two of these rows bill the request and two bill the row."),
        ("Ask for traffic and you may be handed a model's opinion",
         "Tried DataForSEO but couldn't get the traffic endpoint working properly - they provide estimated traffic value instead of traffic.",
         "r/Agentic_SEO, 12 points", "https://www.reddit.com/r/Agentic_SEO/comments/1u6avvw/any_cheap_api_for_url_traffic_estimates/",
         "Fair criticism of a provider on this very page, and it generalises past that provider. "
         "Position is close to observable, since somebody looked at a result page. Volume and "
         "traffic are not: every vendor here derives them, none of them measures them, and the "
         "numbers differ because the models differ. Treat position as data and traffic as an "
         "opinion with a price on it, and the disagreements stop being surprising."),
    ],
    "q_cheapest": "Which ranked keywords API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What is in a ranked keywords pull?",
    "what_is": (
        "One row per keyword the domain appears for in the provider's index of Google: the keyword, "
        "the position it holds, the search volume behind it and usually a cost per click and a "
        "landing URL. It is a snapshot taken from an index that vendor already maintains, not a "
        "live search run for you, which is why it can return thousands of rows for the price of one "
        "request and why two vendors will hand you different lists for the same domain."),
    "notes": [
        "The billing unit changes per row and it changes which one is cheapest for you. DataForSEO "
        "charges per request plus a fraction of a cent for each keyword returned. SE Ranking charges "
        "a flat hundred credits per request and nothing per record, so one page of a thousand rows "
        "costs a fraction of ten pages of a hundred. Serpstat charges a credit per keyword and bills "
        "its one credit minimum even when nothing comes back. Read the unit column before the price "
        "column.",
        "SpyFu's figure here is the top of a published band rather than a quoted rate. Its pricing "
        "table gives a range per thousand rows across the research endpoints and does not say which "
        "tier this one sits in, so the catalog carries the upper bound instead of guessing low. Read "
        "that row as a ceiling and the others as rates. Its page size also defaults to five, so set "
        "it explicitly or the call returns almost nothing.",
        "Semrush shows no dollar figure, and that is the honest thing to print. It bills in API "
        "units bought in packages up front, ten per line returned, so what a pull costs depends on "
        "the package you hold rather than on any public per call rate. A default request there "
        "returns ten thousand lines, which is a hundred thousand units, so send a display limit "
        "before you send anything else.",
    ],
    "faq": [
        ("Is this rank tracking?",
         "Not in the sense of a tracker you configure and leave running. These return the keywords a "
         "domain ranks for right now, from each vendor's index, in one call. Nothing here schedules "
         "itself, keeps a history for you or alerts you to a drop; if you want positions over time "
         "you run the call on your own schedule and store the results. treg.to has no scheduled rank "
         "tracker on the menu, and saying so is more useful than a near miss."),
        ("How much does one domain pull cost?",
         "A cent or two per request on the rows that bill the request, and a fraction of a cent per "
         "keyword on the rows that bill the row, at the provider's own rate with $0.000 added by "
         "treg.to. The comparison above prints both, and which is cheaper depends entirely on how "
         "many keywords you ask for."),
        ("Why do the providers disagree about the same domain?",
         "Because each one is answering from its own index, built by its own crawl on its own "
         "schedule, and the phrase keywords a domain ranks for is a keyword universe that differs "
         "before a single position is compared. No table settles this. Running two of them against "
         "your own Search Console for a week does."),
        ("Can I do this for a competitor's domain?",
         "Yes, and that is the point. Search Console is ground truth for sites you own and silent "
         "about everybody else, so a competitor's ranked keywords have no first party source at all. "
         "The target is just a domain on every row here, which is why this job exists as something "
         "you pay for."),
    ],
    "related": ("Google results for a keyword", "Keyword volume, CPC and competition",
                "Backlink profile of a domain", "Keywords a domain bids on"),
}

USE_CASE_PAGES["how-ai-answers-mention-your-brand"] = {
    "label": "How AI answers mention your brand",
    "sentence": "AI visibility tracking: how ChatGPT and Perplexity answers mention your brand, run by your agent",
    "title": "AI visibility tracking API, from {cheapest} a check | treg.to",
    "lede": (
        "Every AI visibility tool sells the same loop: run a set of prompts through ChatGPT and "
        "Perplexity, note who gets named, repeat next week. Your agent can run that loop itself. "
        "One provider serves it through treg.to, as live answers to a prompt and as aggregated "
        "mention metrics for a keyword or domain, priced per call from {cheapest}, with no "
        "subscription and no dashboard to pay for."),
    "prompt": "Using treg, run these 12 buyer prompts through ChatGPT and Perplexity, US, web search "
              "on, and tell me for each one whether treg.to or any of Composio, Pipedream or Zapier "
              "is named, and in what position. Show me the total price before you start.",
    "prompt_why": [
        ("Fix the prompt set first", "The check is only comparable week to week if the prompts do not move."),
        ("Name the competitors", "Presence on its own says little; share of the answer against named rivals is the number."),
        ("Ask for the price up front", "The two answer endpoints differ five times in price; the metrics call is dearer again."),
        ("Run it more than once", "Answers vary run to run. Two passes on the same day show you the noise floor."),
    ],
    "result_noun": "answer",
    "result_image": None,
    "what_is_heading": "What is AI visibility tracking?",
    "what_is": (
        "AI visibility tracking is the practice of measuring whether, and how, AI answer engines "
        "such as ChatGPT, Perplexity and Google's AI Overviews mention a brand when someone asks "
        "the questions its buyers ask. It borrows the shape of rank tracking, a fixed prompt set "
        "checked on a schedule, but the answers are generated rather than ranked, so the same "
        "prompt can name different brands on different days. The tools sold under this name "
        "charge a monthly subscription for running the prompts and charting the result; the "
        "underlying data is a call to the model and a count."),
    "notes": [
        "There are two kinds of endpoint here and they answer different questions. The two live "
        "answer endpoints send your prompt to ChatGPT or Perplexity, with web search on if you ask "
        "for it and a country to search from, and return the answer as data for your agent to read: "
        "that is the check. The two mention-metrics endpoints return aggregated counts of how often "
        "a keyword or a domain appears across a platform's answers, which is the trend line. Run "
        "the first on your prompt set and the second on your domain.",
        "The prices are not alike. On the catalog's verified rates a Perplexity answer is about "
        "half a cent, a ChatGPT answer about three cents, and a mention-metrics call about a dime. "
        "A weekly run of fifty prompts on both engines is therefore under two dollars, which is the "
        "argument against a subscription; a daily run across many domains is where the metrics "
        "call starts to matter.",
        "The answer is a sample, not a fact. The same prompt to the same model on the same day can "
        "name a different set of brands, and every engine changes what it cites without notice. "
        "Nothing on this page smooths that, and no tool that charts it can either. What the "
        "per-call price buys you is the ability to repeat the run cheaply enough to see the noise "
        "before you read a trend into it.",
    ],
    "faq": [
        ("Is this a scrape of the ChatGPT website?",
         "No. The provider calls the model's own API with web search enabled and returns the "
         "answer as structured data. You choose the model name and the country to search from. "
         "It is the same generated answer, without a browser."),
        ("Can it tell me how to get mentioned?",
         "No. It tells you whether you are named, in which answers, and how that count moves. "
         "Earning the mention is a content and citation problem, and the neighbouring pages on "
         "Google results and a domain's backlinks are the places to start on it."),
        ("Which engines are covered?",
         "Live answers from ChatGPT and Perplexity. The mention metrics take a platform parameter, "
         "and the catalog's verified examples use ChatGPT and Google. Gemini and Claude are not "
         "on this shelf today."),
        ("Do I need a DataForSEO account?",
         "No. treg.to serves this on its own key at the provider's rate with $0.000 markup, "
         "metered from your team's balance. If you already have an account, register the key "
         "and those calls are never metered."),
    ],
    "voices_intro": (
        "This is the most astroturfed category on the menu: 10 of the 22 relevant posts in "
        "August 2026 were tool launches, vendor data dumps posted twice, or a vendor's employee "
        "asking the question their product answers. These four are people doing the job by hand."),
    "voices": [
        ("The check is a weekly copy-and-paste job",
         "who's still manually prompting ChatGPT and Claude to check if their brand shows up in AI answers?",
         "r/seogrowth, 22 points", "https://www.reddit.com/r/seogrowth/comments/1sm5t9r/whos_still_manually_prompting_chatgpt_and_claude/",
         "This is exactly the loop an agent should own. Hand it the prompt list and the competitor "
         "names, and it runs the set through both engines and returns a table, at a few cents a "
         "prompt, on whatever schedule you give it."),
        ("Nobody is sure what the method should even be",
         "API calls on a schedule, a paid tool, something manual in a spreadsheet?",
         "r/aeo, 16 points", "https://www.reddit.com/r/aeo/comments/1vqugjg/how_do_you_actually_track_ai_visibility_across/",
         "The first of those three, and it is less work than it sounds. The schedule is the "
         "agent's, the API is one call per prompt per engine, and the spreadsheet is the table it "
         "hands back. Which prompts represent your buyers is still your judgement; no API makes "
         "that call."),
        ("The numbers feel made up",
         "curious what youre actually tracking for AI citations because the methodology feels kind of made up right now",
         "r/DigitalMarketing, 54 points", "https://www.reddit.com/r/DigitalMarketing/comments/1staemu/added_ai_citation_tracking_to_our_monthly_reports/",
         "It is, a little, everywhere: a generated answer is a sample. The honest version of the "
         "metric is a fixed prompt set, run more than once, with the run-to-run variance reported "
         "next to the share. A per-call price makes the repeat runs affordable; it does not make "
         "the engines consistent."),
        ("Doing it yourself, the API bill ran past the estimate",
         "I ran 1,564 real ChatGPT answers through the numbers to check if its Reddit citations actually cratered",
         "r/aeo", "https://www.reddit.com/r/aeo/comments/1vwuthp/i_ran_1564_real_chatgpt_answers_through_the/",
         "A self-built run with web search on, cut short at seven of ten categories when the "
         "per-call cost overran. That is the case for seeing the price before the run: the agent "
         "shows the total for the prompt set first, and a thousand ChatGPT answers here is a known "
         "figure, not an estimate."),
    ],
    "related": ("Google results for a keyword", "Keywords a domain ranks for",
                "Backlink profile of a domain", "Search Console: clicks, impressions and top queries"),
}

USE_CASE_PAGES["your-google-business-profile-reviews-and-reply-to-them"] = {
    "label": "Your Google Business Profile reviews, and reply to them",
    "sentence": "Google Business Profile API: your Google reviews, read and replied to by your agent",
    "title": "Google Business Profile API: read and reply to reviews | treg.to",
    "lede": (
        "Connect the Google Business Profile you already manage and your agent can read every "
        "review on every location, and reply as the business. This is the Google My Business API "
        "as it is now called, on your own account, so treg.to never meters it; and the API access "
        "request that stops most people at zero quota is one treg.to has already made."),
    "prompt": "Using treg, list every review on our Austin location from the last 30 days with three "
              "stars or fewer, draft a reply to each in our voice, and post them only after I approve.",
    "prompt_why": [
        ("Connect once", "One consent screen for the Google account that manages the listing. No Cloud project of your own."),
        ("Name the location", "A profile holds many locations; the reviews call is per location, or batched across them."),
        ("Keep a human gate on replies", "A reply publishes under the business name. Ask for drafts, then approve."),
        ("It costs nothing", "Your own account, so the call is never metered."),
    ],
    "result_image": None,
    "what_is_heading": "What is the Google Business Profile API?",
    "what_is": (
        "The Google Business Profile API, formerly the Google My Business API, is how a business "
        "reads and manages its own listings on Google Search and Maps as data: locations, hours, "
        "posts, performance, and the customer reviews with their star rating, text and date, plus "
        "the reply the business has posted to each. It is scoped to listings the connected account "
        "owns or manages. It is not a way to read another business's reviews; that is a separate "
        "job on the menu."),
    "notes": [
        "The gate on this API is not the OAuth scope, it is the access request. Google starts every "
        "Cloud project at zero requests a day on the Business Profile API until it approves the "
        "project, and that is the wall in most of the forum posts. The request belongs to the "
        "project making the call, and that project is treg.to's: you consent, you do not apply.",
        "Reviews still live on the older v4 surface while the rest of the profile moved to the v1 "
        "services, so the review calls take an account id and a location id rather than a resource "
        "name. Your agent lists accounts first, then locations, then reviews; a batch endpoint "
        "reads reviews across several locations in one call.",
        "Replying is an action, not a read. The reply publishes publicly under the business name, "
        "it can be edited or deleted later through the same API, and a reply signals to Google that "
        "the review is a real customer's. Ask the agent to draft, and to flag anything that reads "
        "like a policy violation before you answer it rather than after.",
    ],
    "faq": [
        ("Does this cost anything?",
         "No. The Business Profile API runs on your own Google account, so treg.to relays the call "
         "and meters nothing. Only calls on treg.to's own provider keys are billed."),
        ("Do I need to apply for Google Business Profile API access?",
         "No. Google grants that access to the Cloud project making the calls, and treg.to holds "
         "the approved app. You need to be an owner or manager of the listing, and to consent once."),
        ("Can my agent read a competitor's reviews this way?",
         "No. The API returns only the listings your connected account manages. Reading any "
         "business's public reviews is the neighbouring job, served by scraping providers."),
        ("Does it work across several locations?",
         "Yes. The reviews call is per location, and there is a batch call that reads reviews "
         "across several of your listings at once. The agent lists your locations first."),
    ],
    "voices_intro": (
        "The Google Business Profile subreddits are a queue of people waiting on Google. From ~40 "
        "Reddit and X posts in August 2026, ten were vendors selling reply tools, including one "
        "study posted word for word to two subreddits. These four are people stuck at the door."),
    "voices": [
        ("The access request gets rejected, even when you follow the rules",
         "GBP API access rejected even though I followed their \"client account\" rule?",
         "r/GoogleMyBusiness", "https://www.reddit.com/r/GoogleMyBusiness/comments/1sx6686/gbp_api_access_rejected_even_though_i_followed/",
         "The request is judged per Cloud project, and it is opaque: several posters waited weeks "
         "with no reply at all. That is the part treg.to takes off the table. You are consenting "
         "to an app that already has access, not applying for your own."),
        ("Approved, and the quota is still zero",
         "I'm currently stuck with the Google Business Profile API where the quota is set to 0 and the API is basically unusable.",
         "r/localseo", "https://www.reddit.com/r/localseo/comments/1r64f1s/google_business_profile_api_quota_stuck_at_0_has/",
         "Zero is the default for every new project and the quota bump is a second request. Both "
         "belong to the project, not the user, which is why a hosted connection helps here and a "
         "tutorial does not."),
        ("It only ever shows you your own listing",
         "Google's own APIs will hand you your own listing and nothing else.",
         "r/n8n", "https://www.reddit.com/r/n8n/comments/1vms0uu/i_built_a_free_template_that_logs_every/",
         "True, and this page will not pretend otherwise. This job is your reviews; a competitor's "
         "public reviews are the neighbouring job, on Yelp, TripAdvisor and Trustpilot, and the "
         "local pack comes through the SERP providers."),
        ("Replying to a bad review can make it harder to remove",
         "if you reply to the review, it treats it as a real customer, making it harder to get it taken down.",
         "X, 142 likes", "https://x.com/i/status/2090034289811288369",
         "One reason to keep the reply step behind your approval. Have the agent read each new "
         "review against Google's review policy first and flag the ones worth reporting, then reply "
         "to the rest."),
    ],
    "related": ("A business's reviews", "Find local businesses by keyword and location",
                "Search terms that surfaced your listing on Maps", "Search Console: clicks, impressions and top queries"),
}

USE_CASE_PAGES["business-reviews"] = {
    "label": "A business's reviews",
    "sentence": "Review scraper API: a business's reviews from Tripadvisor, Trustpilot and Yelp, as data",
    "title": "Tripadvisor, Trustpilot and Yelp reviews API | treg.to",
    "lede": (
        "Give your agent a business's page and get its reviews back as rows: rating, text, date "
        "and reviewer, ready to sort, count or read. Three review sites answer through one treg.to "
        "key, from {cheapest}, without a Tripadvisor API key, a Yelp Fusion application or a "
        "browser of your own. They are not alternatives to each other; the site is the choice."),
    "prompt": "Using treg, pull the last 200 Tripadvisor reviews for this hotel URL, show me the "
              "price first, then give me the rating distribution by month and the ten most recent "
              "reviews of two stars or fewer in full.",
    "prompt_why": [
        ("Give the page, not the name", "Every provider here takes a URL, a path or a domain. Find the listing first if you only have a name."),
        ("Say how many you want", "The Trustpilot and Tripadvisor tasks take a depth. The count you ask for is the count you pay for."),
        ("Compare on the unit", "One provider bills per record delivered, the other per task. Ask which is cheaper for your count."),
        ("Bring your own analysis", "The rows carry the text. Sentiment, themes and summaries are the agent's job on top."),
    ],
    "result_noun": "review",
    "result_image": None,
    "what_is_heading": "What is a review scraper API?",
    "what_is": (
        "A review scraper API returns the public reviews on a business's listing as structured "
        "records, rating, text, date, reviewer and the business's reply where there is one, "
        "without you running a browser against the site. It exists because the official routes "
        "are narrow: Yelp's Fusion API is an application and a plan, Tripadvisor's Content API "
        "is an approval, and neither is built for pulling every review of one business. The "
        "providers here read the public page and hand back the rows."),
    "notes": [
        "The two providers bill in different units and the difference matters for a long pull. "
        "Bright Data delivers records and bills per record delivered, on Yelp and Trustpilot. "
        "DataForSEO runs a task, on Tripadvisor and Trustpilot, and bills per task at a fraction of "
        "a cent whatever the depth returns. For a few dozen reviews the difference is nothing; for "
        "a business with thousands, ask the agent to price both before it starts.",
        "The input is the listing, not the business name. Tripadvisor wants the review page's "
        "path, Trustpilot the business's domain, Yelp the page URL, so the agent resolves a name "
        "to a listing first, which is the neighbouring job on this menu. A wrong page returns "
        "someone else's reviews, not an error.",
        "This is the public page as it stands. A review the site has removed is gone from here "
        "too, the rows are as fresh as the crawl behind them, and what you may do with the text "
        "is governed by each site's terms and your own use, which no provider settles for you.",
    ],
    "faq": [
        ("Do I need a Yelp Fusion or Tripadvisor API key?",
         "No. Neither provider here uses the sites' official APIs. They read the public listing "
         "page and return the reviews as records, billed to your treg.to balance at the provider's "
         "rate with $0.000 markup."),
        ("Which site should I use?",
         "The one the business is reviewed on. Tripadvisor for hotels, restaurants and attractions, "
         "Trustpilot for online businesses by domain, Yelp for local services in North America. "
         "Pulling from the wrong site returns a short, misleading list."),
        ("What about Google reviews?",
         "The reviews on a listing you own or manage are the Business Profile job on this menu, "
         "free on your own account. This page covers the three public review sites."),
        ("Can I get all of a business's reviews?",
         "You can ask for a depth, and the task returns up to that many. Whether every review "
         "of a business with thousands comes back is a property of the site, so check the count "
         "against the listing rather than assuming."),
    ],
    "voices_intro": (
        "Review data is sold hard: about 25 of the ~120 posts on these three sites in August 2026 "
        "were scraper vendors, Apify listings posted from template accounts, and the same lead-gen "
        "thread pasted twice. These four are people who tried the official door first."),
    "voices": [
        ("The official Yelp API returns nothing for a valid business",
         "Yelp Fusion API \"NOT_FOUND\" error when requesting reviews (Python)",
         "r/webscraping", "https://www.reddit.com/r/webscraping/comments/1ivggvp/yelp_fusion_api_not_found_error_when_requesting/",
         "The Yelp provider on this page does not go through Fusion at all. It reads the public "
         "page by URL and bills per record delivered, so a business that Fusion cannot find is "
         "still a page you can point at."),
        ("Yelp at any scale fights back",
         "Is Yelp just a nightmare to scrape, or are no-code tools just not built for this at scale?",
         "r/scrapingtheweb, 9 points", "https://www.reddit.com/r/scrapingtheweb/comments/1t6pyca/have_you_ever_tried_scraping_yelp_without_coding/",
         "Both, and the answer is to stop running the browser yourself. A per-record provider "
         "carries the blocking, the retries and the proxies, and you pay for the rows that arrive. "
         "That is what the fraction of a cent buys."),
        ("Trustpilot stops at a couple of hundred without a login",
         "they cap you at 200 reviews without auth. A `jwt` cookie removes the cap.",
         "r/webscraping, 7 points", "https://www.reddit.com/r/webscraping/comments/1vw95dz/scraper_for_pulling_trustpilot_reviews/",
         "A self-built scraper's workaround, and the kind of thing that breaks quietly. The "
         "Trustpilot task here takes a depth parameter instead; ask for what you need and read "
         "the count that comes back rather than assuming the whole history arrived."),
        ("The official price is the reason people scrape",
         "their API is horrifically expensive for poor old me, and I was not in the mood to build a web scraper",
         "r/gis, ~800 points", "https://www.reddit.com/r/gis/comments/1iph0yy/the_closer_to_the_railway_station_the_less_tasty/",
         "The page shows the provider's rate before the call, in fractions of a cent per record "
         "or per task, so the choice between paying and building is a number rather than a mood."),
    ],
    "related": ("Find local businesses by keyword and location",
                "Your Google Business Profile reviews, and reply to them",
                "Product reviews", "Hotel listing details"),
}

USE_CASE_PAGES["google-analytics-traffic-and-behaviour-reports"] = {
    "label": "Google Analytics: traffic and behaviour reports",
    "sentence": "Google Analytics MCP or API: GA4 traffic and behaviour reports, read by your agent",
    "title": "Google Analytics API for {agent}: any GA4 report | treg.to",
    "lede": (
        "Connect the GA4 property you already own and your agent can run any report the Data API "
        "can: sessions, users, conversions and events by channel, page, country, device or date, "
        "with filters and ordering, in plain words. It is the Google Analytics API without the "
        "Cloud project, and it runs on your own Google account, so treg.to never meters it."),
    "prompt": "Using treg, show me sessions and key events by default channel group for the last "
              "28 days ending 3 days ago, next to the 28 days before, and flag any channel that is "
              "down by more than a fifth.",
    "prompt_why": [
        ("Connect once", "One consent screen for the Google account that can see the property. No Cloud project, no service account."),
        ("Name the property, or let it list them", "A Google account often sees several GA4 properties. The agent can list them and ask."),
        ("End the window a few days back", "GA4 keeps processing recent days. A window that ends on yesterday is still settling."),
        ("It costs nothing", "Your own account, so the call is never metered."),
    ],
    "result_image": None,
    "what_is_heading": "What is the Google Analytics API?",
    "what_is": (
        "The Google Analytics Data API is the programmatic side of GA4: you send a property id, a "
        "date range, dimensions, metrics and optional filters, and it returns the rows the UI's "
        "Reports and Explore views are built from. It is the same data with none of the Explore "
        "date-range or sampling-pool caps, and the reason to read it through an agent is that "
        "the request body, with its dimension and metric names, is fiddly to write and easy to "
        "get subtly wrong by hand."),
    "notes": [
        "The official path to an agent on GA4 is Google's own Analytics MCP server, and its setup "
        "is a Cloud project, a service account with the API enabled, and admin-level access on "
        "the property. Here the app is treg.to's, the consent is one screen, and read access on "
        "the property is enough, which is what makes it usable by a consultant who does not own "
        "the account.",
        "Totals move with the dimensions you ask for, and that is GA4, not the relay. A metric "
        "scoped by session counts differently once you break it down by an event-scoped "
        "dimension, and the API returns exactly what the UI would for the same request. When the "
        "number disagrees with the report you remember, ask the agent to run both shapes and "
        "show the request, rather than assuming one is wrong.",
        "The API can only return what the property retains. GA4 keeps event-level data for two "
        "months by default, so a year-on-year or cohort question beyond that window comes back "
        "empty from the API exactly as it does from Explore. The Data API also enforces per "
        "property token quotas per hour and per day, so a wide report with many dimension "
        "combinations is dearer in quota than a narrow one, and the agent should ask before "
        "running a loop over every page.",
    ],
    "faq": [
        ("Does this cost anything?",
         "No. Google Analytics runs on your own Google account, so treg.to relays the call and "
         "meters nothing. Only calls on treg.to's own provider keys are billed."),
        ("Do I need a Cloud project or a service account?",
         "No. treg.to holds the Google app; you consent once with the account that can see the "
         "property, and treg.to keeps the token server side. Your agent never sees it."),
        ("Which reports can it run?",
         "Anything runReport accepts: any combination of dimensions and metrics, date ranges, "
         "filters, ordering and paging. Realtime visitors are a separate call, on the "
         "neighbouring row of the menu."),
        ("Will it fix numbers that look wrong?",
         "No. If the tagging or consent setup is feeding GA4 the wrong events, the API returns "
         "the same wrong numbers. The agent can show you the request it made, which is the first "
         "step in finding out why."),
    ],
    "voices_intro": (
        "Around 34 of the ~75 relevant posts in August 2026 were launches of one more GA4 MCP or "
        "dashboard, several posted word for word across five subreddits. These four are people "
        "asking for the thing rather than selling it."),
    "voices": [
        ("People are asking for exactly this, by name",
         "What AI agent tools can I use to connect to the Google Analytics API and retrieve data through a chat-based conversational interface",
         "r/GoogleAnalytics, 17 points", "https://www.reddit.com/r/GoogleAnalytics/comments/1vpw1m9/what_ai_agent_tools_can_i_use_to_connect_to_the/",
         "Any of them, once the agent can reach a connected property. That is what the setup line "
         "on this page does: the agent gets the report call and the token stays with treg.to."),
        ("Writing the API call by hand fails, even with help",
         "I asked 6 LLMs for code samples and I got 6 different answers that all failed to do the API call.",
         "r/dataengineering", "https://www.reddit.com/r/dataengineering/comments/1im3fpx/does_anyone_know_how_to_export_the_audience/",
         "The request body is the hard part and here nobody writes it. The agent builds it from "
         "the question, runs it, and shows it back, so a wrong dimension name is a visible "
         "mistake rather than a silent one."),
        ("The API and the interface disagree",
         "according to the interface I'm getting 2.2M event counts, whereas the API says 495k event counts for the same page.",
         "r/GoogleAnalytics", "https://www.reddit.com/r/GoogleAnalytics/comments/vwhssp/ga4_data_api_vs_interface_discrepancy/",
         "No relay can settle this, and this page will not claim to. Both numbers can be correct "
         "for two differently scoped requests. What the agent adds is the request itself, in the "
         "open, so you can see which shape produced which number."),
        ("The data is visible on screen and still out of reach",
         "I feel like I'm missing something obvious in GA4 about how to get at that data since I can SEE it right there",
         "r/GoogleAnalytics", "https://www.reddit.com/r/GoogleAnalytics/comments/1n09huf/export_daily_views_data_for_a_single_page/",
         "Daily views for one page is a two-line report on the API: a date dimension, a page "
         "filter, a views metric. Ask for it in those words and the agent returns the table, no "
         "export ritual."),
    ],
    "related": ("Search Console: clicks, impressions and top queries", "Realtime visitors on your site",
                "Is this page indexed, and why not", "Your own campaign performance"),
}

USE_CASE_PAGES["current-quote-for-a-ticker"] = {
    "label": "Current quote for a ticker",
    "sentence": "Stock price API: the current quote for a ticker from four providers, free to start",
    "title": "Stock price API: {n} providers, free to try | treg.to",
    "lede": (
        "Ask for a ticker and get the quote back as data: price, day change, open, high, low and "
        "previous close. {n} providers answer through one treg.to key. Three of them are served "
        "on treg.to's own free-tier keys, {cheapest}, then on your own key; the fourth is your own "
        "plan only. Each one says whether its quote is real time or delayed, and the page says it "
        "too, because that word is where stock APIs go wrong."),
    "prompt": "Using treg, get the current price, day change and previous close for AAPL, MSFT and "
              "NVDA. Tell me which provider you will use and whether its quote is real time or "
              "delayed before you call, and stop if the free allowance is used up.",
    "prompt_why": [
        ("Ask whether it is delayed", "One of these is 15 to 20 minutes behind by design. The agent should say which before it quotes."),
        ("One ticker is one call", "The free allowance is counted in calls per team per day, so a watchlist of fifty is a day's allowance."),
        ("Try on the allowance, build on your key", "The daily pool is for finding out which feed you want. A bot needs a key of its own."),
        ("Compare, then pick", "treg.to shows the four side by side and does not choose for you. Say which one you want, or say why."),
    ],
    "result_noun": "quote",
    "result_image": None,
    "what_is_heading": "What is a stock price API?",
    "what_is": (
        "A stock price API returns the current quote for a ticker symbol as data: last price, "
        "the day's change, open, high, low, previous close and usually volume, sometimes the "
        "52-week range. The catch is the word current. A feed is real time, delayed by an "
        "exchange-mandated window, or a single exchange's view rather than the consolidated tape, "
        "and the free tier of most providers is small enough that a script polling every minute "
        "runs out before lunch. The unofficial Yahoo Finance endpoints most free scripts lean on "
        "are not an API at all, and break without notice."),
    "notes": [
        "The free allowance is real and it is small on purpose. Finnhub is served at fifty calls "
        "per team per day on treg.to's key, Tiingo and Twelve Data at twenty each; past that the "
        "call is refused with a hint to connect your own key, and with your own key the calls "
        "are never metered. It is enough to try each feed on the tickers you care about. It is "
        "not a data plan for a trading bot, and this page will not pretend it is.",
        "Real time means four different things here. Finnhub's quote is documented as real time "
        "for US tickers. Tiingo's is the IEX feed, one exchange's top of book rather than the "
        "consolidated tape. Twelve Data returns stocks, forex and crypto in one quote shape with "
        "the 52-week range, and has a one-number price call for the cheapest possible check. "
        "EODHD's live quote is 15 to 20 minutes delayed, and since EODHD publishes no per-call "
        "rate it is served on your own EODHD plan only.",
        "Providers disagree, occasionally by a lot, and treg.to does not referee. A quote is one "
        "provider's number at one moment; a second provider on the same ticker is the cheap "
        "sanity check, and the agent can run both. Symbol formats differ too: EODHD wants an "
        "exchange suffix, AAPL.US, where the others take the bare US ticker.",
    ],
    "faq": [
        ("Is it really free?",
         "Three of the four are, up to a daily allowance per team, on treg.to's own free-tier "
         "keys: fifty calls on Finnhub, twenty each on Tiingo and Twelve Data. After that, "
         "connect your own key and the calls are never metered."),
        ("Is the quote real time?",
         "Depends on the provider, and the page says which. Finnhub is real time for US tickers, "
         "Tiingo is the IEX feed, EODHD is 15 to 20 minutes delayed. Ask the agent to name the "
         "provider before it quotes."),
        ("Can I run a trading bot on this?",
         "Not on the allowance. A bot polling every minute exhausts fifty calls before the open. "
         "Register your own key with the provider you settle on and treg.to stops counting."),
        ("What about tickers outside the US?",
         "Coverage is each provider's, not treg.to's. Finnhub's quote is documented for US "
         "tickers; EODHD and Twelve Data take exchange-suffixed symbols. Check the ticker you "
         "need on the allowance before you build on it."),
    ],
    "voices_intro": (
        "The stock API forums are a long argument about yfinance. From ~200 Reddit and X posts in "
        "August 2026, about thirty were vendors, including one founder seeding eight tweets for "
        "his own product and one listicle pasted into three subreddits. These four are people who "
        "hit the wall."),
    "voices": [
        ("The model cannot see a live price on its own",
         "It doubled my money on the first trade. Then it told me it can't see live stock prices.",
         "r/smallstreetbets, 574 points", "https://www.reddit.com/r/smallstreetbets/comments/1r883gd/i_spent_8_months_asking_claude_dumb_questions_now/",
         "The poster spent eight months wiring a quote feed into the model by hand. The setup line "
         "on this page is the short version: the agent gets four quote providers and a price "
         "shown before the call, and never holds a key."),
        ("The free library breaks and blocks you",
         "yfinance is so unreliable; any other free apis?",
         "r/algotrading, 117 points", "https://www.reddit.com/r/algotrading/comments/1kdw27f/yfinance_is_so_unreliable_any_other_free_apis/",
         "These are documented, metered APIs rather than a reverse-engineered Yahoo endpoint, "
         "which is the whole difference. The honest caveat is the allowance: free to try, your "
         "own key to run."),
        ("By the time it arrives it is stale",
         "By the time I get the data, the prices are already stale.",
         "r/algotrading, 23 points", "https://www.reddit.com/r/algotrading/comments/1jjj6cb/need_a_better_alternative_to_yfinance_any_good/",
         "Stale has a cause, and here it is named per provider: a delayed feed, or a single "
         "exchange's view. Pick the feed for the latency you need rather than discovering it "
         "from a bad fill."),
        ("Public numbers, priced like a secret",
         "How is it possible that you need to pay hundreds of dollars just to access historical data / facts that are publicly known?",
         "r/webdev, 114 points", "https://www.reddit.com/r/webdev/comments/151zk8y/is_there_any_free_stock_market_api_that_allows/",
         "Exchange licensing, mostly, and nothing on this page changes it. What the page can do "
         "is show the provider's own terms next to each other before you commit to one, and let "
         "you try three of them for nothing."),
    ],
    "related": ("Daily price history", "News for a ticker",
                "Live crypto prices and history", "Company profile and fundamentals behind a ticker"),
}

AGENTS["grok-bot"] = {
    "name": "Grok Bot",
    "h1_noun": "MCP server",
    "title": "Grok MCP server: {n} tools without keys | treg.to",
    "description": (
        "treg.to is an MCP server that gives Grok Bot {n} tools across {p} platforms: find work "
        "emails, LinkedIn profiles, creators, keyword volumes, backlinks, competitor ads. Priced "
        "per call at the provider's own rate, with no markup and no provider signup."),
    "definition": (
        "treg.to is an MCP server for Grok Bot that gives it {n} ready-to-call tools across {p} "
        "platforms: SEO data, LinkedIn and people enrichment, Reddit, YouTube, ads and e-commerce. "
        "Calls run on treg.to's own keys and are metered from a prepaid balance at the provider's "
        "rate with $0.000 markup. Every new team starts with $1.00 free, and there are no provider "
        "accounts to open."),
    # {n} is interpolated from the catalog count at render time.
    "install_steps": [
        "Give Grok this line: <b>set up treg — https://treg.to/llms.txt</b>",
        "Grok reads the skill, signs you in, and is ready to call {n} tools.",
        "Ask for what you want done. Grok searches the catalog, tells you the price, and "
        "calls the endpoint. You never hold a provider key.",
    ],
    # No install screenshot: same rule as the ChatGPT page.
    "faq": [
        ("Can Grok Bot do lead generation with this?",
         "Yes, and it is the sequence most people ask for first. A research bot or a sales bot in "
         "Grok Bot can browse, but browsing is not data. With treg.to it can build a company "
         "list by industry, size or funding, find the decision maker at each one, find and verify a "
         "work email, and pull a recent news event for the opener. The whole sequence is on the "
         "workflows page above with the receipt from a real run. Each step is priced before the bot "
         "spends, and a miss on a per-success provider costs nothing."),
        ("What research can a Grok Bot do with the catalog?",
         "Company research: funding rounds, headcount, job postings, tech stack and recent news by "
         "domain. People research: a LinkedIn profile, a person's recent posts, their work email. "
         "Market research: who is hiring for a role this month, what employees say about a company, "
         "which ads a competitor is running, keyword volumes and who ranks. Every job on the menu "
         "below is one call, priced per call."),
        ("What can it not do yet?",
         "It does not send anything: email goes through your sequencer and LinkedIn through your "
         "own account, on your own key. LinkedIn post search covers public posts through Google's "
         "index rather than LinkedIn's own feed, and a post's reactions come back a page at a time. "
         "There is no raw page-to-markdown scraper; website reading is a named-field extraction by "
         "domain. The catalog says what each tool covers on the page rather than pretending."),
        ("Is treg.to free to use in Grok Bot?",
         "Adding it is free and every new team starts with $1.00 of calls. After that, each call is "
         "metered from the team's prepaid balance at the provider's own rate, with no markup and no "
         "subscription. Calls on your team's own keys are free."),
        ("Do I need API keys from the providers?",
         "No. treg.to makes the upstream request on its own key and relays the answer, so Grok never "
         "holds a provider credential. If your team already pays for a provider, register that key "
         "and those calls are never metered."),
        ("Is this an MCP server?",
         "Yes. treg.to is an MCP server that Grok Bot connects to as a remote MCP connector. It is "
         "the same MCP server that Claude, ChatGPT, Cursor and the rest connect to, answering the "
         "same token and the same catalog."),
        ("Does treg.to pick the provider for me?",
         "No. Where several providers do the same job they are shown side by side with prices and "
         "measured reliability, and Grok (or you) chooses. treg.to does not route or fail over "
         "between them automatically."),
    ],
}

USE_CASE_PAGES["tiktok-shop-products-and-reviews"] = {
    "label": "TikTok Shop products and reviews",
    "sentence": "TikTok Shop API: search products by keyword and read a product's reviews, without a seller account",
    "title": "TikTok Shop API: {n} providers compared, from {cheapest} | treg.to",
    "lede": (
        "Give your agent a keyword and a region and get TikTok Shop's product results back as "
        "rows: title, price, seller and product id, then the reviews on any of them by id or URL. "
        "{n} providers read the public storefront through one treg.to key, from {cheapest} a call, "
        "at the provider's own rate with no markup. None of them is the seller-side Partner API, "
        "so there is no shop, no sandbox and no app review to get through first."),
    "prompt": "Using treg, search TikTok Shop US for \"matcha whisk\", show me the price per call "
              "first, then give me the top 20 products by sales with seller, price and rating, and "
              "pull the last 50 reviews on the best seller.",
    "prompt_why": [
        ("Name the region", "Every provider takes a region code and US is the one they all list as reliable. Say which market you mean."),
        ("Ask for the price first", "treg.to returns the cost before the call, so the agent can say what a 20-page pull will spend."),
        ("Go id to id", "Search returns product ids; the reviews call takes one. Two calls, no browser, no login."),
        ("Bring the analysis", "The rows carry sales counts, prices and review text. Ranking and reading them is the agent's job."),
    ],
    "result_noun": "product",
    "result_image": None,
    "q_cheapest": "Which TikTok Shop API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What is a TikTok Shop API?",
    "what_is": (
        "TikTok's own Shop API is for sellers and their partners: it needs a seller account, an "
        "app that passes review and, for many applicants, a US representative, and it manages "
        "orders and inventory rather than answering questions about the market. What buyers "
        "usually mean by a TikTok Shop API is the other thing: a way to read the public "
        "storefront as data, search results by keyword and the reviews on a product, without "
        "owning a shop. That is what the providers here do, per call, in JSON."),
    "notes": [
        "The three providers cover the job differently. TikHub and ScrapeCreators do both halves, "
        "keyword search and product reviews; JustOneAPI does search only and bills per successful "
        "call, so an error costs nothing there. TikHub's reviews call also takes a star filter and "
        "a sort rule, which saves paging through five-star noise to reach the complaints.",
        "Region is a real parameter, not a default to ignore. JustOneAPI lists nine markets, "
        "ScrapeCreators says in its own docs that US is the reliable region for reviews and that "
        "other regions may come back thin or inconsistent, and TikHub takes a region code on "
        "every call. Ask for the market you sell in and read the count that comes back.",
        "This is the public storefront, so it moves. TikTok changes the page and fights scrapers, "
        "and the providers here have had public outages when it did. treg.to shows each provider's "
        "measured success rate on live traffic where there is enough of it, and the agent picks; "
        "treg.to does not fail over between them on its own.",
    ],
    "faq": [
        ("Do I need a TikTok Shop seller account or Partner API access?",
         "No. These providers read the public storefront, not the seller-side API, so there is no "
         "seller account, sandbox, app review or US representative involved. What you cannot do "
         "this way is anything seller-side: orders, inventory, your own shop's analytics."),
        ("How much does a TikTok Shop search cost?",
         "A fraction of a cent per call at the cheapest provider, and the live rate is at the top "
         "of this page. treg.to bills the provider's own rate with $0.000 markup from your team's "
         "prepaid balance, and JustOneAPI bills only when the call succeeds."),
        ("Can I get a product's reviews?",
         "Yes, by product id or product URL, paged. Two of the three providers do reviews, and "
         "one lets you filter by star rating. Reviews TikTok has removed from the page are gone "
         "from here too."),
        ("Which provider should my agent use?",
         "Ask for the price first and let it pick, or name one. treg.to shows the providers side "
         "by side with the rate and the measured success rate; it compares, it does not route or "
         "fail over for you."),
    ],
    "voices_intro": (
        "TikTok Shop is sold harder than it is discussed: of ~140 Reddit and X posts in August "
        "2026, about seventeen vendors were pitching, including one MCP launched by five accounts "
        "reading the same script inside thirty hours. These four are people blocked at the "
        "official door or paying for a dashboard they cannot export."),
    "voices": [
        ("The official API needs a shop before it will let you test",
         "We're stuck in a loop: our app keeps getting rejected in review... but to test that authorization flow we need a sandbox test account",
         "r/TikTokshop", "https://www.reddit.com/r/TikTokshop/comments/1ufgdy3/tiktok_shop_partner_center_create_test_account/",
         "That loop is the Partner API's, and the providers here never enter it. They read the "
         "public storefront, so the first call is a keyword and a region, and the $1.00 a new "
         "team starts with is enough to see live results before deciding anything."),
        ("Everything built on it is priced for agencies",
         "Been looking into the TikTok Shop API but.... I only find those tools meant for agencies.",
         "r/TikTokshop", "https://www.reddit.com/r/TikTokshop/comments/1vhwoph/is_the_tiktok_shop_api_worth_learning_if_you_just/",
         "Per-call metering is the answer to a monthly plan you would use twice. A search is a "
         "fraction of a cent and there is no subscription, so a one-off market check costs what "
         "it costs and nothing the month after."),
        ("The dashboards will show you the data but not give it to you",
         "built a TikTok Shop research tool in Claude Code that replaces your $99/mo Kalodata subscription",
         "@mikefutia on X, 305 likes", "https://x.com/i/status/2068043305292886465",
         "The most-liked on-topic post in the research is someone doing exactly this page's job "
         "with an agent. The rows come back as JSON, so sorting by sales, price or rating and "
         "keeping the output is yours, not a plan tier."),
        ("A seller's full product list is a different question",
         "we are struggling to find a way of programmatically obtaining all product IDs for a particular seller",
         "r/webscraping", "https://www.reddit.com/r/webscraping/comments/1e7042c/tiktok_shop_product_link_scraping/",
         "This page is keyword search and reviews by product id. Search results carry the seller, "
         "so an agent can filter to one shop, but a complete catalogue for a seller is not a call "
         "on this page and it would be wrong to pretend otherwise."),
    ],
    "related": ("Amazon product detail by ASIN", "Product reviews", "Find creators by keyword",
                "A creator's profile and stats"),
}

USE_CASE_PAGES["ads-a-competitor-is-running-now"] = {
    "label": "Ads a competitor is running now",
    "sentence": "Meta Ad Library API, Google Ads Transparency Center and LinkedIn ads: what a competitor is running right now",
    "title": "Meta Ad Library API and Google Ads Transparency data | treg.to",
    "lede": (
        "Give your agent a competitor's Page, advertiser or company and get their live ads back "
        "as rows: creative text, link titles, start dates, platforms and the snapshot URL. Meta, "
        "Google and LinkedIn answer through one treg.to key, from {cheapest} at the "
        "provider's own rate with no markup, or free on the Meta Ad Library token you set up "
        "yourself. The three libraries are not alternatives to each other; the network is the "
        "choice."),
    "prompt": "Using treg, list every Facebook and Instagram ad this Page has run in the US in the "
              "last 30 days, show me the price first, then group them by landing page and tell me "
              "which creatives have been live longest.",
    "prompt_why": [
        ("Give the Page id, not the name", "Many Pages share a brand name. A Page id or URL returns one advertiser; a name returns false positives."),
        ("Say the country", "The Meta library asks which country the ad reached, and US-only can miss half of a global brand's actives. Say ALL when you mean all."),
        ("Read longevity, not spend", "Meta publishes spend only for political ads. How long a creative has run is the signal everyone uses instead."),
        ("Ask for the price first", "treg.to returns the cost before the call, so the agent can say what a full library pull will spend."),
    ],
    "result_noun": "ad",
    "result_image": None,
    "q_cheapest": "Which ad library API is cheapest?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare, per network?",
    "what_is_heading": "What is the Meta Ad Library API?",
    "what_is": (
        "The Meta Ad Library is the public record of every ad running on Facebook and Instagram, "
        "and the Ad Library API is its official query interface: search by keyword or by Page, "
        "filter by country, date and status, and get the creative, the delivery dates and the "
        "platforms back as data. Google's equivalent is the Ads Transparency Center, which has "
        "no official API and lists ads per advertiser; LinkedIn's is its ad library. The "
        "providers here answer all three, either through the official API on your own token or "
        "by reading the public library pages."),
    "notes": [
        "The official Meta route is free and it is yours to set up: a one-time government-ID "
        "verification at Facebook, then a Meta app and an access token, which you register with "
        "treg.to as your own key. The forum wait for verification runs from days to never. The "
        "two scraper providers on the Meta row need none of that and bill per call or per ad, "
        "which is why most people start there and register a token later, if ever.",
        "Spend is not on the page for ordinary ads. Meta publishes spend and impression ranges "
        "for political and issue ads only, and richer fields for ads reaching the EU under the "
        "DSA, so a European pull carries more than a US one. For everything else the working "
        "signals are how long a creative has run and how many variants a Page is testing.",
        "Google is a lookup, not a search. The Transparency Center lists what one advertiser runs, "
        "by advertiser or domain, and that is what the SerpApi engine here returns, with region, "
        "format and date filters. There is no keyword search across every advertiser, which is a "
        "limit of the Center, and the copy of a search ad comes back as text only where Google "
        "renders it as text.",
    ],
    "faq": [
        ("Do I need Meta Ad Library API access?",
         "Not to start. ScrapeCreators and Apify read the public library without any Meta app, "
         "billed per call or per ad. If you have completed Meta's identity verification and hold "
         "a token, register it and the official calls run free on your own key, never metered."),
        ("Can I see how much a competitor is spending?",
         "Only for political and issue ads, where Meta publishes ranges. For everything else no "
         "provider has the number, and this page will not invent one. Run length, variant count "
         "and the platforms an ad reaches are what the data does carry."),
        ("Does this cover Google Ads?",
         "Yes, per advertiser, through the Ads Transparency Center engine at SerpApi's flat "
         "per-search rate. You look up an advertiser or a domain; searching every advertiser for "
         "a keyword is not something the Center offers."),
        ("Which provider should my agent use?",
         "The network decides the shelf; on Meta, the cheapest verified row or your own token. "
         "treg.to shows the rows side by side with the rate and measured success; it compares, "
         "it does not route or fail over for you."),
    ],
    "voices_intro": (
        "Ad intelligence is a crowded shelf: about seventeen tools were pitched across the ~65 "
        "on-topic posts on Reddit and X in August 2026, including one founder posting a dozen "
        "times. These four are people who tried the official libraries first."),
    "voices": [
        ("Meta's identity check is the first wall, and it does not always answer",
         "I've sent my id however 4 days passed and there is no answer. It is still \"in progress\".",
         "r/FacebookAds", "https://www.reddit.com/r/FacebookAds/comments/1vr13ds/",
         "The official API waits on that check; the scraper rows do not. Start on a per-call "
         "provider today, and register your own Meta token when the verification lands so the "
         "official calls run free."),
        ("There is no spend number, so people watch launch velocity instead",
         "Meta doesn't expose dollar spend for non-political ads, so \"velocity\" = ad-launch volume not actual budget",
         "r/FacebookAds", "https://www.reddit.com/r/FacebookAds/comments/1t045tp/",
         "True, and no provider here changes it. What the rows do carry is start date, stop date "
         "and platform per ad, which is enough for an agent to count new creatives a week and "
         "flag the ones that have survived a month."),
        ("Google only lets you look at one advertiser at a time",
         "Google only lets you look up one advertiser at a time. There's no way to answer \"who is running ads on roof repair right now?\"",
         "r/marketingagency", "https://www.reddit.com/r/marketingagency/comments/1ve8pke/",
         "Still the case through the API, because it is the Center's own shape. The honest "
         "workaround is a list: give the agent the advertisers you already know and let it "
         "loop, at a flat rate per lookup."),
        ("The library gives you data, not intelligence",
         "The Ad Library gives you data, but not much intelligence. You end up opening hundreds of ads manually",
         "r/FacebookAds", "https://www.reddit.com/r/FacebookAds/comments/1vi52qj/",
         "The rows come back as JSON with the creative text, so grouping by landing page, "
         "clustering hooks and ranking by run length is the agent's job on top, and it is the "
         "part that used to be a weekend of tabs."),
    ],
    "related": ("Your own campaign performance", "Keywords a domain bids on",
                "A competitor's recent posts", "Google results for a keyword"),
}

USE_CASE_PAGES["read-and-post-in-your-slack-channels"] = {
    "label": "Read and post in your Slack channels",
    "sentence": "Slack bot API: your agent reads your channels and posts in them, on your own workspace",
    "title": "Slack MCP and bot API for {agent}: read and post | treg.to",
    "lede": (
        "Install one bot in the Slack workspace you already run and your agent can read a "
        "channel's recent messages and post replies into it, as the bot, with no token in the "
        "agent's hands. It is your own workspace, so treg.to never meters it; and the scopes, the "
        "manifest and the bot-versus-user-token question are settled before you start, which is "
        "where most of the forum threads stall."),
    "prompt": "Using treg, read the last two days of #support, summarise the open questions with "
              "who asked each one, and post the summary to #support-digest as a single message.",
    "prompt_why": [
        ("Install once", "A pre-filled manifest creates the bot with a fixed scope set. You paste one bot token; the agent never sees it."),
        ("Invite the bot first", "A bot reads only channels it is a member of. It can post to any public channel without joining."),
        ("Read in windows", "History comes back in pages with a cap per call and a rate tier per minute. Ask for a window, not the archive."),
        ("It costs nothing", "Your own workspace, so the call is never metered."),
    ],
    "result_image": None,
    "what_is_heading": "What is the Slack bot API?",
    "what_is": (
        "The Slack Web API is how an app reads and writes a workspace as data: list channels, "
        "read a channel's message history, post a message or a threaded reply. A bot token is "
        "the credential an installed app holds, scoped to that one workspace and to the "
        "permissions its manifest asked for, and everything it does shows up as the bot, not as "
        "you. That is the token this connection uses, so the agent reads what the bot can see "
        "and posts under the bot's name, never under yours."),
    "notes": [
        "Reading history on a new, unlisted app is rationed. Apps created after May 2025 that "
        "are not listed in the Slack Marketplace get one history request a minute and fifteen "
        "messages a call, and the bot you install from treg.to's manifest is one of those apps. "
        "Nothing here lifts that; treg.to relays the call and the 429 as they are. A daily "
        "digest of a busy channel is fine; a backfill of a year is not this tool.",
        "Membership is the read permission. The bot reads only channels it has been invited to, "
        "and direct messages need their own scopes, which the manifest does not request. Posting "
        "is broader: the write scope lets it post into any public channel without joining. "
        "Slack answers a dead channel or a missing scope with HTTP 200 and ok: false, so read "
        "the body, not the status.",
        "History returns top-level messages with authors as user ids, so a readable digest needs "
        "the users call to resolve names, and thread replies live behind a separate replies call. "
        "Ask the agent for a summary with names and it will make those calls; ask for the raw "
        "rows and it will hand you ids.",
    ],
    "faq": [
        ("Does this cost anything?",
         "No. Slack runs on your own workspace, so treg.to relays the call and meters nothing. "
         "Only calls on treg.to's own provider keys are billed."),
        ("Does my agent post as me or as a bot?",
         "As the bot, always. The connection holds a bot token, which is what makes it "
         "auditable: every message it writes carries the bot's name, and it cannot read your "
         "DMs or anything a channel you did not invite it to."),
        ("Why is reading history slow?",
         "Slack rate-tiers history reads on unlisted apps: one request a minute, fifteen messages "
         "each. That is Slack's rule for every app created outside the Marketplace since 2025, "
         "and treg.to does not get around it. Ask for windows and let the agent page."),
        ("What if my workspace restricts app installs?",
         "Then an admin has to approve the install, the same as any app. treg.to gives you the "
         "manifest to submit; it cannot approve it for you."),
    ],
    "voices_intro": (
        "Slack posts in August 2026 were four-fifths agent launches and MCP listicles; about "
        "twenty tools were pitched across ~170 posts, one with zero-width characters in the "
        "text. These four are people trying to read their own workspace."),
    "voices": [
        ("Slack cut history reads to one a minute, and it applies to internal apps",
         "For accessing messages, you can now only make 1 request per minute, with a maximum of 15 messages",
         "@grinich on X, 1,951 likes", "https://x.com/i/status/1946391052489028082",
         "Still the rule for any unlisted app, and this connection is one. The page says so "
         "rather than implying a way round it: a summary of a channel's last day fits the "
         "allowance, and that is the job most people are actually asking for."),
        ("All anyone wants is the morning summary, and the easy path got switched off",
         "All I want is to get a summary of my Slack messages and with a list of action items every morning.",
         "r/AI_Agents", "https://www.reddit.com/r/AI_Agents/comments/1txzeow/",
         "The same poster found the Claude Slack connector disabled by IT and was unsure they "
         "could create an app at all. A manifest install is still an app install, so an admin "
         "may have to say yes; after that the digest is one read and one post a day."),
        ("The bot has to be in the room",
         "Turns out the bot needs to be a member of the channel first.",
         "r/indiehackers, 18 points", "https://www.reddit.com/r/indiehackers/comments/1vryo4q/",
         "True here too, and the manifest keeps it that way on purpose: read what you invited it "
         "to, post where you point it. The same thread had DM scopes rejected in review as too "
         "much access, which is why this connection does not ask for them."),
        ("Pasting bot tokens into code is where the audit trail goes",
         "pasting bot tokens into your code... less great when you need to know exactly what your agent can touch",
         "@Mai_Builds on X", "https://x.com/i/status/2091946621504311753",
         "The token sits with treg.to, the agent holds one team token that works for every tool, "
         "and what it can touch is the manifest's scope list, which is fixed and readable. Revoke "
         "the bot and the agent's access is gone with it."),
    ],
    "related": ("Read a Telegram channel", "Publish to your own accounts",
                "Google Analytics: traffic and behaviour reports",
                "Search Console: clicks, impressions and top queries"),
}

USE_CASE_PAGES["is-this-page-indexed-and-why-not"] = {
    "label": "Is this page indexed, and why not",
    "sentence": "Google index checker: is this page indexed, and why not, from the URL Inspection API",
    "title": "Google index checker: URL Inspection API for {agent} | treg.to",
    "lede": (
        "Connect the Search Console property you already own and your agent can ask Google, per "
        "URL, the question the UI answers one click at a time: is it indexed, when was it last "
        "crawled, which canonical did Google choose, and if it is not indexed, which bucket it "
        "sits in. It runs on your own Google account, so treg.to never meters it, and it is the "
        "same URL Inspection data the Search Console panel shows, without the clicking."),
    "prompt": "Using treg, take every URL in our sitemap, inspect each one in Search Console, and "
              "give me a table of the ones that are not indexed with the coverage state, last "
              "crawl date and the canonical Google picked.",
    "prompt_why": [
        ("Connect once", "One OAuth click for the property you own. treg.to holds the token, not you."),
        ("Give it the property string", "The URL must sit under the property you name, sc-domain or URL-prefix. A mismatch is a permission error, not a result."),
        ("Mind the quota", "Google allows 2,000 inspections per property per day, and nothing lifts it. A 5,000-page site is three days."),
        ("It costs nothing", "Your own account, so the call is never metered."),
    ],
    "result_image": None,
    "what_is_heading": "What is the URL Inspection API?",
    "what_is": (
        "The URL Inspection API is the Search Console endpoint behind the URL Inspection tool. "
        "For one URL on a property you own it returns Google's index verdict, the coverage "
        "state (indexed, crawled but not indexed, discovered but not indexed, and the rest), the "
        "last crawl date, the robots and indexing state, the canonical you declared against the "
        "one Google chose, and the mobile and rich-result checks. It is a read. It does not ask "
        "Google to index anything, and it is not the Indexing API, which is a different endpoint "
        "restricted to job postings and live-broadcast pages."),
    "notes": [
        "The quota is Google's and it is hard: 2,000 inspections per property per day. treg.to "
        "does not pool, rotate or route around it, and the forum's tales of getting past it "
        "involve multiple properties or policy violations. Inspect the pages that matter, keep "
        "the verdicts, and diff them week to week; that is where the tool earns its place.",
        "This is the fresher of the two Search Console answers. The Page Indexing report lags and "
        "its validations can sit for months, which is why people see it disagree with the URL "
        "Inspection panel. The API reads the inspection side, so a scripted check gives the "
        "per-URL answer without waiting for the report to catch up.",
        "Two things this page does not do. It does not request indexing: that button has no API "
        "and its own small daily limit. And it is not the Indexing API, which Google restricts to "
        "JobPosting and BroadcastEvent pages; using it for anything else is against policy and "
        "returns a cheerful 200 that changes nothing.",
    ],
    "faq": [
        ("Does this cost anything?",
         "No. Search Console runs on your own Google account, so treg.to relays the call and meters "
         "nothing. Only calls on treg.to's own provider keys are billed."),
        ("Can it make Google index my page?",
         "No. The URL Inspection API reads Google's verdict; it does not submit anything. It "
         "tells you which bucket a page is in and why, which is the part you can act on."),
        ("Is this the Google Indexing API?",
         "No. The Indexing API is for job postings and live-broadcast pages only. Most people "
         "searching for it want what this page does: check whether a URL is indexed, in bulk, "
         "and see the reason when it is not."),
        ("How many URLs can I check a day?",
         "2,000 per Search Console property per day, set by Google. The agent can loop a sitemap "
         "and stop at the quota, then carry on the next day."),
    ],
    "voices_intro": (
        "Indexing is a queue of people waiting on Google and a shelf of tools promising to jump "
        "it: about a dozen index-checker and indexing-service pitches sat among ~70 on-topic "
        "Reddit and X posts in August 2026. These four are people checking their own pages."),
    "voices": [
        ("The two Search Console reports disagree",
         "URL Inspection says Indexed, but Page Indexing says 'Crawled – currently not indexed.'... Which status should we trust more?",
         "r/SEO, 12 points", "https://www.reddit.com/r/SEO/comments/1ume4w0/",
         "The inspection side is the fresher one, and it is what the API reads. A scripted "
         "pass over the affected URLs gives you the current verdict per page instead of a "
         "report that updates on its own schedule."),
        ("Crawled, not indexed, for months, and validation never finishes",
         "Crawled - currently not indexed for almost 2 months. Validation Started - Started: 5/29/26 and still nothing.",
         "r/TechSEO, 70 points", "https://www.reddit.com/r/TechSEO/comments/1uzt1t0/",
         "No API makes Google decide faster, and this page will not claim one does. What it "
         "gives you is the per-URL state, the last crawl date and the canonical Google chose, "
         "kept over time, so you can see which pages moved and which did not."),
        ("The Indexing API turns out to be for job postings",
         "Google Search API can not be used because it only supports categories related to broadcasting and job postings.",
         "r/SEO, 32 points", "https://www.reddit.com/r/SEO/comments/1robehb/",
         "Correct, and it is why the most-searched term for this job names the wrong API. The "
         "URL Inspection API is the one that answers 'is this indexed', for any page on a "
         "property you own."),
        ("The 2,000 a day quota is the whole constraint",
         "The big limitation of the insufferable URL inspection API is the 2,000 daily quota.",
         "@iannuttall on X", "https://x.com/i/status/1734591329953276340",
         "It is, and it is per property, set by Google. treg.to relays the call as it is, so the "
         "quota is yours to spend well: inspect the pages that changed, not the whole site every "
         "morning."),
    ],
    "related": ("Search Console: clicks, impressions and top queries", "Keywords a domain ranks for",
                "On-page audit of a URL", "Google results for a keyword"),
}

USE_CASE_PAGES["news-for-a-ticker"] = {
    "label": "News for a ticker",
    "sentence": "Stock news API: the headlines on a ticker over a date range, as rows your agent can read",
    "title": "Stock news API for {agent}: headlines by ticker | treg.to",
    "lede": (
        "Give your agent a ticker and a date range and get the news on that company back as "
        "rows: headline, source, summary, time and the link. Finnhub answers it, {cheapest}, so "
        "the first question of every forum thread, which free news API is worth trying, costs "
        "nothing to settle. What comes back is headlines and summaries, not full text and not a "
        "sentiment score; the reading is the agent's job, and it is the part it is good at."),
    "prompt": "Using treg, get the news on NVDA from the last seven days, drop anything that is not "
              "about the company itself, and give me the five stories that matter with one line "
              "each and the source link.",
    "prompt_why": [
        ("Give a symbol and two dates", "The call takes a ticker and a from and to date. A week is a page; a year is a lot of pages."),
        ("Ask it to filter", "Finnhub tags stories to a symbol and the tagging is loose. Ask the agent to drop the ones that only mention the sector."),
        ("Score it yourself", "The rows carry the headline and summary. Sentiment, themes and 'does this matter' are the agent's to judge."),
        ("Try free, run on your key", "The daily allowance is for finding out whether the feed suits you. A bot that polls all day needs your own Finnhub key, never metered."),
    ],
    "result_image": None,
    "what_is_heading": "What is a stock news API?",
    "what_is": (
        "A stock news API returns the news articles tagged to a company as data, by ticker and "
        "date range, so a program can read them instead of a person scrolling a feed. Each row is "
        "a headline, the source, a summary, a timestamp and the article's URL. It is not the "
        "article itself, which stays on the publisher's site, and it is not a verdict on whether "
        "the news is good; providers that sell sentiment scores layer that on top, and this "
        "endpoint does not."),
    "notes": [
        "The free allowance is a trial, not a plan. treg.to serves Finnhub's company news on its "
        "own free-tier key at 50 calls a day per team; past that the call is refused with a hint "
        "to connect your own key, and on your own key nothing is metered. Finnhub's free tier "
        "carries its own rate limit and, per Finnhub's own docs, this endpoint covers North "
        "American companies, so a UK or Indian ticker is not this tool's job.",
        "Symbol tagging is Finnhub's, and it is generous. A story about the chip sector arrives "
        "under every chip ticker, and at least one builder in the research shipped wrong-symbol "
        "rows by trusting the scoping. Ask the agent to check that the headline is about the "
        "company, and to keep the article URL so a human can verify the ones that count.",
        "This is headlines and summaries, delivered after publication, not a real-time wire. "
        "The forum wants full text from the paywalled sources and a sentiment number; no free "
        "feed gives either, and this page will not pretend to. What the rows are good for is "
        "the morning brief, the earnings-week watch, and a pre-trade sanity check on what "
        "happened overnight.",
    ],
    "faq": [
        ("Is the stock news API free?",
         "50 calls a day per team on treg.to's own Finnhub key, free, which is enough to try it "
         "and to run a daily brief on a handful of tickers. Beyond that, register your own "
         "Finnhub key and the calls are never metered by treg.to."),
        ("Does it include sentiment?",
         "No. The rows are headline, source, summary, time and URL. Sentiment is the agent's "
         "reading, or a separate provider's product; this endpoint returns the news, not a "
         "score on it."),
        ("Do I get the full article?",
         "No. You get the headline, a summary and the link. Full text from paywalled outlets is "
         "not something any news API on the free side hands out, and the link is there so a "
         "person can read the ones that matter."),
        ("Can I build a trading bot on it?",
         "Not on the free allowance, which is a daily trial, and not as a real-time signal: the "
         "feed is post-publication. On your own key a polling bot is between you and Finnhub's "
         "limits; the news is an input for the agent to reason over, not a trigger."),
    ],
    "voices_intro": (
        "Stock news threads are half vendors: about 30 of the ~190 Reddit and X posts in August "
        "2026 were news APIs pitching themselves, two of them in five subreddits each with the "
        "same 'I polled the free ones and then found this' arc. These four are people building "
        "on their own."),
    "voices": [
        ("The first thing the model admits is that it cannot see the market",
         "Then on the second day ChatGPT told me, \"Uh... I can't actually see live stock prices.\"",
         "r/ClaudeAI, 432 points", "https://www.reddit.com/r/ClaudeAI/comments/1r35gpb/",
         "That poster spent eight months wiring data in by hand, and Finnhub's news was one of "
         "the feeds. One setup line gives the agent the same call, with the price of every "
         "other tool shown before it spends anything."),
        ("Free tiers are for trying, not for the backtest",
         "Their free tiers don't provide enough history for a proper multi-year backtest.",
         "r/algotrading, 13 points", "https://www.reddit.com/r/algotrading/comments/1vp74rb/",
         "True of this one as well. The allowance settles whether the feed fits; a multi-year "
         "pull is your own key and Finnhub's plan, and the page says so instead of hiding the "
         "cliff behind a free badge."),
        ("Nobody knows which news feed is actually current",
         "I have seen some reviews of Polygon.io saying their news feed is outdated by months",
         "r/algotrading, 17 points", "https://www.reddit.com/r/algotrading/comments/1i0ghfd/",
         "No comparison table can answer that honestly for your tickers. The cheap test is the "
         "point of the allowance: pull last week on five symbols today and read the timestamps "
         "yourself."),
        ("The tagging puts stories under the wrong symbol",
         "company news sometimes gets tagged with the wrong symbol because I'm trusting Finnhub's endpoint scoping instead of double-checking it.",
         "X", "https://x.com/i/status/2078351559344128383",
         "A known property of the feed, so make the agent do the double-checking: keep the "
         "headline and summary, drop what is not about the company, and carry the URL for "
         "anything it flags as material."),
    ],
    "related": ("Current quote for a ticker", "Daily price history",
                "Company profile and fundamentals behind a ticker", "A company's SEC filings"),
}


USE_CASE_PAGES["live-crypto-prices-and-history"] = {
    "label": "Live crypto prices and history",
    "sentence": "CoinGecko API and Tiingo through one key: a crypto price API for live prices and history, per call",
    "title": "CoinGecko API for {agent}: crypto prices and history | treg.to",
    "lede": (
        "Give your agent a coin and get its price back as data: the current price in any quote "
        "currency for a whole watchlist in one call, or the price, market cap and volume series "
        "over a day, a month or the coin's whole life. CoinGecko and Tiingo answer through one "
        "treg.to key, from {cheapest} at the provider's own rate with no markup, and there is no "
        "CoinGecko key to paste into a spreadsheet, a dashboard or a device. What comes back is "
        "an aggregate price, not an order book, and it is polled, not streamed."),
    "prompt": "Using treg, get the current price and 24h change for bitcoin, ethereum and solana in "
              "USD in one call, then pull the last 30 days of daily prices for each and tell me "
              "which one moved most against its 30-day average. Show me the price per call first.",
    "prompt_why": [
        ("Use CoinGecko coin ids", "The call takes ids like bitcoin and ethereum, not tickers. BTC is not a coin id, and several tokens share a symbol."),
        ("Ask for the whole watchlist at once", "Current price takes a comma-separated list of coins and currencies. One call for fifty coins costs the same as one call for one."),
        ("Say the window", "History takes 1, 7, 30, 365 or max days, and the granularity follows the window: five-minute points under a day, hourly under 90 days, daily beyond."),
        ("Ask for the price first", "treg.to returns the rate before the call, so the agent can say what a daily refresh of a hundred coins will spend."),
    ],
    "result_noun": "price",
    "result_image": None,
    "q_cheapest": "Which crypto price API is cheapest per call?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do CoinGecko and Tiingo compare?",
    "what_is_heading": "What is the CoinGecko API?",
    "what_is": (
        "The CoinGecko API is the query interface to CoinGecko's aggregate market data: a "
        "current price for any listed coin in any quote currency, with market cap, 24h volume and "
        "24h change on request, and a history endpoint that returns the price, market cap and "
        "volume series for a coin over a window you choose. The number is an aggregate across "
        "the exchanges CoinGecko tracks, which is what a dashboard or an agent wants and what a "
        "trader on one exchange does not. Tiingo is the other provider here and answers a "
        "different shape: open, high, low, close and volume bars for a pair like btcusd, at a "
        "resample frequency from a minute to a day."),
    "notes": [
        "The rate is per request, not per coin. CoinGecko bills one credit for a successful "
        "call whatever the endpoint, and the current-price call takes a list of coin ids and a "
        "list of quote currencies, so the forum's \"it only returns 28 of my 131 coins\" is a "
        "pagination problem on a different endpoint, not a ceiling here. On treg.to's own key "
        "that credit is metered from your team's balance at the provider's rate; on your own "
        "CoinGecko key nothing is metered and the plan's limits are between you and CoinGecko.",
        "History depth is CoinGecko's, and the page will not stretch it. The series call "
        "returns per-coin prices over 1, 7, 30, 365 or max days with granularity set by the "
        "window, and the snapshot call returns one coin's price, market cap and volume on a "
        "date. Neither is a total-market-cap history and neither is a top-N-by-day table; an "
        "agent builds that by looping coins, one metered call each. Tiingo's bars are the "
        "exchange-style alternative, served free on treg.to's key at 20 calls a day per team, "
        "then on your own Tiingo key.",
        "The price is an aggregate and it passes through unchanged. CoinGecko's market cap and "
        "supply figures are its own, and the research has real examples of both being wrong "
        "for small tokens; treg.to relays the answer verbatim and adds no consensus, no spread "
        "and no second opinion. If a number matters, pull the Tiingo bar for the same pair and "
        "let the agent compare, which is a comparison you make, not one treg.to makes for you. "
        "Nothing here is a websocket, an order book or an exchange connection.",
    ],
    "faq": [
        ("Is the CoinGecko API free through treg.to?",
         "Not free, cheap: one CoinGecko credit per successful call, metered from your team's "
         "prepaid balance at CoinGecko's own rate with $0.000 added. Each new team gets $1.00 to "
         "start, which is a lot of price calls. Register your own CoinGecko key and the calls "
         "are never metered."),
        ("How far back does the history go?",
         "As far as CoinGecko's series for that coin goes, with days set to max, at daily "
         "granularity. Sub-daily points come only inside the recent windows: five-minute under "
         "a day, hourly under 90 days. Tiingo's bars go down to one minute for the pairs it "
         "carries, on its own history."),
        ("Can my agent stream prices or trade on them?",
         "No. Both providers here answer a request with a number; there is no websocket, no "
         "order book and no exchange execution. An agent that refreshes a watchlist every few "
         "minutes is the fit; a bot that needs a tick feed is not."),
        ("Which provider should my agent use?",
         "CoinGecko for coin ids, quote currencies and the aggregate market view; Tiingo for "
         "OHLCV bars on a pair. treg.to shows both with the rate and the measured success side "
         "by side; it compares, it does not route or fail over for you."),
    ],
    "voices_intro": (
        "Crypto data threads are two-thirds noise: of the ~170 Reddit and X posts read in August "
        "2026, most were token shills, subscription resellers and two 'finally found a free "
        "API' posts with the same arc in two subreddits. These five are people building "
        "something on a price feed."),
    "voices": [
        ("The free tier's rate limit is the first thing that breaks",
         "There I ran into a problem with Rate limit from the Coingecko api itself.",
         "r/coingecko", "https://www.reddit.com/r/coingecko/comments/1cibixi/google_sheet_not_working_rate_limits/",
         "That poster was calling from Google Sheets, where every request leaves a shared "
         "Google IP. Through treg.to the call is one metered request on a paid key, a whole "
         "watchlist per call, and the sheet, the dashboard or the device holds no key at all."),
        ("Twelve months is where the free history stops",
         "CoinGecko only goes back 12 months, but I was hoping to go back further",
         "r/CryptoCurrency", "https://www.reddit.com/r/CryptoCurrency/comments/1kq99z1/historic_market_cap_data/",
         "Per coin, the series call with days set to max returns the daily history CoinGecko "
         "holds, on the paid key treg.to serves it from. That poster wanted total market cap "
         "history, which is not this endpoint, and the page will not pretend it is."),
        ("Free is for testing; what happens at scale is the question",
         "Free APIs are fine for testing but I want something that scales.",
         "r/ethdev, 6 points", "https://www.reddit.com/r/ethdev/comments/1rf4ehy/best_crypto_market_data_api_for_real_time/",
         "Scale here means one credit a call, metered, with no plan tier to jump when the "
         "dashboard grows. What it does not mean is streaming: the feed is polled, and a "
         "bot that needs every tick is on a different product."),
        ("Nobody's number agrees with anybody else's",
         "CoinGecko API shows wrongs market capital data for 700M !",
         "r/SmoothLovePotion", "https://www.reddit.com/r/SmoothLovePotion/comments/sq6odb/coingecko_api_shows_wrongs_market_capital_data/",
         "No comparison table can answer that. The price is the provider's aggregate and "
         "treg.to relays it as is. The cheap check is to pull the same pair as a Tiingo bar "
         "and let the agent flag the gap, which it can do in the same prompt."),
        ("The agent wrote the rate limiter",
         "until I had ChatGPT incorporate rate-limiting given the 500 rate limit/min",
         "r/api_connector", "https://www.reddit.com/r/api_connector/comments/1dg1of5/coingecko_api_x_mixed_analytics_crypto_tracker/",
         "The agent already does the plumbing. What it lacks is the key and the price, and one "
         "setup line gives it both: the call, the rate shown before it spends, and no "
         "CoinGecko token in its context."),
    ],
    "related": ("Current quote for a ticker", "Daily price history",
                "Coins trending right now", "News for a ticker"),
}


USE_CASE_PAGES["employee-reviews-of-a-company"] = {
    "label": "Employee reviews of a company",
    "sentence": "Glassdoor API and Glassdoor scraper, through one key: a company's employee reviews as rows your agent can read",
    "title": "Glassdoor API for {agent}: employee reviews as data | treg.to",
    "lede": (
        "Give your agent a company and get its employee reviews back as rows: rating, title, "
        "pros, cons, date and whether the reviewer still works there. Glassdoor closed its "
        "partner API, so the two providers here read the reviews for you, by company website "
        "or by Glassdoor page URL, from {cheapest} at the provider's own rate with no markup. "
        "There is no Glassdoor login, no bot wall and no give-to-get review to write first. "
        "What comes back is what Glassdoor shows; nobody here vets the reviewers."),
    "prompt": "Using treg, pull the last 100 employee reviews for canva.com, show me the price "
              "first, then split them into current and former staff, give me the three "
              "complaints that recur most in the cons, and flag any month where the rating "
              "dropped sharply.",
    "prompt_why": [
        ("Give the website, or the Glassdoor URL", "Akta keys on the company's website; Bright Data on the Glassdoor page URL. Say which one you have and the agent picks the row that takes it."),
        ("Ask for a count you can afford", "Akta bills per 50 reviews returned and defaults to 10. A hundred reviews is a few cents; every review a big employer has is not."),
        ("Split current from former", "Each row carries the reviewer's status. The two groups tell different stories, and a mean over both hides both."),
        ("Ask for the price first", "treg.to returns the rate before the call, so the agent can say what a list of fifty companies will spend."),
    ],
    "result_noun": "review",
    "result_image": None,
    "q_cheapest": "Which Glassdoor reviews provider is cheapest per review?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do Akta and Bright Data compare?",
    "what_is_heading": "Is there a Glassdoor API?",
    "what_is": (
        "Not one you can sign up for. Glassdoor ran a partner API and stopped taking new "
        "partners, so the phrase on the forums, Glassdoor API, now means one of two things: a "
        "scraper you run yourself against pages that block plain requests and render reviews "
        "through GraphQL, or a data provider that does the reading and returns rows. The two "
        "providers here are the second kind. Akta returns employee reviews and ratings for a "
        "company by its website, so a list of company names and domains is enough; Bright "
        "Data's Glassdoor reviews dataset takes the Glassdoor company page URL and returns "
        "the records, pay per record delivered."),
    "notes": [
        "The input decides the provider. Akta takes a company website, which is what a "
        "spreadsheet of targets usually has, and returns reviews in pages of up to 100. Bright "
        "Data wants the Glassdoor page URL itself, and finding that URL from a company name is "
        "the step the forum's scrapers kept failing at. If you have websites, start at Akta; "
        "if you have Glassdoor URLs, either row works.",
        "Neither endpoint has been called live through treg.to yet, and the page says so "
        "rather than hiding it. Akta's rate is documented as 1.5 credits per 50 reviews, Bright "
        "Data's as $1.50 per 1,000 records on a pay-per-success basis, and both are the "
        "provider's own rate with $0.000 added. Bright Data's scraper answers within a minute "
        "for a handful of URLs and falls back to a snapshot id past that, so a long list is a "
        "job to poll, not a single call.",
        "Rows are what Glassdoor shows, not what is true. The research has retail investors "
        "using review scrapes for due diligence and hiring managers reading the cons before an "
        "offer, and it also has the same people asking how many reviews are bots or bought. No "
        "provider here answers that. What the rows do carry is date, status and sub-ratings, "
        "which is enough for an agent to weight recent reviews, separate current staff from "
        "former, and show its working.",
    ],
    "faq": [
        ("Does Glassdoor have a public API?",
         "No. The partner programme stopped accepting new partners, which is why the people "
         "who asked for access on the forums were turned away. The providers here read the "
         "public review pages for you and return rows; you never fetch glassdoor.com yourself."),
        ("Do I need a Glassdoor account?",
         "No. Both calls run on treg.to's own key and return review text without a login, so "
         "the sign-up wall that asks you to review your own employer first does not apply to "
         "the agent. Register your own Akta or Bright Data key and the calls are never metered."),
        ("Can it tell me which reviews are fake?",
         "No, and this page will not pretend otherwise. The rows are Glassdoor's, unvetted. "
         "Use the date and the current-or-former flag to weight them, and treat a cluster of "
         "five-star reviews in one week as a question, not an answer."),
        ("Which provider should my agent use?",
         "The one whose input you hold: website for Akta, Glassdoor URL for Bright Data. "
         "treg.to shows both with the rate side by side; it compares, it does not route or "
         "fail over for you."),
    ],
    "voices_intro": (
        "Glassdoor threads split in two: people who want the data and people who hate the "
        "sign-up wall. Of the ~165 Reddit and X posts read in August 2026, about 25 were "
        "organic and on the job; the vendor share was scraper launches, one identical "
        "cross-post in two subreddits, and a review-removal service. These five are the "
        "people doing the work."),
    "voices": [
        ("The API everyone reaches for first is closed",
         "I was planning to use Glassdoor’s API to gather this data, but unfortunately, they’ve stopped API partnerships for now.",
         "r/learnprogramming", "https://www.reddit.com/r/learnprogramming/comments/1j8k0ji/glassdoor_api_access_denied_any_alternatives_for/",
         "Still closed, and the page does not know a way in. What it does have is two "
         "providers that return the reviews as rows without a Glassdoor app, priced per "
         "review or per record, with the rate shown before the call."),
        ("A company name is not a Glassdoor URL",
         "For Glassdoor it is more complicated. I cannot just add the company website to the Glassdoor url to locate and scrape the correct page",
         "r/learnpython", "https://www.reddit.com/r/learnpython/comments/wcxmdk/glassdoor_web_scraping/",
         "That is the exact input Akta takes: the company website, no Glassdoor URL needed. "
         "A dataframe of names and domains is the whole job, one call per company."),
        ("Is it even worth trying to scrape it for free?",
         "is it possible to scrape Glassdoor reviews (completely free). I don’t want to waste my time if I can’t.",
         "r/webscraping", "https://www.reddit.com/r/webscraping/comments/13ef7yh/glassdoor_reviews/",
         "The honest answer from the same thread is that plain requests get a bot page. "
         "Through treg.to it is not free, it is a fraction of a cent per review at the "
         "provider's rate, and the first dollar is on the house for a new team."),
        ("Nobody knows how many of the reviews are real",
         "there is not much transparency about how many reviews on the website are made by nefarious actors (e.g. bots).",
         "r/RKLB, 13 points", "https://www.reddit.com/r/RKLB/comments/143o18g/glassdoor_reviews_vacancies_analysis/",
         "No comparison table can answer that, and neither provider vets a reviewer. What "
         "an agent can do is what that investor did by hand: pull the rows, weight by date "
         "and status, and compare the shape against peers."),
        ("The rating is a hiring signal, whether or not it is fair",
         "developers are keen to weed out companies with ratings like this.",
         "r/cscareerquestions, 1,108 points", "https://www.reddit.com/r/cscareerquestions/comments/sofqaq/my_ceos_rating_on_glassdoor_is_so_bad_that_all_we/",
         "Which is why a list of target companies with their recent reviews is worth a few "
         "cents a row to a recruiter, a seller or a candidate. The agent reads the cons; "
         "the page only gets it the rows."),
    ],
    "related": ("Job postings across companies", "Enrich a company from its domain",
                "A business's reviews", "Hiring, headcount and news signals"),
}


USE_CASE_PAGES["keywords-a-domain-bids-on"] = {
    "label": "Keywords a domain bids on",
    "sentence": "Competitor PPC keywords: the Google Ads keywords a domain bids on, with CPC, from SpyFu or Semrush",
    "title": "Competitor PPC keywords API: what a domain bids on | treg.to",
    "lede": (
        "Give your agent a competitor's domain and get the Google Ads keywords it bids on "
        "back as rows: keyword, search volume, cost per click, estimated monthly spend and who "
        "else bids on it. SpyFu and Semrush answer through one treg.to key, from {cheapest} "
        "at the provider's own rate with no markup, priced per row rather than per seat. Both "
        "are estimates built from a crawl of the ads they saw, not Google's own numbers, and "
        "the page says so before the comparison does."),
    "prompt": "Using treg, get the top 200 Google Ads keywords that competitor.com bids on in "
              "the US, show me the price first, then drop their brand terms and give me the "
              "twenty highest-volume keywords we are not bidding on, with CPC.",
    "prompt_why": [
        ("Give a domain, and a country", "The call takes the advertiser's domain and a country code. A brand that advertises in five markets has five different keyword lists."),
        ("Cap the rows", "SpyFu's page size defaults to 5 and goes to 10,000, and every row is billed. Two hundred rows is a strategy; ten thousand is a bill."),
        ("Strip their brand", "Every advertiser bids on its own name. Ask the agent to drop those rows before it ranks the rest, or the top of the list is noise."),
        ("Ask for the price first", "treg.to returns the rate before the call, so the agent can say what a full pull across ten competitors will spend."),
    ],
    "result_noun": "keyword",
    "result_image": None,
    "q_cheapest": "Which competitor PPC keywords API is cheapest per row?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do SpyFu and Semrush compare?",
    "what_is_heading": "How do you find the keywords a competitor bids on?",
    "what_is": (
        "Google will not tell you. Auction Insights shows which advertisers overlapped with "
        "your campaign, not which keywords they bought, and Keyword Planner's competition "
        "index counts your own ads. The third-party answer is a crawl: SpyFu and Semrush "
        "sample Google results, record which domains' ads appear against which queries, and "
        "estimate volume, CPC and spend from there. That is what a competitor PPC keywords "
        "API returns, one domain at a time, and why two tools will not agree with each other "
        "or with the advertiser's own account."),
    "notes": [
        "SpyFu bills per row and defaults to five. At $2.00 per 1,000 rows, a first look at a "
        "domain is a fraction of a cent and a ten-thousand-row dump is $20, so the page size "
        "is the cost dial. Rows carry the keyword, search volume, ranking difficulty, monthly "
        "clicks, broad and exact CPC, estimated monthly cost and the count of competing "
        "advertisers, and the endpoint has answered every call treg.to has measured so far.",
        "Semrush prices in API units bought up front, 20 per line on the current view and "
        "100 per line for a past date, and publishes no dollar rate for a unit, so the page "
        "prints no price for it. Its rows add the position and the live ad copy, which SpyFu "
        "does not carry. Send a display limit: the default is 10,000 lines, and an agent that "
        "loops without one spends real units. It has not been called live through treg.to "
        "yet.",
        "Both are crawl estimates, and the research is full of the consequences: a local "
        "advertiser missing entirely, a tool showing one bidder on a keyword that had "
        "several, two tools disagreeing on a domain's traffic by a factor of four. No "
        "comparison table settles which is right for your niche. The cheap test is to pull "
        "the same domain from both and treat the overlap as the confident set.",
    ],
    "faq": [
        ("Is this Google's own data?",
         "No. Google publishes no per-keyword view of another advertiser's account. SpyFu and "
         "Semrush estimate it from the ads their crawls observed, which is why the monthly cost "
         "column is an estimate and a competitor's max bid is not on the page at all."),
        ("Do I need a SpyFu or Semrush subscription?",
         "Not for SpyFu: treg.to serves it on its own key, metered per row from your team's "
         "balance at SpyFu's rate with $0.000 added. Semrush's API units come with a Semrush "
         "plan, so that row runs on your own Semrush key, never metered by treg.to."),
        ("Can I see a competitor's budget or impression share?",
         "No. The rows carry an estimated monthly spend per keyword, built from volume, CPC and "
         "observed position. Impression share, bids and search terms Google does not disclose "
         "are not in any third-party tool, and this page will not imply they are."),
        ("Which provider should my agent use?",
         "SpyFu for a priced, verified per-row pull; Semrush when you hold a plan and want the "
         "ad copy. treg.to shows both side by side with the rate and the measured success; it "
         "compares, it does not route or fail over for you."),
    ],
    "voices_intro": (
        "PPC tool threads are heavily seeded: of the ~95 distinct Reddit and X posts read in "
        "August 2026, one vendor's cluster ran to seven posts across three subreddits and two "
        "listicles were posted twice each. These five are advertisers and SEOs asking on "
        "their own behalf."),
    "voices": [
        ("Google's own report does not answer the question",
         "Auction Insights only provides a campaign summary, which lacks detail.",
         "r/PPC", "https://www.reddit.com/r/PPC/comments/1gdyefi/see_if_competitor_is_bidding_on_a_specific/",
         "It names the overlapping advertisers and stops. The per-domain rows here are the "
         "detail it lacks: which keywords, at what estimated CPC, against how many other "
         "bidders, one call per competitor."),
        ("The estimate missed the other bidders",
         "I tried Spyfu, and it was a little inaccurate. It showed me that only my brand was bidding on a specific keyword",
         "r/PPC", "https://www.reddit.com/r/PPC/comments/1gdyefi/see_if_competitor_is_bidding_on_a_specific/",
         "A crawl only records the ads it saw, and small or local advertisers fall through. "
         "No table fixes that. Pull the same keyword from both providers and treat a bidder "
         "both report as real; treat one nobody reports as unknown, not absent."),
        ("Do you have to buy both tools?",
         "Given Spyfu is only $9/month, do you think there is a case to be made to just purchase both?",
         "r/PPC, 35 points", "https://www.reddit.com/r/PPC/comments/wmvyd4/spyfu_vs_semrush_for_ppc/",
         "Through treg.to nobody buys either seat. SpyFu is metered per row on treg.to's key; "
         "Semrush runs on a plan you already hold. Calling both for one domain costs rows, "
         "not subscriptions."),
        ("The two tools do not agree with each other",
         "one site I put into SEMrush shows 12.6k in traffic while Spyfu shows 3.2k",
         "r/bigseo, 8 points", "https://www.reddit.com/r/bigseo/comments/4b4gwi/could_someone_explain_to_me_semrush_numbers_vs/",
         "Different crawls, different models, different numbers, and neither is Google's. "
         "The page shows the two side by side and leaves the choice to you; it does not "
         "average them or pick a winner."),
        ("The subscriptions are priced for agencies, not for one question",
         "I wanted to do some keyword research yesterday and was surprised by how expensive Ahrefs / Semrush were.",
         "r/TechSEO, 40 points", "https://www.reddit.com/r/TechSEO/comments/1r7qifp/open_source_seo_tool_that_uses_your_own/",
         "One competitor's keyword list is a per-row call, a fraction of a cent for the first "
         "page, with the rate printed before the agent spends it. The seat-priced tools "
         "stay on their own sites, linked, not restated."),
    ],
    "related": ("Ads a competitor is running now", "Keywords a domain ranks for",
                "Your own campaign performance", "Keyword volume, CPC and competition"),
}


USE_CASE_PAGES["backlink-profile-of-a-domain"] = {
    "label": "Backlink profile of a domain",
    "sentence": "Backlink API: the backlink profile of a domain from Moz, DataForSEO, Serpstat, SE Ranking, Majestic or Semrush, per call",
    "title": "Backlink API: backlink profile of a domain, {n} providers | treg.to",
    "lede": (
        "Give your agent a domain and get its backlink profile back as one row: total "
        "backlinks, referring domains, follow and nofollow split, the vendor's authority score "
        "and, from some providers, the spam and anchor breakdowns. {n} providers answer through "
        "one treg.to key, from {cheapest}, each at its own rate with no markup and none of them "
        "behind a monthly plan or a credit reset. Ahrefs is not among them. Every index is that "
        "vendor's own crawl, so the counts differ by design, and the page shows them side by "
        "side rather than picking one."),
    "prompt": "Using treg, get the backlink summary for our domain and our three main "
              "competitors from the cheapest verified provider, show me the price first, then "
              "put referring domains, follow share and the authority score in one table and "
              "tell me where the gap is widest.",
    "prompt_why": [
        ("Give bare domains", "Most rows want the domain without scheme or www, and a page URL where you mean a page. Say which, or the agent guesses."),
        ("One call per domain", "Each provider prices the summary per target, and the dearest per-call row is ten times the cheapest. Four domains is four calls."),
        ("Name the score you mean", "Moz DA, Majestic Trust Flow, Semrush Authority Score: each belongs to its vendor and compares only to itself. Ask for one and stick to it."),
        ("Ask for the price first", "treg.to returns the rate before the call, so the agent can say what a thousand expired domains will spend before it starts."),
    ],
    "result_noun": "domain",
    "result_image": None,
    "q_cheapest": "Which backlink API is cheapest per call?",
    "q_reliable": "Which backlink API is the most reliable?",
    "q_compare": "How do the backlink providers compare?",
    "what_is_heading": "What is a backlink API?",
    "what_is": (
        "A backlink API returns what a link index knows about a domain or a page as data: how "
        "many pages link to it, from how many distinct domains, how many of those links pass "
        "authority, and a score the vendor computes from all of it. The summary endpoint on "
        "this page is the one-row version, the profile rather than the list of links; the "
        "list is a different job. Each provider runs its own crawler and its own index, so "
        "DataForSEO, Moz, Serpstat, SE Ranking, Majestic and Semrush will each give a "
        "different referring-domain count for the same domain, and none of them is the "
        "count. Ahrefs, the index most of the forum pays for, is not in the catalog."),
    "notes": [
        "Per-call is the point. The forum's problem is not the data, it is the plan: an API "
        "tier priced for enterprises, a credit allowance that resets in two weeks, a seat that "
        "costs hundreds a month for one column. Here Serpstat's summary is $0.0025 a call, "
        "Moz's two quota rows come to $0.0133, SE Ranking's 100 credits to $0.0179 per target "
        "and DataForSEO's request to $0.024 plus a fraction of a cent per returned row, "
        "metered from a prepaid balance at the provider's rate with $0.000 added. A new "
        "team's free dollar is a few hundred Serpstat summaries.",
        "Two rows are the odd ones. Majestic charges one index item unit per target, and it is "
        "the same command as its URL metrics call, so ask for one and read both sets of "
        "columns rather than paying twice. Semrush charges 40 API units flat for the overview "
        "and publishes no dollar rate for a unit, so the page prints no price and it runs on "
        "your own Semrush plan. Neither has been called live through treg.to yet; the other "
        "four have, and the measured success rates on this page come from that traffic.",
        "The columns are not the same shape. Moz's call is its URL metrics with distributions "
        "on, which adds histograms by domain authority, spam score and root domains; SE "
        "Ranking's summary is ten times the price of its metrics call and adds the full "
        "breakdown, so use metrics when you only need totals; DataForSEO returns the counts, "
        "rank and spam score with breakdown arrays you can cap. Read the docs linked on each "
        "row before the agent loops.",
    ],
    "faq": [
        ("Is there an Ahrefs API here?",
         "No. Ahrefs is not in the catalog, and this page does not resell or proxy it. The "
         "six indexes here are the alternatives, each priced per call at its own rate, and "
         "the honest note is that their counts and scores are theirs, not Ahrefs'."),
        ("Why do the backlink counts differ between providers?",
         "Because each provider crawls the web itself and keeps its own index. A link one "
         "crawler found last week is one another has not reached. There is no correct count; "
         "pick one index and compare domains within it, or pull two and treat the overlap as "
         "the confident set."),
        ("Is Domain Authority in the response?",
         "Moz's DA is in Moz's row, and only there; Majestic returns Trust Flow and Citation "
         "Flow, Semrush its Authority Score, DataForSEO and SE Ranking their own rank. Each "
         "score is comparable only to itself, and no provider's number should be read as "
         "another's."),
        ("Which provider should my agent use?",
         "The one whose columns match the question and whose rate fits the volume. treg.to "
         "shows all six side by side with the rate and the measured success; it compares, it "
         "does not route or fail over for you."),
    ],
    "voices_intro": (
        "Backlink threads are among the most seeded in SEO: of the ~163 Reddit and X posts "
        "read in August 2026, one vendor's ring ran to eleven posts across five subreddits "
        "with the same template, and link sellers filled most of the rest. These five are "
        "people paying for the data themselves."),
    "voices": [
        ("The API plan is priced for a different kind of company",
         "Don't really want to drop $14K to have access to the ahrefs API",
         "r/bigseo", "https://www.reddit.com/r/bigseo/comments/1fyki5o/if_i_want_to_identify_ranking_keywords_for_a/",
         "That is a plan, and here there is none. A summary is a call, priced per call at the "
         "provider's rate, from a prepaid balance with no minimum and no tier to unlock."),
        ("Credits reset on the vendor's calendar, not yours",
         "two more weeks until my @ahrefs API credits reset. BRUTAL. this may push me to the $449/mo plan.",
         "X", "https://x.com/i/status/2091997435379773762",
         "Nothing here resets. The balance is money, it is spent per call, and the rate is "
         "printed before the agent spends it. When it runs out you top it up; you do not "
         "wait."),
        ("Whose data do you trust for link building?",
         "Just started testing APIs for backlinks of the two.. Which ones data do you prefer when it comes to linkbuilding?",
         "r/SEO", "https://www.reddit.com/r/SEO/comments/1r5gxxd/ahrefs_vs_semrush_backlinks_data/",
         "No comparison table can answer that for your niche. The cheap experiment is on this "
         "page: pull the same domain from Serpstat, Moz and DataForSEO for a few cents and "
         "see which index has the links you know exist."),
        ("A hundred domains a day makes the seat price absurd",
         "DA checks are way too expensive at volume",
         "r/Domains, 7 points", "https://www.reddit.com/r/Domains/comments/1oof78c/whats_your_workflow_for_checking_da_on_100/",
         "At volume the per-call rate is the whole story: Moz's DA row is two quota rows a "
         "call, and the cheaper summaries here carry their own vendor's score. An agent "
         "looping a list of expired domains is the fit; the price of the loop is the rate "
         "times the list."),
        ("If DR is gameable, which score is not?",
         "If Ahrefs DR should be ignored, then which “domain rating” you should care about?",
         "X", "https://x.com/i/status/2092739835694137635",
         "None of them is more than its vendor's model, and this page will not crown one. "
         "What it can do is put four vendors' scores for the same domain in one table, with "
         "the referring-domain counts beside them, so the agent shows its working."),
    ],
    "related": ("Keywords a domain ranks for", "List backlinks and find link gaps",
                "Google results for a keyword", "On-page audit of a URL"),
}


USE_CASE_PAGES["search-posts-by-keyword"] = {
    "label": "Search posts by keyword",
    "sentence": "Reddit search API and X search API: posts by keyword on Reddit, X, LinkedIn and TikTok, per call",
    "title": "Reddit and X search API: posts by keyword, per call | treg.to",
    "lede": (
        "Give your agent a keyword and get the posts back as rows: title, text, author, "
        "score, date and the link, from Reddit, X, LinkedIn or TikTok, each through the same "
        "treg.to key, from {cheapest} at the provider's own rate with no markup. No Reddit "
        "developer app to apply for, no X Basic tier to subscribe to, and no account of yours "
        "on the line. The networks are not alternatives to each other; the platform is the "
        "choice, and the rows are relayed as the provider returns them."),
    "prompt": "Using treg, search Reddit and X for posts mentioning our product name from the "
              "last week, show me the price per platform first, then group them by theme, "
              "flag anything that reads as a complaint, and give me the link for each.",
    "prompt_why": [
        ("Name the platforms", "Each network is its own shelf with its own providers. Reddit and X is two calls; all four is four."),
        ("Put a window on it", "X's official recent search covers the last seven days and the archive call the rest. Say which week you mean."),
        ("Ask for the price per platform", "The rates differ by an order of magnitude between rows. treg.to prints each before the call, so the agent can say what a daily watch will spend."),
        ("Keep the link", "The rows carry the permalink. A complaint is only useful if a human can open it."),
    ],
    "result_noun": "post",
    "result_image": None,
    "q_cheapest": "Which search API is cheapest, per platform?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the providers compare, per platform?",
    "what_is_heading": "What is a Reddit search API, and what happened to the official one?",
    "what_is": (
        "A Reddit search API returns the posts matching a keyword as data, across every "
        "subreddit, with the score, the comment count and the permalink. Reddit's own API "
        "still exists, but self-service app keys stopped, anonymous JSON calls now answer with "
        "a 403, and access runs through an approval queue, which is why the forum's Reddit "
        "MCP servers keep breaking and rebuilding on RSS. X's official search API is open but "
        "priced per post returned and capped per month. The providers on this page read "
        "both networks for you, plus LinkedIn's public posts as Google indexes them and "
        "TikTok's keyword search, and return rows; the official X call is here too, on your "
        "own X developer account."),
    "notes": [
        "Reddit is three third-party providers, none of them Reddit. ScrapeCreators bills one "
        "credit a call, TikHub a tenth of a cent per successful call, JustOneAPI per "
        "success in yuan; all run on treg.to's own key with no developer app. What they "
        "search is Reddit's own search, so relevance is Reddit's, and the rows are posts: "
        "comment trees, votes and anything that writes to Reddit still need an approved app "
        "of your own.",
        "X is the one platform with an official row. X's recent search runs on your own X "
        "developer account at X's own per-post rate, never metered by treg.to; the archive "
        "call covers history at the same rate. The two scraper rows are cheaper and, on the "
        "traffic treg.to has measured, answer more often, and they use no account of yours. "
        "They are also scraping, and the provider carries that risk, not the page; the "
        "reliability section shows what the live traffic looks like rather than promising.",
        "LinkedIn and TikTok are narrower shelves. LinkedIn post search is one provider "
        "reading what Google has indexed of public posts, so a post the index has not "
        "reached is not there. TikTok keyword search returns videos, with three providers. "
        "There is no Facebook or Instagram keyword search in the catalog, and monitoring is "
        "the agent's loop: no alerts, no schedule and no sentiment score come with the rows.",
    ],
    "faq": [
        ("Do I need a Reddit API key?",
         "No. The three Reddit rows are third-party providers on treg.to's own key, billed per "
         "call from your team's balance at the provider's rate with $0.000 added. If you hold "
         "an approved Reddit app, that is for posting and comment trees; searching posts "
         "does not need it here."),
        ("How much does the X search cost?",
         "Two ways. The official recent search bills at X's own per-post rate on your own X "
         "developer account, and treg.to meters none of it. The scraper rows bill per "
         "successful call on treg.to's key, at a rate the page prints beside each. Neither "
         "needs an X Basic subscription."),
        ("Can I monitor a keyword continuously?",
         "The agent can, by calling on a schedule you give it. treg.to has no alerting, no "
         "scheduler and no sentiment layer; it returns the posts and the price. A daily watch "
         "on two platforms is two calls a day."),
        ("Which provider should my agent use?",
         "The platform decides the shelf. Within it, treg.to shows each row's rate and "
         "measured success side by side, and the agent picks, or you tell it; treg.to does "
         "not route or fail over between them."),
    ],
    "voices_intro": (
        "Keyword-monitoring threads are a vendor parade: of the ~165 Reddit and X posts read "
        "in August 2026, one launch campaign ran to six near-identical tweets and two "
        "reposts into vendor-run subreddits, and a dozen monitoring tools pitched themselves "
        "to the same questions. These five are people hitting the wall themselves."),
    "voices": [
        ("Reddit closed the side door",
         "Reddit now blocks anonymous access to its JSON API at the network level.",
         "r/ClaudeAI, 20 points", "https://www.reddit.com/r/ClaudeAI/comments/1tsis6e/i_built_a_tiny_mcp_server_to_use_reddit_from/",
         "That poster's Reddit MCP stopped returning rows and was rebuilt on RSS. The Reddit "
         "rows here are providers with their own access, priced per call, and the agent "
         "calls them with the same setup line it uses for everything else."),
        ("A developer app just to search is the wrong shape",
         "They require setting up Reddit developer apps and OAuth tokens just to do basic searches.",
         "r/mcp, 57 points", "https://www.reddit.com/r/mcp/comments/1vtig8g/redditmcpai_an_mcp_server_for_searching_reddit/",
         "Agreed, and none of the Reddit rows here asks for one. The honest limit is that "
         "they return posts through Reddit's own search; the comment tree and the vote are "
         "still the official API's."),
        ("The official X rate adds up fast",
         "25 cents for 2 requests? This is just for a basic post search.",
         "X", "https://x.com/i/status/2074000371908043033",
         "That is X's per-post rate, and the official row here carries exactly it, on your own "
         "account. The two scraper rows sit beside it at a fraction of the price, with what "
         "the live traffic says about each, so the choice is yours with the numbers in front "
         "of you."),
        ("Searching from your own account can get it flagged",
         "doing any programmatic actions outside of the official API will flag an account",
         "X, 1,287 likes", "https://x.com/i/status/2030491364056830011",
         "That warning is about your account. The scraper providers here use none of yours, "
         "so the risk sits with the provider; the page does not call that safe, it calls it "
         "theirs, and shows their measured success instead."),
        ("The old objection, still the objection",
         "Why is he charging for low volume pull requests too?",
         "r/sysadmin, 5,377 points", "https://www.reddit.com/r/sysadmin/comments/12s95sl/is_elon_on_crack_im_not_paying_42k_per_month_for/",
         "Low volume is what per-call pricing is for. Ten posts a day on a scraper row is "
         "cents a month, with no tier, no minimum and the rate printed before each call."),
    ],
    "related": ("Mine the comments", "Find creators by keyword",
                "A competitor's recent posts", "Posts under a hashtag"),
}


USE_CASE_PAGES["app-store-search"] = {
    "label": "App store search",
    "sentence": "App store API: search the Apple App Store and Google Play by keyword, per call",
    "title": "App store API: search the App Store and Google Play | treg.to",
    "lede": (
        "Give your agent a keyword and get the store listing rows back: app name, developer, "
        "rating, review count, price and the store link, from the Apple App Store and from "
        "Google Play, both through one treg.to key from {cheapest} a search at the provider's "
        "own rate with $0.000 added. No Apple developer account, no Play console, no proxy "
        "pool and no scraper of your own to keep alive when a store changes its markup. The "
        "two stores are separate shelves here, not two views of one dataset, so ask for both "
        "when you want both."),
    "prompt": "Using treg, search the Apple App Store and Google Play for apps matching "
              "a habit tracker, show me the price per search first, then put the results "
              "from each store in one table with developer, rating, review count and price, "
              "and mark the apps that appear in both.",
    "prompt_why": [
        ("Name both stores", "Apple and Google Play are separate calls with separate app ids and separate rankings. One store is one search; both is two."),
        ("Say which country", "Store results are per storefront. The US list and the UK list are different lists, and the agent will pick for you if you do not."),
        ("Ask for the columns", "The rows carry the developer, the rating, the review count, the price and the link. Name the ones you want or the whole listing comes back."),
        ("Ask for the price first", "treg.to prints the rate before the call, so a hundred keywords across two stores has a number on it before anything runs."),
    ],
    "result_noun": "app",
    "result_image": None,
    "q_cheapest": "What does an app store search cost?",
    "q_reliable": "Which store search is the most reliable?",
    "q_compare": "How do the two stores compare?",
    "what_is_heading": "What is an app store API?",
    "what_is": (
        "An app store API returns what a store's search results page holds, as data: the apps "
        "matching a keyword, in the store's own order, with the developer, the rating, the "
        "review count, the price and the listing link. It is not App Store Connect and it is "
        "not the Google Play Developer API. Those are the accounts you own, and they only "
        "cover your own apps. It is also not Apple's public iTunes Search endpoint, which is "
        "free, answers a different query shape, and is not in this catalog. The two rows here "
        "read the two public stores and relay what the store returned."),
    "notes": [
        "One provider serves both stores at one rate. SerpApi runs the Apple App Store engine "
        "and the Google Play engine at the same flat price per successful search, metered "
        "from your team's prepaid balance at SerpApi's own rate with $0.000 added, and its "
        "rate card says a failed or cached search is not billed. Both rows were verified "
        "against the live API on 2026-07-28. A new team's free dollar is a few dozen searches "
        "across both stores.",
        "The two engines take different parameters, and the Play row does two jobs. Apple's "
        "engine wants a search term; Google Play's wants a query, and the same row answers "
        "the store charts as well as a keyword search depending on what you send. Read the "
        "docs linked on each row before an agent loops, because treg.to relays the store's "
        "own response verbatim and models neither store's API.",
        "What is not here is the part most people are really after. This is search, not rank "
        "tracking: nothing stores yesterday's position, and there is no keyword popularity "
        "index, no download or revenue estimate and no review feed on these two rows. Google "
        "Play also stops exposing results publicly at shallow depth, which android developers "
        "have measured for themselves, so a position deeper than the first page or two is not "
        "a number any public search can honestly return. An agent can rebuild position over "
        "time by running the same search on a schedule you give it and keeping the results.",
    ],
    "faq": [
        ("Do I need an Apple or Google developer account?",
         "No. Both rows run on treg.to's own key and bill per successful search from your "
         "team's balance at SerpApi's rate with no markup. A developer account is for "
         "publishing apps and reading your own; this reads the public store."),
        ("Is this App Store Connect or the Play Developer API?",
         "No. Those two are your own account's APIs and they only see apps you publish: your "
         "installs, your ratings, your revenue. This page is the public store search, so it "
         "sees any app but none of the private numbers behind it."),
        ("Can I track where my app ranks for a keyword over time?",
         "Your agent can, by running the search on a schedule you set and keeping the "
         "results. treg.to has no scheduler, no history and no ASO rank tracker. The honest "
         "limit is depth: the stores expose only so many results, and a rank below that is a "
         "guess wherever you read it."),
        ("Can I get reviews, downloads or keyword volume?",
         "Not from these two rows. They return the search listing and nothing more. The "
         "catalog carries app review and app data endpoints from other providers on their own "
         "shelves, and Apple's keyword popularity is a relative index behind an Apple Search "
         "Ads account, not a search volume figure anyone can resell."),
    ],
    "voices_intro": (
        "App store threads are unusually seeded: of the ~140 Reddit and X posts read in "
        "August 2026, more than half were builders announcing their own scrapers, one ran the "
        "same review-scraper copy through seven subreddits, and one post seeded zero-width "
        "spaces at every paragraph break. These five are developers hitting the wall in "
        "public."),
    "voices": [
        ("Deep store ranks are interpolated, not measured",
         "The paid SERP APIs go deeper, but tools that don't pay for them and still show you 'rank #63' are interpolating, not measuring.",
         "r/androiddev, 11 points", "https://www.reddit.com/r/androiddev/comments/1va99d9/psa_google_play_only_exposes_30_search_results/",
         "That is the honest ceiling and this page will not pretend past it. The rows return "
         "the store's own results in the store's own order, as deep as the store exposes "
         "them, and treg.to publishes no rank number of its own."),
        ("Apple's free endpoint stops answering without notice",
         "As of this morning (April 16), all my requests to the /search endpoint are returning HTTP 404 Not Found.",
         "r/iOSProgramming, 11 points", "https://www.reddit.com/r/iOSProgramming/comments/1sn3k1m/itunes_search_api_returning_404_for_search/",
         "That is Apple's public iTunes Search endpoint, which is free, undocumented in "
         "practice, and not in this catalog. The rows here are a paid provider's store "
         "engines, billed only on a successful search, with what the live traffic shows "
         "rather than an uptime promise."),
        ("Everything is API first except the store itself",
         "To have to scrape them from the app store pages when everything else is so API first seems like a miss.",
         "X, 19 likes", "https://x.com/i/status/2092565867145879853",
         "Agreed, and that is the whole reason this row exists. The agent sends a keyword and "
         "a country and gets rows; the scraping, the proxies and the markup changes are the "
         "provider's problem, at a price printed before the call."),
        ("The free endpoint carries none of the numbers you want",
         "the itunes search api does not give downloads, subscriptions, or any financial metrics, and it does not provide subscription pricing either.",
         "X, 69 likes", "https://x.com/i/status/1994112990866518506",
         "True, and neither do these rows. Listing price, rating and review count come back; "
         "installs and revenue do not exist in any public store response, and no provider on "
         "this page can conjure them."),
        ("The whole store is bigger than the context window",
         "welp, even using a 1M token context limit, the data is too large.",
         "r/shopifyDev, 77 points", "https://www.reddit.com/r/shopifyDev/comments/1o4ugbp/an_analysis_of_15003_apps_in_the_shopify_app_store/",
         "Which is the argument for asking for columns rather than everything. Say which "
         "fields a row needs in the prompt, cap the result count, and the agent reads a table "
         "instead of drowning in a store dump."),
    ],
    "related": ("Amazon search and best sellers", "TikTok Shop products and reviews",
                "Product reviews", "Google results for a keyword"),
}


USE_CASE_PAGES["transcripts-of-x-and-facebook-video-posts"] = {
    "label": "Transcripts of X and Facebook video posts",
    "sentence": "Facebook video transcript and X video transcript: the words in a video post, from its URL",
    "title": "Facebook and X video transcript by URL, per call | treg.to",
    "lede": (
        "Paste your agent the URL of a Facebook video post or an X video post and get the "
        "spoken words back as text, through one treg.to key from {cheapest} a call at the "
        "provider's own rate with $0.000 added. No download step, no ffmpeg, no Whisper run "
        "of your own and no developer account on either network. The price is per call, not "
        "per minute of video, so a fifty minute livestream and a twenty second clip cost the "
        "same. Public posts only, and a post that has been taken down is gone for everyone."),
    "prompt": "Using treg, get the transcript of this Facebook video post and this X video "
              "post, show me the price per call first, then give me the text of each with the "
              "source link above it, and tell me plainly if either one came back empty.",
    "prompt_why": [
        ("Give the post URL, not the video file", "Both rows take the URL of the post. Getting the media off the platform is the part that normally breaks, and it is the provider's job here."),
        ("Say which network", "Facebook and X are separate rows behind separate calls. One post is one call, on the network the URL belongs to."),
        ("Ask for the text, not a summary", "The rows return what was said. A summary is the agent's own work afterwards, and it is worth reading the transcript first."),
        ("Handle the empty case out loud", "A post with no speech, or one the provider cannot reach, comes back with nothing and still costs the call. Tell the agent to say so rather than invent."),
    ],
    "result_noun": "transcript",
    "result_image": None,
    "q_cheapest": "What does a transcript cost, per network?",
    "q_reliable": "Which one is the most reliable?",
    "q_compare": "How do the two networks compare?",
    "what_is_heading": "What is a Facebook video transcript API?",
    "what_is": (
        "It is a call that takes the URL of a video post and returns the words spoken in it as "
        "text. The hard part was never the speech recognition: it is getting the media out of "
        "a network that does not want to hand it over. Facebook Reels carry no automatic "
        "transcription and Facebook blocks the usual downloaders, so the do it yourself path "
        "runs download, extract audio, transcribe, and falls over at the first step. X posts "
        "have their own wall around saving the file. Both rows here take the URL and do all "
        "of it on the provider's side, so your agent gets text and never touches the video."),
    "notes": [
        "One provider covers both networks at one rate. ScrapeCreators bills a credit per "
        "call for the X row and for the Facebook row alike, metered from your team's prepaid "
        "balance at ScrapeCreators' own rate with $0.000 added. Per call means per call: an "
        "empty answer is billed the same as a full transcript, so budget the rate times the "
        "number of posts, not the number of transcripts you keep.",
        "Neither row has been called live through treg.to yet, and the catalog says so. Both "
        "carry a documented price and a documented shape from the provider's docs, neither "
        "has a verified date, and the Facebook row's stored test request does not even name a "
        "URL. Treat them as documented rather than proven, run one post before an agent runs "
        "a thousand, and read the docs linked on each row.",
        "The limits are the platforms', and they are worth saying out loud. Public posts "
        "only: a friends only, group only or age gated Facebook video is not reachable. A "
        "deleted post is gone, and no provider recovers it. YouTube transcripts are a "
        "different and much cheaper shelf with its own page, and TikTok and Instagram "
        "transcripts are not in the catalog at all. Grok will also transcribe an X video "
        "inside X if a human is doing it by hand; these rows are for the case where an agent "
        "needs a hundred of them.",
    ],
    "faq": [
        ("Do I need an X or Facebook developer account?",
         "No. Both rows run on treg.to's own key and bill per call from your team's balance at "
         "the provider's rate with no markup. Nothing uses an account of yours, so nothing "
         "puts one at risk."),
        ("Does it work on any post?",
         "Video posts, and public ones. A text post has nothing to transcribe, a private or "
         "age gated Facebook video is not reachable, and a post that has been removed is gone "
         "for the provider too. The call still costs its rate when the answer comes back "
         "empty, which is why the prompt above asks the agent to say so."),
        ("What about YouTube, TikTok or Instagram?",
         "YouTube has its own page and its own much cheaper row. TikTok and Instagram video "
         "transcripts are not in the catalog today, and this page will not pretend otherwise. "
         "Rumble is there, on its own shelf."),
        ("Is this cheaper than running Whisper myself?",
         "That depends on what your time is worth. Whisper is free and the audio is not: the "
         "download is what fails, and the published attempts at doing this with an agent end "
         "in blocked downloaders, expired tokens and a fallback to a paid speech API anyway. "
         "These rows are one call at a printed rate, with no pipeline to maintain."),
    ],
    "voices_intro": (
        "This is a thin corpus honestly reported: of the ~180 Reddit and X posts read in "
        "August 2026 only about twenty were on the job, and the loudest block of pain "
        "language in the Facebook dumps came from one bot posting the same six templates. "
        "These five are people doing it the hard way."),
    "voices": [
        ("Facebook Reels have no captions to fall back on",
         "Facebook blocks most AIs by default, and Reels don't have automatic transcription, so there's no shortcut.",
         "r/claude, 6 points", "https://www.reddit.com/r/claude/comments/1s22ccw/i_tested_claude_to_see_if_they_could_get_a/",
         "That is the honest state of Facebook video, and it is why this row exists. The "
         "shortcut is somebody else's fetch: your agent sends the post URL and reads text "
         "back, at a rate printed before the call."),
        ("The agent's problem was the fetch, never the transcription",
         "It tried again, used yt-dlp, said that it was blocked because FB blocks bots.",
         "r/claude, 6 points", "https://www.reddit.com/r/claude/comments/1s22ccw/i_tested_claude_to_see_if_they_could_get_a/",
         "In that thread the model eventually got there through a third party downloader, an "
         "expired token and a paid speech API, after four nudges. One call replaces the "
         "chain; what it cannot do is reach a post that is private or gone."),
        ("Hand typing is the fallback people actually use",
         "Please note, the transcript may not be 100% accurate as it was typed out by hand.",
         "r/Superstonk, 6,923 points", "https://www.reddit.com/r/Superstonk/comments/qmnan7/computershare_ama_part_1_video_link_with/",
         "Machine text is faster and it is not automatically better on accents or crosstalk. "
         "The useful version is both: get the transcript in a second, then spot check the "
         "lines you are going to quote against the video."),
        ("Nobody asked for a summary, they asked what was said",
         "For those who can't wade through the whole 12 minutes or so, here's a rough and ready transcript.",
         "X, 6 likes", "https://x.com/i/status/2092561667783573707",
         "Which is why the prompt on this page asks for the text first. The rows return the "
         "words; summarising, quoting and pulling a clip out of them is your agent's own work "
         "afterwards, on data it can show you."),
        ("A video post with nothing to read",
         "Finally found a video on Facebook that doesn't have captions or a transcript. WTH.",
         "X", "https://x.com/i/status/1716876952337068536",
         "That is the ordinary case on Facebook rather than the unlucky one. The row does not "
         "read captions off the post, so a video with none is still readable, as long as the "
         "post is public."),
    ],
    "related": ("Get a video's transcript", "Search posts by keyword",
                "A competitor's recent posts", "Video details, views and stats"),
}


USE_CASE_PAGES["get-a-linkedin-profile"] = {
    "label": "Get a LinkedIn profile",
    "sentence": "LinkedIn API and LinkedIn scraper: a person's profile by URL, headline, experience and education",
    "title": "LinkedIn API: fetch a profile by URL, {n} providers | treg.to",
    "lede": (
        "Give your agent a LinkedIn profile URL and get the profile back as data: name, "
        "headline, location, current role, past roles and education. {n} providers answer "
        "through one treg.to key, the cheapest of them a tenth of a cent a profile, each "
        "at its own rate with no markup and none behind a monthly seat. No session cookie of yours, no browser "
        "extension, no Sales Navigator subscription and no account of yours making the "
        "request. LinkedIn's own row is here too and it is honest about what it is: it "
        "returns the profile of the account you connected, and nobody else's."),
    "prompt": "Using treg, fetch these 40 LinkedIn profile URLs from the cheapest verified "
              "provider, show me the price per profile first, then give me name, headline, "
              "current company and years in the current role as a table, and list the URLs "
              "that came back empty.",
    "prompt_why": [
        ("Give the profile URL", "Most rows take the public profile URL or its slug. A name and a company is a different job, and a different page."),
        ("Say which fields you need", "A full profile is a large object. Naming the columns keeps the table readable and keeps the agent from spending its context on someone's volunteering history."),
        ("Ask for the price first", "The rates here differ by more than an order of magnitude per profile. treg.to prints the rate before the call, so 40 profiles has a number on it before anything runs."),
        ("Ask for the misses by name", "Every provider misses some profiles. A list of the URLs that came back empty is worth more than a table that quietly has 34 rows instead of 40."),
    ],
    "result_noun": "profile",
    "result_image": None,
    "q_cheapest": "Which LinkedIn API is cheapest per profile?",
    "q_reliable": "Which LinkedIn API is the most reliable?",
    "q_compare": "How do the LinkedIn providers compare?",
    "what_is_heading": "What is a LinkedIn API, and why is there no official one?",
    "what_is": (
        "A LinkedIn API, as people mean the phrase, returns a person's public profile as "
        "structured data: headline, location, the roles they have held with dates, and where "
        "they studied. LinkedIn's official API does not do this. It is an OAuth API for the "
        "account that authorised it, so it can tell you your own name and email and it cannot "
        "tell you a stranger's job history; that product does not exist for you. Everything "
        "else on this page is a third-party provider that reads public profiles with its own "
        "infrastructure and sells the result per profile. Proxycurl, which was the default "
        "answer here for years, shut down, which is why the question keeps being asked."),
    "notes": [
        "Five third-party rows and one official one, and the official one is a different job. "
        "LinkedIn's own OAuth row is free, runs on your team's connected account and is never "
        "metered, and it returns that account's own profile: name, email, member id. It "
        "cannot fetch a prospect. The other five fetch anyone's public profile and none of "
        "them uses an account of yours, so no cookie of yours, no extension in your browser "
        "and no view on your activity log.",
        "The units differ, so read the rate and the word after it. TikHub bills per "
        "successful call, ScrapeCreators per call whether or not the profile comes back, "
        "Bright Data per record delivered, JustOneAPI per success priced in yuan and "
        "converted, and Fiber per credit for a live fetch. The cheapest claim on this page is "
        "made inside one unit and never across them. ScrapeCreators, Bright Data, TikHub and "
        "JustOneAPI have been called live and carry a verified date in the catalog; the Fiber "
        "row and the LinkedIn OAuth row are documented from the provider's own docs and have "
        "not been verified through treg.to.",
        "What nobody can promise is coverage, and this page will not. Each provider reads "
        "LinkedIn its own way, so a profile one returns in full is one another returns "
        "thin, and the fields most often missing are exactly the ones people want: the dated "
        "experience list. The cheap experiment is the honest answer: run the same twenty URLs "
        "through three rows for a few cents and count who filled the columns you need. "
        "treg.to shows the rows side by side and the agent picks; it does not route between "
        "them and it does not fail over.",
    ],
    "faq": [
        ("Is there an official LinkedIn API for other people's profiles?",
         "No. LinkedIn's OAuth API returns the profile of the member who authorised your app, "
         "so it answers \"who am I\" and never \"who is this person\". The row on this page "
         "is that call, marked free and run on your own connection. Everything else here is a "
         "third party."),
        ("Will this get my LinkedIn account banned?",
         "None of the five third-party rows uses an account of yours: no cookie, no session, "
         "no extension, so there is nothing of yours for LinkedIn to restrict. That is not "
         "the same as zero risk in general, because the restriction stories in the forum "
         "include people who were only browsing. The risk these rows remove is the automation "
         "risk on your own login; the provider carries its own."),
        ("What replaced Proxycurl?",
         "Nothing single replaced it. The five third-party rows here are what the catalog "
         "carries for a person profile by URL, priced per profile with no plan, and the "
         "honest note is that they are not a drop-in: the field coverage and the freshness "
         "are each provider's own. Company pages, job changes and people search are separate "
         "jobs on separate rows."),
        ("Which provider should my agent use?",
         "The one whose fields survive your list at a rate that fits the volume. treg.to puts "
         "the rate, the billing unit and the measured success rate beside each other and the "
         "agent chooses, or you tell it which to use. treg.to compares; it does not pick for "
         "you."),
    ],
    "voices_intro": (
        "LinkedIn data is one of the most seeded topics on Reddit: of the ~170 posts read in "
        "August 2026, one account posted fourteen actor listings in a subreddit it appears to "
        "run, one promo shipped with its template placeholder still in the body, and one "
        "seeded thread carried a zero-width space. These five are practitioners, and the last "
        "one is the reason the page will not claim zero risk."),
    "voices": [
        ("An extension got an SDR restricted in days",
         "My SDR got his account limited after using PhantomBuster for like 3 days.",
         "r/b2bmarketing, 16 points", "https://www.reddit.com/r/b2bmarketing/comments/1sv9jwv/linkedin_lead_scraping_tools_whats_safe_that_wont/",
         "That risk is the one thing these rows genuinely remove. A call here runs on the "
         "provider's infrastructure, not on your seat, so there is no cookie of yours and no "
         "activity on your account to flag."),
        ("Being banned is not survivable for some jobs",
         "I review hundreds of Linkedin profiles a week for a living and it is not viable to be banned from the platform.",
         "r/apify", "https://www.reddit.com/r/apify/comments/1u7pogx/how_concerned_about_a_linkedin_ban_should_i_be/",
         "Which is the argument for keeping the fetch off your login entirely. Read the "
         "profiles through a provider, keep your own account for the human work, and pay per "
         "profile rather than per seat."),
        ("The enrichment tools are priced for a different buyer",
         "Tools like Clay's 'Enrich Profile' feature or APIs like Brightdata feel quite pricey. Any suggestions or alternative approaches?",
         "r/revops, 5 points", "https://www.reddit.com/r/revops/comments/1i4xwzh/cheapest_way_to_enrich_full_linkedin_profile/",
         "Bright Data is one of the rows here, at its own per-record rate with nothing added, "
         "and it is not the cheapest one. The point is the shape: a profile is a call, there "
         "is no plan to be on, and the rate is printed before the agent spends it."),
        ("The default answer shut down",
         "Looking for a good alternative to Proxycurl (they shut down) for B2B sales automation",
         "r/n8n, 6 points", "https://www.reddit.com/r/n8n/comments/1myqu0v/looking_for_a_good_alternative_to_proxycurl_they/",
         "This page is five of the alternatives with their prices next to each other. It is "
         "worth saying that the forum has not moved on to one successor, so the checklist "
         "that thread asks for, profile plus company plus job changes, is three jobs here and "
         "three rows."),
        ("The restriction can arrive with no automation at all",
         "the funny thing is i wasnt using anything to automate linkedin, just clicking around and opening new tabs",
         "X, 199 likes", "https://x.com/i/status/2059841283674533977",
         "Which is why this page says the rows remove the automation risk on your own login "
         "and stops there. Nothing here makes LinkedIn safer for the account you browse with; "
         "it just stops needing it."),
    ],
    "related": ("Find people by role, company or location",
                "Enrich a person from an email or LinkedIn URL",
                "Find professional emails", "Find phone numbers"),
}


USE_CASE_PAGES["find-phone-numbers"] = {
    "label": "Find phone numbers",
    "sentence": "Phone number lookup API: a prospect's mobile from a LinkedIn URL or work email",
    "title": "Phone number lookup API: a mobile from a LinkedIn URL | treg.to",
    "lede": (
        "Give your agent a LinkedIn profile URL, a work email or a name and company, and get "
        "a direct mobile number back where one exists. {n} providers answer through one "
        "treg.to key, from {cheapest}, each at its own rate with no markup and none of them "
        "behind a seat or an annual contract. This runs one way only: person to number. It is "
        "not a reverse lookup, it will not tell you who owns a number you already have, and "
        "no provider here finds a number for everyone you ask about."),
    "prompt": "Using treg, find mobile numbers for these 25 LinkedIn URLs, show me the price "
              "per found number for each provider first, use the cheapest one, then give me a "
              "table of who was found and who was not, and do not guess a number for anyone "
              "who came back empty.",
    "prompt_why": [
        ("Give the identifier, not the name alone", "Most rows resolve from a LinkedIn URL or a work email. A bare name and company is the weakest input and the one that misses most."),
        ("Ask for the misses explicitly", "A list of 25 that returns 11 numbers is the normal outcome. The 14 that missed are the information; a table that silently has 11 rows is not."),
        ("Ask for the price per found number", "The rates here span more than five times per number. treg.to prints each before the call, so 25 people has a worst case before anything runs."),
        ("Never let the agent infer a number", "A model asked for a phone number will happily produce a plausible one. Say out loud that an empty answer stays empty."),
    ],
    "result_noun": "number",
    "result_image": None,
    "q_cheapest": "Which phone finder is cheapest per number found?",
    "q_reliable": "Which phone finder is the most reliable?",
    "q_compare": "How do the phone finders compare?",
    "what_is_heading": "What is a phone number lookup API?",
    "what_is": (
        "In business-to-business terms it is a call that takes a person, usually as a LinkedIn "
        "profile URL or a work email, and returns a direct mobile number for them if the "
        "provider has one. The direction matters: this is person to number. The consumer "
        "product that goes the other way, number to person, is a different thing entirely and "
        "is not on this page. Every provider here is assembling the same kind of data from "
        "the same kinds of sources, which is why the useful question is not who has the best "
        "database in the abstract but who fills your list, in your countries, this month."),
    "notes": [
        "The prices are per number found, and they span the page. Tomba is the cheapest per "
        "found number, Aviato next, then LeadMagic, Findymail and LeadsForge, which is more "
        "than five times the cheapest; LeadsForge also has a bulk row at the same rate per "
        "row. Ocean.io meters in credits and publishes no dollar rate, so the page prints "
        "none. All of it is the provider's own rate with $0.000 added, from a prepaid balance "
        "with no minimum.",
        "Read the miss rule carefully, because it is where the money goes. The rate cards say "
        "a miss is free, and one row on this page proves it through treg.to: LeadMagic "
        "reports its own charge back on every call, so a miss settles at zero. The others do "
        "not report a charge, so the call settles at the catalog rate whether or not a number "
        "came back. Until that changes, budget the printed rate times every attempt, not "
        "times the numbers you keep.",
        "Nobody publishes an honest hit rate, including this page. What the practitioners in "
        "the forum say instead is that coverage swings by geography and vertical, that a "
        "provider strong on US technology can be thin on European professional services, and "
        "that a number being current is not the same as somebody answering it. The cheap "
        "experiment is the only real answer: run the same twenty rows through three providers "
        "for a couple of dollars and count. treg.to shows the rates side by side; the agent "
        "picks, and treg.to never routes or fails over between them.",
    ],
    "faq": [
        ("Can I find out who owns a phone number I already have?",
         "No. Every row here runs person to number: you give a profile URL, an email or a "
         "name and company, and get a mobile back. Reverse lookup is a consumer product and a "
         "different job, and the catalog does not carry it."),
        ("What hit rate should I expect?",
         "Nobody on this page will give you a number, and any published match rate you find "
         "elsewhere was written by whoever is selling. Expect a minority of a cold list to "
         "resolve, expect it to be worse outside the US, and measure it on your own rows: "
         "twenty people through three providers costs a couple of dollars."),
        ("Do I pay for a miss?",
         "By the providers' rate cards, no. Through treg.to today, only LeadMagic settles a "
         "miss at zero, because it is the one row that reports its own charge back on the "
         "call. On the others the call settles at the catalog rate, so budget per attempt."),
        ("Is a found number safe to call?",
         "That is your call to make, not the data's. These rows return a number and nothing "
         "about consent: do not call screening, no suppression list and no country by country "
         "guidance. Whatever your obligations are under the rules that apply to you, they "
         "stay yours."),
    ],
    "voices_intro": (
        "This is the most heavily seeded category behind any of these pages: of the ~380 Reddit and X "
        "posts read in August 2026 roughly seven in ten were vendor marketing, including a "
        "ring of six fabricated review subreddits and one post that hid a combining grapheme "
        "joiner inside half its words. That post was the most quotable in the set, and it is "
        "not quoted here. These five are practitioners."),
    "voices": [
        ("Better data does not make somebody answer",
         "Post Covid, office lines don't even seem to exist now and connect rates on cell phones are abysmal.",
         "r/sales, 102 points", "https://www.reddit.com/r/sales/comments/1jfuzv7/cold_calling_is_it_actually_harder_now_or_am_i/",
         "So this page will not claim to fix connect rates. What a row here can raise is the "
         "share of your dials that reach a current mobile at all. Whether anyone picks up is "
         "carrier screening and human behaviour, and no provider sells that."),
        ("Paying for the data is not the same as the data being right",
         "The mistake is assuming that paying for contact data means every record will be correct.",
         "r/RecruitmentAgencies", "https://www.reddit.com/r/RecruitmentAgencies/comments/1v1gi34/best_tools_to_grow_a_recruiting_desk/",
         "Agreed, and the shape of this page follows from it. Per call pricing means testing "
         "three providers on your own rows costs less than one seat's first day, which beats "
         "trusting anyone's accuracy claim, this page's included."),
        ("It is hard to tell what is good from what is hype",
         "I've looked at tools like Apollo, ZoomInfo, Lusha, Seamless, Cognism, etc., but it's hard to tell what's good vs hype.",
         "r/salestechniques, 8 points", "https://www.reddit.com/r/salestechniques/comments/1qi5fx8/b2b_prospecting_tools_that_actually_work_in_europe/",
         "It is hard because most of what you find when you search is seeded, which the "
         "research behind this page ran into head first. The answer this page offers is not "
         "another review: it is the raw result on your own list, for cents, from several "
         "providers at once."),
        ("Coverage is a function of where your buyers are",
         "We tested a few vendors and the data quality changes a lot depending on region. Some are strong in US but weak in Europe.",
         "r/AppBusiness", "https://www.reddit.com/r/AppBusiness/comments/1s28iwb/what_are_the_best_enrichment_tools_for_mobile/",
         "No comparison table can tell you which of these is strong in your market, and this "
         "one does not try. It puts the rates and the billing units side by side so the "
         "experiment that would tell you is cheap enough to actually run."),
        ("The ask is plain and nobody answers it plainly",
         "Basically looking for a reliable provider for cold calling that gives accurate direct dials without breaking the bank.",
         "r/sales_intelligence", "https://www.reddit.com/r/sales_intelligence/comments/1vh77bm/need_suggestions_on_providers_for_cold_calling/",
         "The honest version: here are six, here is what each charges per number found, here "
         "is which one gives you a free miss today, and here is how to test them on twenty of "
         "your own rows before you commit to any of them."),
    ],
    "related": ("Find professional emails", "Check a phone number is real",
                "Enrich a person from an email or LinkedIn URL", "Get a LinkedIn profile"),
}


USE_CASE_PAGES["keyword-volume-cpc-and-competition"] = {
    "label": "Keyword volume, CPC and competition",
    "sentence": "Keyword research API: Google search volume, CPC and competition for a list of keywords",
    "title": "Keyword research API: Google search volume and CPC | treg.to",
    "lede": (
        "Hand your agent a list of keywords and get a figure back for each one: average "
        "monthly searches, the competition level and the top of page bid range. {n} providers "
        "answer through one treg.to key: a twentieth of a cent per keyword on the cheapest "
        "row, a flat rate per request on another, each at the provider's own rate with no markup. Google's own row is free on your connected "
        "Google Ads account and never metered, and it answers with a number per keyword rather "
        "than the bucket the Keyword Planner screen shows you. It is the row the research "
        "behind these pages runs on."),
    "prompt": "Using treg, get the average monthly searches, competition and top of page bid "
              "for these 200 keywords in the US in English, show me the price per provider "
              "first, batch them into as few calls as the provider allows, and sort the table "
              "by volume with the ones that returned no data listed separately.",
    "prompt_why": [
        ("Batch, do not loop", "One row charges a flat rate per request for up to a thousand keywords. Sending them one at a time on that row multiplies the bill by a thousand for the same answer."),
        ("Name the country and language", "Volume is per market. A keyword measured across everywhere is a number that describes nowhere, and every row here wants the market stated."),
        ("Ask for the price per provider first", "The rows meter in three different ways: per request, per keyword returned, and free on your own account. treg.to prints each before the call."),
        ("Keep the empty rows visible", "Google returns nothing for some ordinary words, and a provider that drops them quietly leaves you reading a shorter list than you sent."),
    ],
    "result_noun": "keyword",
    "result_image": None,
    "q_cheapest": "Which keyword research API is cheapest?",
    "q_reliable": "Which keyword data provider is the most reliable?",
    "q_compare": "How do the keyword data providers compare?",
    "what_is_heading": "What is a keyword research API?",
    "what_is": (
        "It is a call that takes a list of keywords and returns what an advertiser's data set "
        "knows about each one: roughly how many times a month people search it, how "
        "contested it is, and what advertisers are paying at the top of the page. The reason "
        "people want it as an API rather than a screen is volume and shape. The Keyword "
        "Planner screen shows bucketed ranges to accounts that are not spending, its CSV "
        "export collapses everything to a handful of round numbers, and neither is something "
        "you can join onto twenty thousand rows. Every provider here answers per keyword, in "
        "a response an agent can put in a table."),
    "notes": [
        "The Google Ads row is free, and it is the real thing. It runs on your team's "
        "connected Google Ads account, is never metered by treg.to, counts only against the "
        "developer token's own daily operation limit, and it returns a figure per keyword "
        "rather than the range the Keyword Planner screen shows. The figure is Google's own "
        "rounded number and not a raw count. The cost of this row is not money, it is access: "
        "Google's flow pushes you towards creating a campaign on the way in, and the "
        "developer token is its own approval.",
        "The paid rows meter in two different shapes, and the shape decides the bill. "
        "DataForSEO charges a flat rate per request that covers one keyword or a thousand "
        "alike, so batching is the whole game; the catalog uses the rate card's price rather "
        "than the lower one on the public pricing page, because the rate card is what the "
        "account is actually charged. Serpstat and SE Ranking charge per keyword returned, "
        "and Serpstat bills its one credit minimum even when a keyword comes back empty. "
        "Semrush charges API units with no published dollar rate, so the page prints no price "
        "for it and it runs on your own Semrush plan.",
        "The numbers will not agree, and no row here is ground truth. Each provider models "
        "Google's data its own way, so the same keyword comes back with different volumes on "
        "different rows, and Search Console will show you clicks on queries that none of them "
        "list at all. The bid range is an estimate of an auction rather than the auction: "
        "practitioners regularly report paying many times the figure, or a fraction of it. "
        "Use these fields to rank keywords against each other and take your real cost from "
        "your own campaign data. treg.to puts the rows side by side; the agent picks, and "
        "treg.to never routes or fails over between them.",
    ],
    "faq": [
        ("Is this the same data as Google Keyword Planner?",
         "The Google Ads row is, on your own connected account, and it answers with a figure "
         "per keyword instead of the range the screen shows. The other rows are each "
         "provider's own model of the same underlying signal, which is why their numbers "
         "differ from Google's and from each other."),
        ("Do I need a Google Ads account?",
         "Only for the free row, which runs on your own connection and is never metered. The "
         "paid rows need nothing of yours: they run on treg.to's key and bill from your "
         "team's prepaid balance at the provider's rate with $0.000 added, so a list of "
         "keywords does not require a developer token or an ad campaign."),
        ("Why do the volume numbers differ between providers?",
         "Because none of them is counting searches. Each one models Google's published "
         "signal in its own way, and this page will not crown one, because there is no "
         "ground truth to crown it against. Pull the same list from two rows for a few cents "
         "and treat the disagreement as the error bar."),
        ("How much does ten thousand keywords cost?",
         "It depends entirely on which row and how you batch. A flat per request row covering "
         "up to a thousand keywords is ten calls; a per keyword row is ten thousand "
         "chargeable results however you send them; the Google Ads row is free and bounded by "
         "a daily operation limit instead. treg.to prints each rate before the call so the "
         "agent can say the number first."),
    ],
    "voices_intro": (
        "SEO tooling threads are seeded on an industrial scale: of the ~180 Reddit and X "
        "posts read in August 2026, three near identical six week bake off posts landed on "
        "the same recommendation in the same house style, two subreddits in the results were "
        "review farms, and one post ran word for word from two different accounts. These "
        "five are people with a spreadsheet and a problem."),
    "voices": [
        ("The screen answers in buckets",
         "It keeps showing me ranges such as 100-1k. This is very hard for me to guess for any good decision I wish to make.",
         "r/googleads", "https://www.reddit.com/r/googleads/comments/1dodfc3/how_do_i_see_the_exact_monthly_search_volume/",
         "The free row on this page is the same Keyword Planner data through the API, and it "
         "answers with a figure per keyword rather than a range. That figure is Google's own "
         "rounded number, so treat it as a rank rather than a count."),
        ("The export rounds everything to nothing",
         "when i export in CSV it's either 50, 500, 5000 or 50000 for all results",
         "r/googleads", "https://www.reddit.com/r/googleads/comments/1dz73xx/important_features_missing_from_keyword_planner/",
         "That is the data quality floor the API is worth having for. Every row here returns "
         "a per keyword field an agent can sort, join and put in a table, which the export "
         "cannot give you at any spend level."),
        ("The bid estimate and the invoice are different numbers",
         "Keyword planner says Low competition keyword top page bid high range is 0.03 cent. Why am I paying 1.28?",
         "r/PPC, 8 points", "https://www.reddit.com/r/PPC/comments/1grv58y/google_ads_keyword_planner_says_low_competition/",
         "No provider on this page fixes that, and any page that says otherwise is selling. "
         "The bid fields are an estimate of an auction. Use them to rank keywords against one "
         "another, and take what you actually pay from your own campaign data."),
        ("Twenty thousand keywords is not a spreadsheet job",
         "there are like 20,000 keywords, so it's not something a standard VLOOKUP will work for",
         "r/SEO", "https://www.reddit.com/r/SEO/comments/1dj6b4z/keyword_api_suggestions/",
         "This is the case per call pricing is for. A flat per request row batches up to a "
         "thousand keywords at a time, so twenty thousand is twenty calls at a rate printed "
         "before the first one, with no plan to be on."),
        ("The tools do not know about traffic you are already getting",
         "lately we've been seeing a lot of traffic in Google Search Console from keywords that don't even exist in the tools we're using",
         "r/SEO", "https://www.reddit.com/r/SEO/comments/1ry5v7o/should_i_trust_tools_on_keyword_search_volume/",
         "Which is the honest limit of every row here. These are models of demand, not "
         "records of it. Your own Search Console data is the one source that is actually "
         "counting, and it is a free own account row in this same catalog."),
    ],
    "related": ("Keyword ideas from a seed", "Google results for a keyword",
                "Keywords a domain ranks for", "Keywords a domain bids on"),
}


# The workflow pages (`/workflows/<slug>`): the sequence a person actually runs, as ONE prompt. A
# use-case page answers one job; a workflow chains several, with a per-step price pulled live from
# the catalog and a receipt from a real run. `run` is hand-recorded from that run and dated. A
# workflow page is never written without a real run behind it; the numbers are the page.
# Each step: (name, capability id, what the agent effectively asks, the endpoint the worked run
# used, a one-line why). The step's link resolves through USE_CASES by capability at request time.
WORKFLOWS: dict[str, dict] = {}

WORKFLOWS["find-and-verify-a-lead-list"] = {
    "sentence": "AI lead generation: build a verified lead list from one prompt",
    "title": "AI lead generation: a verified lead list in {n} calls | treg.to",
    "lede": (
        "Give your agent one prompt and get back a lead list with a named person, a verified work "
        "email and a reason to write, for every company that matched. {steps} steps, each a "
        "metered call through one treg.to key, with the price printed before the agent spends it. "
        "The numbers on this page come from running it, not from a rate card."),
    "prompt": (
        "Using treg, build me a lead list: 50 US software companies with 51 to 200 staff that raised "
        "a Series A. For each one find the VP or Head of Marketing, find their work email with the "
        "cheapest provider that only bills on a hit, verify it, and pull the latest news so I have "
        "an opener. Show me the total price before each step, and give me a CSV at the end with "
        "the deliverable ones first."),
    "prompt_why": [
        ("One list in, one CSV out", "The agent carries the domain from step to step. You never paste anything twice."),
        ("Ask for the price before each step", "Every step is metered per call, so the agent can show the bill before it spends."),
        ("Only bill on a hit", "Most email finders' rate cards charge nothing for a miss. Say so and the agent picks one."),
        ("Deliverable first", "Verification sorts the list into send, do not send, and unknown. Ask for that order."),
    ],
    # Steps whose endpoint is called ONCE per run rather than once per row. The list step is one
    # page for all 50 companies (Apollo bills per page); everything after it runs per row. The
    # "at the rates above" total and the hub's per-row price both read this — without it the page
    # charged 50 × the list price and blamed misses for a gap that was mostly its own arithmetic.
    "once": ("apollo.companies.search",),
    # Each step: (name, capability id, the line the agent effectively asks, the endpoint the worked run used, one-line why)
    "steps": [
        ("Build the company list", "companies.search",
         "50 US software companies, 51 to 200 staff, latest round Series A",
         "apollo.companies.search",
         "Apollo bills per page, not per company, so one page of 50 is one charge."),
        ("Find the person", "people.search",
         "the VP or Head of Marketing, or Head of Growth, at each company",
         "findymail.search.employees",
         "Findymail's rate card bills per contact returned. LeadMagic's role finder is the fallback, and it settled at $0.00 on every miss in the run."),
        ("Find the work email", "people.email.find",
         "their work email, cheapest provider that only bills on a hit",
         "tomba.people.email.find",
         "Tomba is the cheapest per-success finder in the catalog. Hunter runs on Tomba's misses and settled at $0.00 on its own."),
        ("Verify it", "people.email.verify",
         "drop anything not deliverable, keep the unknowns separate",
         "leadmagic.people.email.verify",
         "LeadMagic bills a quarter credit per definitive verdict and nothing for an unknown."),
        ("Find an opener", "companies.news",
         "the three most recent news events about each company",
         "predictleads.companies.news_events",
         "PredictLeads bills $0.04 a call for classified events. Akta is a third of that per call but was out of credit on the day, so the run used PredictLeads."),
    ],
    "run": {
        "date": "2026-08-26",
        "rows_in": 50,
        "receipt": [
            ("Companies matched", "746 on Apollo; the first page of 50 taken, one charge of $0.026"),
            ("Rows with a usable domain", "47 of 50"),
            ("A named marketing lead found", "40 of 47 (27 by Findymail, 13 by LeadMagic's role finder)"),
            ("Work email found", "31 of 40 (22 by Tomba, 9 by Hunter on Tomba's misses)"),
            ("Verified deliverable", "27 of 31; 4 invalid; 0 unknown or catch-all"),
            ("A news event in the last year", "29 of 31 (PredictLeads; Akta refused every call, see below)"),
            ("Wall clock, one call at a time", "about 21 minutes; Findymail and Tomba take 10 seconds a row"),
            ("Total metered", "$3.62 for 50 companies, or $0.13 per deliverable lead"),
        ],
        "cost_usd": 3.62,
        "csv": "/workflows/find-and-verify-a-lead-list.csv",
        "narrative": [
            "Every number above is what treg.to's ledger settled on 2026-08-26 for this run, not a "
            "rate-card estimate. The 50 rows cost $0.026 to list, $1.58 to name a person ($0.93 at "
            "Findymail, $0.65 at LeadMagic), $0.58 to find emails, $0.19 to verify them and $1.24 for "
            "news. The news step was the dearest per row because Akta, the cheapest provider for it, "
            "answered every call with an insufficient-credits error on treg.to's own key and the run "
            "fell back to PredictLeads at $0.04 a call. A miss on a per-success endpoint is free at "
            "Hunter and LeadMagic, and both showed it: 9 of Hunter's 18 calls and 14 of the role "
            "finder's 27 settled at $0.00. Findymail and Tomba list a free miss too, but treg.to "
            "settled all 47 Findymail calls and all 40 Tomba calls at the list rate, misses included, "
            "because neither provider reports the charge in its response. That is $0.56 of the $3.62, "
            "and it is being fixed on treg.to's side.",
            "Where the rows fell out: 3 Apollo rows had no domain (two were acquired companies). "
            "Neither people provider had a marketing lead for 7 of the 47 companies; the ones "
            "LeadMagic's role finder returned drift in seniority, so a request for Head of Marketing "
            "came back as a Marketing Manager at four companies. Of the 40 named people, 9 had no "
            "findable work email at either finder, and 4 of the 31 addresses found failed "
            "verification. Nothing landed in the unknown bucket, which is unusual for a B2B list "
            "and says more about this list of small software companies with plain mail setups than "
            "about the verifier. Apollo's United States filter also let a handful of Indian and "
            "Singaporean companies through; check the location column before you send.",
        ],
    },
    "failure_modes": [
        ("The filter returns almost nothing",
         "Icypeas' company search sized the same filter at 12 companies, Apollo at 746. Size the filter with a free count call before paying for a page, and expect the count to swing an order of magnitude between providers."),
        ("A row with no domain",
         "Three of the 50 Apollo rows carried no primary domain (two were acquired companies). Every later step keys on the domain, so those rows stop at step one. Keep them in the CSV with the reason rather than dropping them silently."),
        ("The people search times out, or nobody has the person",
         "LeadMagic's people search answered \"query too broad\" for a single domain with six titles, at no charge. Findymail by title returned a person for 27 of 47 companies and LeadMagic's role finder for 13 of the remaining 20. Nobody's database has a marketing lead for every 100-person company; the miss rate is the workflow, not a bug."),
        ("The cheapest provider is out of credit",
         "Akta answered all 31 news calls with an insufficient-credits error on treg.to's own key, at no charge, and the run fell back to PredictLeads at four times the price. A provider outage shows up as a price change, so ask the agent for the price before each step, not once at the start."),
        ("Catch-all domains",
         "A verifier cannot resolve an address on a domain that accepts everything. Expect a fifth of a B2B list to land in that bucket, and decide once, per campaign, whether to send to it."),
    ],
    "faq": [
        ("How much does the whole workflow cost?",
         "The receipt on this page prints the real total for a 50-company run. Per-call rates are the provider's own with $0.000 added by treg.to. A miss on a per-success step is free at the provider's rate card; the receipt shows where that held and where it did not."),
        ("Can I change the filter or the title?",
         "Yes. The prompt is plain text. Change the industry, headcount band, funding stage or the job title and the agent changes the calls. The prices per step do not change."),
        ("Does treg.to pick the providers?",
         "No. treg.to shows the agent every provider for each step with its price and measured success rate; the agent picks, or you tell it which one. There is no automatic failover."),
        ("What comes back at the end?",
         "A CSV with company, domain, person, title, email, which provider found it, the verifier's verdict, whether the domain is catch-all, and the latest news event. The one from the run on this page is linked above with the person, title and email columns removed, because these are real people and a title at a named company is enough to identify one; the row-level outcomes are what the numbers on this page come from. Your own run returns every column."),
    ],
    "related": ("Find professional emails", "Verify an email before you send",
                "Find people by role, company or location", "Build a company list by industry, size or tech"),
}


USE_CASE_PAGES["company-email-format"] = {
    "label": "A company's email format",
    "sentence": "Company email format finder: the pattern a domain uses, so a name becomes an address",
    "title": "Company email format finder API, by domain | treg.to",
    "lede": (
        "Give your agent a company domain and get back the address pattern the company uses, "
        "such as first.last or f.last, with how confident the provider is in it. Two providers "
        "answer through one treg.to key at their own rate with $0.000 added: The Companies API "
        "bills only when a pattern comes back, Tomba bills the call and makes a repeat of the "
        "same domain free for the rest of the month. A pattern is a rule, not a mailbox: it "
        "tells you how to write an address, and the verify page tells you whether that "
        "address exists."),
    "prompt": "Using treg, get the email format for these 40 company domains, show me the "
              "price per domain for each provider first, then give me a table of domain, "
              "pattern, confidence and which provider answered, and leave the pattern blank "
              "where nothing came back rather than guessing first.last.",
    "prompt_why": [
        ("Give the domain, not the company name", "Both rows take a domain. A name has to be resolved to a domain first, and that is a separate call at the same rate."),
        ("Ask for the confidence with the pattern", "A pattern seen on three addresses is a guess; one seen on three hundred is a rule. The number is the difference."),
        ("Ask for the price per domain", "The two rows meter differently: one per pattern found, one per call. Forty domains has a worst case before anything runs."),
        ("Do not let the agent default to first.last", "A model asked for a format will offer the commonest one. Say that an empty answer stays empty, then verify before sending."),
    ],
    "result_noun": "pattern",
    "result_image": None,
    "q_cheapest": "Which email format finder is cheapest?",
    "q_reliable": "Which email format finder is the most reliable?",
    "q_compare": "How do the two compare?",
    "what_is_heading": "What is a company email format finder?",
    "what_is": (
        "It is a call that takes a domain and returns the shape of that company's addresses, "
        "learned from addresses already seen on that domain: first.last@, first@, flast@, "
        "first_last@ and so on, usually with a count or a confidence behind each. With the "
        "pattern and a person's name, an agent can write a probable address for anyone at the "
        "company without a per person lookup. It is the cheap first step in an outreach "
        "pipeline, and only the first step: a probable address still bounces if the person "
        "has left, uses a middle initial or is the one exception, which is why the pattern "
        "call is normally followed by a verify call on the address it produced."),
    "notes": [
        "The two rows bill in different units, so the cheapest depends on your hit rate. The "
        "Companies API is priced per pattern found, so a domain with no known addresses costs "
        "nothing; Tomba is priced per call, and its logs showed the first call of the day at "
        "one credit and two replays of the same domain at zero, so a repeat inside the month "
        "is free. On a list of well known domains the per call row is the cheaper one; on a "
        "list of small companies the per found row is. Both are the provider's own rate with "
        "$0.000 added.",
        "A pattern is only as good as the sample behind it. Both providers infer the format "
        "from addresses they have seen on the domain, and a domain with three known addresses "
        "yields a confident looking pattern that may be wrong for the fourth person. Read the "
        "confidence or count the provider returns, and treat anything built on a handful of "
        "samples as a hypothesis to verify, not a fact to send to.",
        "Tomba's row has been called live through treg.to and carries a verified date; The "
        "Companies API row is documented but not yet verified through treg.to, so run one "
        "domain before an agent runs a thousand. The 1,300 a month who search for an email "
        "format checker are mostly asking whether an address is valid, which is a different "
        "call: that is the verify page, and the two calls are meant to run one after the other.",
    ],
    "faq": [
        ("Does the pattern give me a working address?",
         "No. It gives you the rule the company follows, and the address you build from it is "
         "probable, not confirmed. Verify it before sending: a bounce from a guessed address "
         "costs more in sender reputation than the verify call costs in cents."),
        ("Which is cheaper, per pattern or per call?",
         "It depends on how many of your domains have a known pattern. The Companies API charges "
         "only when it finds one, Tomba charges the call and makes repeats free within the "
         "month. Ask the agent to print both rates against your list before it runs."),
        ("Can I get the pattern from a company name instead of a domain?",
         "Not in one step. Resolve the name to a domain first, which Tomba's suggestions row "
         "does for a credit, then ask for the pattern. An agent can chain the two."),
        ("Is guessing addresses from a pattern allowed?",
         "A pattern is public information about how a company names mailboxes, and building an "
         "address from it is what every outreach tool does. What you then send, to whom and "
         "under which rules is your responsibility, and nothing here changes that."),
    ],
    "voices_intro": (
        "This is a thin corpus honestly reported: of the ~180 Reddit and X posts read in "
        "August 2026 about twenty were on the job, and nine vendor clusters, one in bold "
        "Unicode from two accounts, were excluded. Nobody complains about the pattern step "
        "itself; they complain about what happens after it."),
    "voices": [
        ("The pattern is easy, which cuts both ways",
         "My work email follows a predictable format, so I surmise it's not hard to guess from my name.",
         "r/recruitinghell, 2,112 points", "https://www.reddit.com/r/recruitinghell/comments/1r7fc9m/",
         "That predictability is the whole reason a pattern call works, and it is why the "
         "call is cheap: most companies follow one rule. The page will not pretend the hard "
         "part is here; the hard part is knowing whether the person still has the mailbox."),
        ("The price of the tools, or the price of your evening",
         "You either pay thousands for tools like ZoomInfo/Apollo or you spend hours manually scraping LinkedIn and hoping a generic email format works.",
         "r/smallbusiness", "https://www.reddit.com/r/smallbusiness/comments/1t11un9/",
         "Neither, is the honest answer. A pattern lookup is a fraction of a cent per domain, "
         "billed from a prepaid balance with no seat and no contract, and the agent can chain "
         "it to a verify call so the guess is checked before it is sent."),
        ("Everyone is guessing from the same data",
         "Out of 100 people I actually wanted, maybe one address worth sending to.",
         "X", "https://x.com/i/status/2092909603566837762",
         "A pattern will not raise that number on its own. What it changes is the cost of "
         "trying: a probable address for everyone on the list for cents, then verification to "
         "keep the ones that exist. The list itself is still your job."),
        ("Fatigue with the usual databases",
         "I'm increasingly skeptical of relying only on Apollo, Hunter and the usual databases. Everyone is targeting the same people with the same data.",
         "X, 6 likes", "https://x.com/i/status/2092548681425694929",
         "A pattern call is not a database of people. It gives you the rule, and you bring the "
         "names, which means the addresses you build are for the people you chose rather than "
         "the ones every other list already carries."),
        ("Finding the person is the bottleneck, not the address",
         "Scraping Google Maps is easy. Finding the real owner behind each business is the bottleneck.",
         "X, 3 likes", "https://x.com/i/status/2092205559961239719",
         "Agreed, and this page does not solve it. Once you have a name and a domain, the "
         "pattern makes the address a one line step; the people search and enrichment pages "
         "are where the name comes from."),
    ],
    "related": ("Find professional emails", "Verify an email before you send",
                "Enrich a company from its domain", "Find people by role, company or location"),
}


USE_CASE_PAGES["mine-the-comments"] = {
    "label": "Mine the comments",
    "sentence": "Social listening API: export the comments on Instagram, YouTube, Reddit and LinkedIn posts as data",
    "title": "Instagram comment export and social listening API | treg.to",
    "lede": (
        "Give your agent a post URL on Instagram, a video on YouTube, a thread on Reddit or a "
        "post on LinkedIn and get the comments back as rows: author, text, likes, time, "
        "replies. {n} providers across the four platforms answer through one treg.to key, "
        "each at its own rate with $0.000 added, from a fraction of a cent per call, and on "
        "YouTube the official API is free on the Google account you already have. This is the "
        "raw material of social listening, not the dashboard: the rows come back and your "
        "agent does the reading."),
    "prompt": "Using treg, get every comment on these three Instagram posts and this YouTube "
              "video, show me the price per platform first, then group the comments into "
              "questions, complaints and praise, quote the three most upvoted in each group "
              "with a link, and tell me how many comments you actually fetched per post.",
    "prompt_why": [
        ("Give the post, not the account", "Every row here takes one post, video or thread and returns its comments. Comments by one user across many posts is a different job and no row here does it."),
        ("Ask for the price per platform", "The four platforms are priced separately and the units differ: per call, per result, per found. Four posts is a worst case before anything runs."),
        ("Ask for the groups, not the dump", "Three thousand comments is not an answer. The forty that matter are, and that is what the agent is for."),
        ("Ask how many it fetched", "Comments paginate. A provider that returns the first page and stops is a partial read that looks complete unless the count is on the table."),
    ],
    "result_noun": "comment",
    "result_image": None,
    "q_cheapest": "What do comments cost, per platform?",
    "q_reliable": "Which comment API is the most reliable?",
    "q_compare": "How do the platforms compare?",
    "what_is_heading": "What is a social listening API?",
    "what_is": (
        "Strictly, it is the fetch step of social listening: a call that takes a post and "
        "returns the public comments under it as structured rows, so software can read them "
        "instead of a person scrolling. The listening suites sell the whole loop, monitoring, "
        "alerts, sentiment and a dashboard, by the seat. These rows sell the one part that is "
        "hard to build, getting the comments off the platform, per call, and leave the "
        "reading to your agent. On YouTube the official Data API does this for any public "
        "video on your own key. On Instagram the official API only reaches comments on posts "
        "you manage, which is why the paid rows exist: they fetch comments on anyone's public "
        "post. Reddit and LinkedIn have paid rows only."),
    "notes": [
        "The four platforms are not the same job, and the prices say so. YouTube is the cheap "
        "one: TikHub at a tenth of a cent per successful call, and the official API free on "
        "your own Google account for any public video, ten thousand quota units a day at one "
        "unit per call. Instagram runs from TikHub, ScrapeCreators and Bright Data at fractions "
        "of a cent, with the official row free but limited to comments on your own posts. "
        "Reddit is priced like YouTube. LinkedIn is the dear one, from Aviato at two cents per "
        "found, and TikHub's LinkedIn row carries no published price yet, so the table prints "
        "none for it. All of it is the provider's own rate with $0.000 added.",
        "Units differ across the rows, so read the unit before the number. ScrapeCreators is "
        "per call, which on a post with a thousand comments means paginating and paying per "
        "page; Bright Data is per record delivered, so a thousand comments is a thousand "
        "records; TikHub and JustOneAPI are per successful call. The row labelled treg is the "
        "routed endpoint: the explicit opt in where you ask treg.to to choose among the "
        "providers for that platform, your own keys first, and it names the provider that "
        "served and bills that provider's rate. Every other row is a direct call and the "
        "choice is yours.",
        "What no row here does: comments by one user across many posts, which is the question "
        "the OSINT forums keep asking; comments on a private account; and any promise about "
        "your own LinkedIn or Instagram login, because none of these rows use one. A "
        "scraped LinkedIn post is public data fetched by the provider, and LinkedIn's terms "
        "are LinkedIn's. Several rows on the page have not been called live through treg.to "
        "yet and say unverified in the table: Bright Data on all three platforms, "
        "ScrapeCreators on Reddit, TikHub on LinkedIn. Run one post before an agent runs a "
        "thousand.",
    ],
    "faq": [
        ("Can I get all the comments a particular user has left?",
         "No. Every row takes a post and returns its comments. There is no row that takes a "
         "username and returns everything they have written, on any of the four platforms, "
         "and this page will not pretend one exists."),
        ("Does the official Instagram API do this for free?",
         "Only on posts you manage. The Instagram row here is free on your own connected "
         "account and returns comments on your own posts. Comments on a competitor's post "
         "need one of the paid rows, which fetch any public post at a fraction of a cent."),
        ("Is this a replacement for a social listening tool?",
         "For the fetch, yes, and for cents rather than a seat. For the rest, no: there is no "
         "alerting, no dashboard and no case management here. The rows return comments, "
         "your agent reads them, and you decide what to build on top."),
        ("Will my account get banned?",
         "Nothing here uses your account except the two official rows, which use it the way "
         "the platform intends. The paid rows run on the provider's own infrastructure and "
         "return public data; treg.to holds the provider keys and you never log in anywhere."),
    ],
    "voices_intro": (
        "Of the ~210 Reddit and X posts read in August 2026 about seventy were on the job, and "
        "eleven vendor clusters were excluded, including one coordinated push across seven X "
        "accounts and one post with a word joiner hidden inside its words. These five are "
        "people trying to read comments, not sell a tool."),
    "voices": [
        ("The comments of one user, not one post",
         "Is there a way/tool to find all instagram comments of a particular user?",
         "r/OSINT, 17 points", "https://www.reddit.com/r/OSINT/comments/zp45ym/is_there_a_waytool_to_find_all_instagram_comments/",
         "Not through any row on this page, and not through any provider in the catalog. The "
         "rows go post to comments. Saying so plainly is worth more than a page that lets an "
         "agent try and fail."),
        ("Five hundred comments, without a subscription",
         "Looking for a free open-source tool to scrape Instagram comments (approx. 500)",
         "r/osinttools, 6 points", "https://www.reddit.com/r/osinttools/comments/1txogbf/looking_for_a_free_opensource_tool_to_scrape/",
         "Not free, but close: five hundred Instagram comments is a handful of pages at a "
         "fraction of a cent each, from a prepaid balance that starts with a dollar of credit "
         "and no card. The open source route works until the platform changes; the paid row "
         "is somebody else's job to keep working."),
        ("The fear is the ban, not the fetch",
         "How can I do a cron job that scrape likes and comments on Instagram without get banned",
         "r/DataHoarder", "https://www.reddit.com/r/DataHoarder/comments/1qcdcg0/how_can_i_do_a_cron_job_that_scrape_likes_and/",
         "By not using your account. The paid rows here fetch public posts on the provider's "
         "side, so there is no session of yours to flag. What they cannot reach is a private "
         "account, and no row should be expected to."),
        ("Three thousand comments, forty that matter",
         "a video with 3,000 comments might have 40 that are actually useful to me and the rest is noise",
         "r/claude, 3 points", "https://www.reddit.com/r/claude/comments/1ragfjd/im_building_a_youtube_comment_filtering_tool_with/",
         "That is the whole design of the prompt on this page: fetch cheaply, then have the "
         "agent group and quote. The fetch is the commodity; the filtering is where the "
         "reader's own judgement, and the model, earn their keep."),
        ("People do not say what a dashboard can count",
         "People won't say 'this product lacks value.' They'll say 'idk man feels overpriced for what it is'",
         "r/AIAgentsStack, 24 points", "https://www.reddit.com/r/AIAgentsStack/comments/1q57jgj/anyone_else_realizing_social_listening_is_way/",
         "Which is the case for raw comments over a sentiment score. A keyword dashboard "
         "misses that sentence; an agent reading the rows does not. The rows are the cheap "
         "part, and they are what this page sells."),
    ],
    "related": ("A video's comments", "Search posts by keyword",
                "Find creators by keyword", "Posts under a hashtag"),
}


USE_CASE_PAGES["build-a-company-list-by-industry-size-or-tech"] = {
    "label": "Build a company list by industry, size or tech",
    "sentence": "Companies by industry: build a company list by industry, size, location or tech stack through one key",
    "title": "Company list by industry, size or tech: {n} APIs | treg.to",
    "lede": (
        "Describe the companies you want, by industry, headcount, country, revenue, funding or "
        "the technology they run, and get a list back as rows with a domain on each. {n} "
        "providers answer through one treg.to key, from Apollo and Crunchbase to the smaller "
        "databases nobody has heard of, each at its own rate with $0.000 added and most of "
        "them priced per company returned, so the size of the page is the price. Three rows "
        "are free, and a free row that returns ids is not the same as a free row that returns "
        "companies; the notes say which is which."),
    "prompt": "Using treg, build me a list of 200 B2B software companies in Germany with 50 to "
              "500 employees, show me the price for 200 rows from each provider first, run the "
              "two cheapest, dedupe on domain, and tell me how many rows each one returned and "
              "how many overlapped.",
    "prompt_why": [
        ("Say the filters in plain words", "Every row here filters on industry, size and location, and most on tech or funding. The agent maps your words to each provider's field names."),
        ("Ask for the price for N rows, not per row", "Most rows bill per company returned, Apollo bills per page, Tomba bills per fifty. The same 200 rows costs a different amount on every row."),
        ("Run two and compare the overlap", "No provider has every company. Two cheap lists deduped on domain is the honest way to find out which one covers your market."),
        ("Dedupe on domain, not name", "The same company arrives as three spellings of its name and one domain. The domain is the key for everything downstream."),
    ],
    "result_noun": "company",
    "result_image": None,
    "q_cheapest": "Which company search API is cheapest?",
    "q_reliable": "Which company database is the most reliable?",
    "q_compare": "How do the company databases compare?",
    "what_is_heading": "What is a company list by industry?",
    "what_is": (
        "It is the output of a company search call: you send filters, industry, headcount "
        "band, country, revenue, funding stage, technology in use, and the provider returns "
        "the companies in its database that match, with a domain, a name and whatever "
        "firmographics it holds. It is the first step of account based prospecting and of "
        "most market sizing, and every provider on this page sells the same shape of answer "
        "from a different database. The differences that matter are coverage, which is "
        "where a company actually gets indexed, how stale the headcount and industry are, "
        "and the billing unit, which decides what a two hundred row list costs before you "
        "know whether it is any good."),
    "notes": [
        "The billing unit is the whole story, and it varies. Most rows bill per company "
        "returned, so the page size is the price: Icypeas at a few hundredths of a cent, The "
        "Companies API, Aviato, Lusha, Crustdata, CompanyEnrich and Fiber AI at fractions of "
        "a cent, LeadMagic and Diffbot around three cents, PredictLeads four, and People Data "
        "Labs at thirty eight cents a record, two hundred times the cheapest. Apollo bills "
        "per page rather than per company, so a full page is cheaper than a small one, the "
        "opposite of everyone else. Tomba bills one credit per fifty companies revealed. "
        "Crunchbase, Ocean.io and Findymail publish no dollar rate, so the table prints none. "
        "All of it is the provider's own rate with $0.000 added.",
        "Three rows are free and they are not interchangeable. Hunter Discover returns "
        "companies with their email counts and consumes no credits. Coresignal's search is "
        "free because it returns ids only; the cost lands on the collect step that turns an "
        "id into a company. Akta's row is a name or domain to id lookup, not a filtered "
        "search. The Companies API has a `simplified=true` switch that makes the whole call "
        "free at the price of fewer fields, which is the cheapest honest way to count a "
        "market before paying for rows. The row labelled treg is the routed endpoint: the "
        "explicit opt in where you ask treg.to to choose among these providers, your own "
        "keys first, and it names the one that served and bills that provider's rate.",
        "Nobody on this page publishes a number for how stale their data is, and the one "
        "practitioner test in the research measured the drift at about a fifth of records "
        "having changed title or employer while the email still resolved. Treat every "
        "headcount and industry field as a claim from the month it was indexed. The rows "
        "that were called live through treg.to carry a verified date in the table; Apollo, "
        "Coresignal, Crunchbase, Fiber AI, Findymail and The Companies API are documented "
        "but not yet verified through treg.to, so run one page before an agent "
        "runs a thousand.",
    ],
    "faq": [
        ("Which provider has the best company data?",
         "The research behind this page found no independent benchmark, and every accuracy "
         "figure in circulation was written by the vendor selling it. Run the same filter "
         "through two or three cheap rows, dedupe on domain, and count. Two hundred rows from "
         "three providers costs less than a dollar on most of them."),
        ("Why does the same list cost so much more on one provider?",
         "Because the billing units differ. Per company, per page and per fifty are not the "
         "same thing, and one provider charges thirty eight cents a record where another "
         "charges a few hundredths of a cent. Ask the agent to print the price for your list "
         "size on every row before it runs."),
        ("Can I count the matches before paying for the rows?",
         "On some rows, yes. The Companies API's simplified mode is free, Coresignal's search "
         "returns ids for nothing, and several providers have a count call on its own page. "
         "A count first is the cheapest way to check that your filters mean what you think."),
        ("Is this the Apollo API or the Crunchbase API?",
         "Both are rows on this page, called through one treg.to key at their own rates. "
         "Crunchbase is covered by its licence rather than priced per call, so the table "
         "prints no dollar figure for it, and Apollo bills per page rather than per company."),
    ],
    "voices_intro": (
        "This is a thin corpus honestly reported: of the ~210 Reddit and X posts read in "
        "August 2026 about ten were on the job, and a template farm of eight near identical "
        "review posts across eight invented subreddits was excluded, along with a post that "
        "hid word joiners inside its bracket tags. These five are practitioners, and they "
        "are mostly talking about what goes wrong after the list arrives."),
    "voices": [
        ("Accuracy drifts once nobody is checking",
         "even the best b2b data enrichment tools for crm accuracy drift once they go live and nobody double checks",
         "r/SmallBusiness_US, 3 points", "https://www.reddit.com/r/SmallBusiness_US/comments/1vx3uub/even_the_best_b2b_data_enrichment_tools_for_crm/",
         "No provider here publishes a freshness number, and this page will not invent one. "
         "What per call pricing changes is the cost of checking: re-pull a sample of your "
         "list every quarter for cents and measure the drift yourself."),
        ("Four vendors, one ICP, bounce rates from 2% to 19%",
         "Verified 4 B2B data vendors against the same ICP. Bounce rates ranged from 2% to 19%",
         "r/Coldemailing", "https://www.reddit.com/r/Coldemailing/comments/1vezuay/verified_4_b2b_data_vendors_against_the_same_icp/",
         "That test is the method this page recommends, and per call pricing is what makes it "
         "affordable: the same filter through several rows, deduped on domain, then verified. "
         "The spread that poster found is the reason not to trust any single row, this "
         "page's cheapest included."),
        ("Ninety five percent accurate, of what?",
         "I got tired of vendor pages claiming '95%+ accuracy' with no definition of what accuracy means",
         "r/Coldemailing", "https://www.reddit.com/r/Coldemailing/comments/1vezuay/verified_4_b2b_data_vendors_against_the_same_icp/",
         "So there is no accuracy column here. The table carries the rate, the billing unit, "
         "the fields each row accepts and whether it has been called live through treg.to. "
         "Accuracy on your market is something you measure, not something a vendor states."),
        ("Which of these is a database and which is a scraper",
         "Tried searching online but getting overwhelmed with options and not sure which ones are legit vs just data scrapers",
         "r/b2bmarketing", "https://www.reddit.com/r/b2bmarketing/comments/1r08jcy/need_recommendations_for_b2b_contact_data/",
         "That is what a comparison with real rates is for. Every row here is a named "
         "provider with a documented endpoint and a published or observed price, and the "
         "difference between them is on the table rather than in a review somebody paid for."),
        ("Crunchbase without the scraping workaround",
         "Turns out Crunchbase has a bunch of restrictions, so I tried a couple methods to scrape leads efficiently",
         "r/weezly", "https://www.reddit.com/r/weezly/comments/1n38bo0/how_i_scraped_thousands_of_crunchbase_leads_free/",
         "Crunchbase's own search API is a row on this page, called through treg.to on a "
         "licence rather than per call. It is not the cheapest route to a list, and a "
         "different row will be for most filters, but it is the legitimate one."),
    ],
    "related": ("Enrich a company from its domain", "Find people by role, company or location",
                "Count the matches before you pay for rows", "Find companies that use a given technology"),
}


USE_CASE_PAGES["job-postings-across-companies"] = {
    "label": "Job postings across companies",
    "sentence": "LinkedIn jobs API and job scraper: job postings across companies as rows your agent can read",
    "title": "Jobs API: LinkedIn job postings and company openings | treg.to",
    "lede": (
        "Search job postings by title, keyword, company or location and get them back as "
        "rows, for hiring signals, market research or a job board of your own. {n} providers "
        "answer through one treg.to key on two shelves: LinkedIn job search from a tenth of a "
        "cent per posting, and company level openings from the firmographic databases, each "
        "at its own rate with $0.000 added. Two things this page will not pretend: there is "
        "no Indeed row in the catalog, so the 720 people a month searching for an Indeed API "
        "will not find one here, and no row here removes duplicates, ghost jobs or stale "
        "dates. The postings arrive as the platform shows them."),
    "prompt": "Using treg, find every posting for a data engineer in Berlin published in the "
              "last week, show me the price per 100 postings from each provider first, cap "
              "the run at 500 rows, then give me a table of company, title, date posted and "
              "link, deduped on the posting URL, and say how many rows came back.",
    "prompt_why": [
        ("Give the title and the place", "The LinkedIn rows take a keyword and a location; the company rows take a company or a filter. Say which you have."),
        ("Cap the run", "The Apify row bills per posting it returns and a broad search returns thousands. A maxItems cap is the difference between a cent and a dollar."),
        ("Ask for the date and the link", "The date posted is the field nobody trusts and the link is the one that lets you check. Both belong on the table."),
        ("Dedupe on the posting URL", "The same job is reposted, cross-posted and recycled. No row here dedupes for you; the agent has to."),
    ],
    "result_noun": "posting",
    "result_image": None,
    "q_cheapest": "What do job postings cost, per shelf?",
    "q_reliable": "Which jobs API is the most reliable?",
    "q_compare": "How do the two shelves compare?",
    "what_is_heading": "What is a jobs API?",
    "what_is": (
        "It is a call that searches job postings and returns them as structured rows: title, "
        "company, location, date, description, link. The boards themselves mostly do not "
        "offer one to the public any more, which is why the searches for an Indeed API keep "
        "landing on scrapers. The rows here come in two shapes. The LinkedIn shelf searches "
        "LinkedIn's postings by keyword and location and returns the postings, priced per "
        "posting or per call. The companies shelf comes from the firmographic databases, "
        "which index job openings per company as a hiring signal, so the natural question "
        "there is which companies are hiring for what, rather than which jobs match a title."),
    "notes": [
        "Two shelves, two prices. On LinkedIn, Apify's job search actor bills a tenth of a "
        "cent per posting returned with no platform charge on top, and TikHub's LinkedIn row "
        "bills a tenth of a cent per successful call, which on a big search is the cheaper of "
        "the two. On the companies shelf Crustdata bills under a cent per result, LeadMagic "
        "two and a half cents, and PredictLeads four cents a call for a company's openings "
        "and a credit per record for a filtered search. All of it is the provider's own rate "
        "with $0.000 added. The row labelled treg is the routed endpoint: the explicit opt "
        "in where you ask treg.to to choose among the company rows, your own keys first, and "
        "it names the provider that served and bills that provider's rate.",
        "The cap is the budget. The Apify row is charged per dataset item it produces, so a "
        "search that matches four thousand postings costs four dollars unless maxItems says "
        "otherwise; set it on every call and read the run's usage afterwards. Per call rows "
        "have the opposite shape: one page costs the same whatever it holds, so page size is "
        "free and pagination is the cost.",
        "What no row here does, said plainly: nothing dedupes across boards, nothing checks "
        "whether a posting is still live or was ever real, nothing crawls a company's own "
        "careers page, and there is no Indeed, Glassdoor or Naukri row. The date posted is "
        "the platform's date, which the research says is the field people trust least. "
        "TikHub's LinkedIn job search has not been called live through treg.to yet and is "
        "marked unverified in the table; run one search before an agent runs a hundred.",
    ],
    "faq": [
        ("Is there an Indeed API here?",
         "No. Indeed closed its publisher API and the catalog carries no Indeed row, scraped "
         "or official. The LinkedIn rows are the closest thing on this page, and the company "
         "rows answer a different question: who is hiring, not which jobs match a title."),
        ("Will I get duplicates and ghost jobs?",
         "Yes, as many as the platform shows. No row here dedupes across reposts or checks "
         "that a posting is real, and this page will not claim otherwise. Dedupe on the "
         "posting URL in the agent, and treat the date posted as the platform's claim."),
        ("How much does a big pull cost?",
         "On the per posting row, the number of postings times a tenth of a cent, which is why "
         "the prompt caps the run. On the per call rows, the number of pages. Ask the agent "
         "to print the price for your cap on every row before it runs."),
        ("Can I get a company's openings as a hiring signal?",
         "That is what the companies shelf is for. PredictLeads, Crustdata and LeadMagic index "
         "openings per company, so an agent can ask which of your two hundred target accounts "
         "opened an engineering role this month, without searching a board at all."),
    ],
    "voices_intro": (
        "Of the ~210 Reddit and X posts read in August 2026 roughly half were on the job, the "
        "densest corpus behind any of these pages, and five vendor clusters were excluded, "
        "including one launch story reposted to three subreddits with the AI tool's name "
        "swapped each time. These five are people fighting the boards."),
    "voices": [
        ("The boards fight back",
         "Since when did Indeed start injecting invisible fake job cards as a scraper honeypot?",
         "X, 189 likes", "https://x.com/i/status/2057908987845300478",
         "Which is why there is no Indeed row here and this page says so in its first "
         "paragraph. The LinkedIn rows exist because a provider absorbs that fight on its "
         "side; on Indeed nobody in the catalog does."),
        ("The filters are the problem, not the data",
         "I hate LinkedIn and Indeed. Filters don't work well, search experience is terrible, and the sites are contaminated with so many offshore agencies.",
         "r/findapath, 745 points", "https://www.reddit.com/r/findapath/comments/1eqn8xm/i_decided_to_scrape_15_million_job_postings_using/",
         "Rows fix the filter half: once the postings are data, your agent filters them by "
         "whatever rule you like. The contamination half arrives with the data, and the "
         "agent is the filter for that too."),
        ("The date is the field nobody trusts",
         "Not being able to trust the date posted of any job. Being shown too many irrelevant jobs.",
         "r/leetcode, 401 points", "https://www.reddit.com/r/leetcode/comments/1itbh82/i_scraped_2500_software_engineering_jobs_from/",
         "No row here fixes the date; it returns what the platform shows. What a daily pull "
         "gives you is your own first seen date, which is the only one you can trust, and "
         "that costs a tenth of a cent a posting."),
        ("Half the postings are gone in a month",
         "Half are gone by day 28, and almost all of that drop happens in a single week.",
         "r/jobsearchhacks, 376 points", "https://www.reddit.com/r/jobsearchhacks/comments/1vujb75/i_tracked_165m_job_postings_daily_for_two_months/",
         "That poster tracked over a million postings a day to learn it, and the method is "
         "the point: a repeated pull is what turns postings into a signal. The rows here "
         "make the pull cheap; the tracking is the agent's job."),
        ("The old way was cheaper and about as good",
         "Everyone in this community is bragging about AI-powered automations or seemingly simple workflows that call overpriced APIs to parse a page.",
         "r/n8n, 307 points", "https://www.reddit.com/r/n8n/comments/1op8oho/scraping_linkedin_jobs_no_ai_no_paid_apis/",
         "Fair, and a tenth of a cent per posting is the answer to overpriced rather than to "
         "free. The self written scraper wins until LinkedIn changes something; the row wins "
         "the day after."),
    ],
    "related": ("Hiring, headcount and news signals", "Employee reviews of a company",
                "Get a company's LinkedIn page", "Build a company list by industry, size or tech"),
}


USE_CASE_PAGES["daily-price-history"] = {
    "label": "Daily price history",
    "sentence": "Historical stock data API: daily OHLCV price history by ticker, free to try, then your own Polygon, EODHD or Marketstack key",
    "title": "Historical stock data API: EOD prices, free to try | treg.to",
    "lede": (
        "Give your agent a ticker and a date range and get the daily open, high, low, close "
        "and volume back as rows, adjusted where the provider adjusts. {n} providers answer "
        "through one treg.to key. Tiingo and Twelve Data are free for twenty calls a day per "
        "team on treg.to's own key, enough to backfill a watchlist; past that, and for "
        "Polygon, EODHD and Marketstack, you connect your own subscription key and the calls "
        "are never metered. Two things this page will not pretend: there is no Yahoo Finance "
        "row and no Alpha Vantage row, because neither is in the catalog, and nothing here "
        "is unlimited and free."),
    "prompt": "Using treg, get the daily price history for AAPL, MSFT and NVDA from January "
              "2020 to today, show me which providers are free on treg.to's key and how many "
              "calls I have left today, use one of those, then give me a table of ticker, "
              "date, adjusted close and volume as CSV, and say if any day is missing.",
    "prompt_why": [
        ("Give the ticker and the range", "Every row takes a symbol and a start and end date. Thirty years is one call on most of them, so the range is free to widen."),
        ("Ask which rows are free today", "Two providers are served on treg.to's own key with a daily allowance per team. The agent can see the allowance before it spends one."),
        ("Say adjusted or unadjusted", "Splits and dividends change the series. Tiingo returns both; the others differ. Say which you want before the agent picks."),
        ("Ask for the missing days", "A gap in a daily series is silent unless the agent counts. Ask for it and a holiday looks different from a hole."),
    ],
    "result_noun": "bar",
    "result_image": None,
    "q_cheapest": "What does daily price history cost?",
    "q_reliable": "Which is the most reliable?",
    "q_compare": "How do the providers compare?",
    "what_is_heading": "What is a historical stock data API?",
    "what_is": (
        "It is a call that returns end of day bars for a ticker over a date range: the open, "
        "high, low and close, the volume, and on most providers an adjusted close that folds "
        "splits and dividends back into the series. It is the data behind every backtest, "
        "every chart and every do it yourself portfolio tracker, and it is the data the free "
        "scripts of the last decade leaned on Yahoo Finance for, which is why every time that "
        "unofficial endpoint changes a forum fills with people asking for another. The rows "
        "here are documented APIs with a published allowance or a subscription behind them, "
        "which is the trade: nothing breaks on a Tuesday, and nothing is unlimited."),
    "notes": [
        "Two rows cost nothing to start, on treg.to's own key. Tiingo and Twelve Data are "
        "served from treg.to's free tier keys with an allowance of twenty calls a day per "
        "team; a call is one ticker over any range, so twenty calls is twenty tickers of full "
        "history a day, and past the allowance the call is refused with a hint to connect your "
        "own key. Polygon and EODHD serve on your own subscription key only, and treg.to "
        "publishes no rate for them, so the table prints none: your plan's limits apply and "
        "treg.to meters nothing. Marketstack's row prints a per call figure derived from its "
        "plan's monthly request cap, which is the catalog's way of saying one request against "
        "the cap whatever it returns, not a metered price.",
        "Adjusted and unadjusted are different series and the rows treat them differently. "
        "Tiingo returns both in the same row, with adjusted open, high, low, close and volume "
        "beside the raw ones, across thirty plus years. Polygon's bars endpoint takes an "
        "adjusted flag. Twelve Data, Marketstack and EODHD document their own conventions on "
        "the linked pages. A backtest built on the wrong one will look right until a split "
        "lands in the window, so say which you want in the prompt and check one known "
        "split date.",
        "Coverage is the provider's, not this page's. EODHD documents seventy plus exchanges, "
        "Marketstack a similar spread, Polygon is US equities, and Tiingo and Twelve Data "
        "cover US plus a range of international symbols; the research turned up real demand "
        "for Indian and Sri Lankan exchange data and nothing here promises it. Nothing on "
        "this page serves intraday history except Polygon's bars and Twelve Data's series, "
        "and the crypto history page covers coins. The five provider rows were verified on "
        "2026-08-15; the row labelled treg is the routed endpoint, the explicit opt in where "
        "you ask treg.to to choose among them, your own keys first, and it is unverified.",
    ],
    "faq": [
        ("Is there a free historical stock data API here?",
         "Free to try: Tiingo and Twelve Data at twenty calls a day per team on treg.to's own "
         "key, no card and no account with either provider. Past the allowance you connect "
         "your own key, which on both providers has a free tier of its own. Nothing here is "
         "unlimited and free, and this page will not say otherwise."),
        ("Where is Yahoo Finance or Alpha Vantage?",
         "Not in the catalog. Yahoo has no official API and the unofficial endpoints break on "
         "their own schedule; Alpha Vantage is a documented API whose free tier is a handful "
         "of calls a day, and it is not a row here today. The rows on this page are the "
         "documented alternatives people move to when either fails."),
        ("Do I get adjusted prices?",
         "On Tiingo, both series in one row. On Polygon, with a flag. On the others, per "
         "their documentation, linked from each row. Say which you want in the prompt, and "
         "check one known split date before trusting a backtest."),
        ("Can I use my own Polygon subscription?",
         "Yes, and it is the only way Polygon serves here: connect the key once and every "
         "call runs on your plan, never metered by treg.to. The same is true of EODHD and "
         "Marketstack, and of Tiingo and Twelve Data once the daily allowance is used."),
    ],
    "voices_intro": (
        "Of the ~200 Reddit and X posts read in August 2026 about thirty were on the job, and "
        "six vendor clusters were excluded, including one open source launch posted to four "
        "Indian trading subreddits at once and an eight free APIs list where every link "
        "carried an affiliate tag. These five are people whose data source broke."),
    "voices": [
        ("The free endpoint everyone used",
         "yfinance is so unreliable; any other free apis?",
         "r/algotrading, 118 points", "https://www.reddit.com/r/algotrading/comments/1kdw27f/yfinance_is_so_unreliable_any_other_free_apis/",
         "The honest answer is free to try rather than free: twenty calls a day on treg.to's "
         "key across two documented providers, then your own key. What that buys is an "
         "endpoint with a published contract, which is the thing yfinance never had."),
        ("Public facts, private prices",
         "How is it possible that you need to pay hundreds of dollars just to access historical data / facts that are publicly known?",
         "r/webdev, 117 points", "https://www.reddit.com/r/webdev/comments/151zk8y/is_there_any_free_stock_market_api_that_allows/",
         "Because the exchanges license the data and everyone downstream pays them. The rows "
         "here do not change that; what they change is the entry price, which for a "
         "watchlist of daily bars is zero on two providers and your own plan on the rest."),
        ("Rate limits measured in minutes",
         "even with multithreading i'm looking at 45 minutes to get all the data because of their rate limiting",
         "r/algotrading", "https://www.reddit.com/r/algotrading/comments/76tqyt/alternatives_to_alpha_vantage/",
         "A daily allowance is a rate limit too, and this page says so. The difference is the "
         "shape: one call here is a ticker over its whole history, so twenty calls is twenty "
         "full series, not twenty days of one."),
        ("This data should not be hard to find",
         "In all honesty, I don't feel like this data should be expensive or hard to find.",
         "r/algotrading, 65 points", "https://www.reddit.com/r/algotrading/comments/1atlh3o/i_need_highquality_historical_fundamental_data/",
         "Hard to find is the part a catalog fixes: six documented providers, their "
         "allowances and their conventions on one page, callable through one key. Expensive "
         "is the exchanges' decision, and no page undoes it."),
        ("Fifty eight million answers",
         "there are 58 million answers and the vast majority of them are sarcastic, rhetorical, or a simple 'try this platform'",
         "r/algotrading, 79 points", "https://www.reddit.com/r/algotrading/comments/1nzqrl8/what_preferably_free_apis_are_preferred_for/",
         "This page is one more, so here is what makes it checkable: every row names its "
         "endpoint, its allowance or its key requirement, and its verified date, and the "
         "prompt above asks the agent to show the allowance before spending it."),
    ],
    "related": ("Current quote for a ticker", "News for a ticker",
                "Dividends and splits", "Company profile and fundamentals behind a ticker"),
}
