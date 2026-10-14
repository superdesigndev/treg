# brand-mentions

Who is talking about a brand this week, on Reddit and X, without the noise. Keyword search on both
networks is loose (a search for `linear.app` returns r/LinearTVSupporters; X matches link cards), so
the script keeps a post only when its own text contains the term, deduplicates, and sorts newest
first. `dropped_as_noise` says how much it threw away.

Each mention has `source`, `url`, `author`, `where` (subreddit or x), `created_at`, `text` (first
600 characters) and `engagement`. X search goes through treg's router, so the cheapest X provider
answers.

Priced in the script: `ctx.charge(mentions.length * 0.002, "per mention")`, on top of the provider
fees (about $0.003 per run), capped at `max_price_usd` $0.10 (50 mentions, the most `limit` allows).
A run that finds nothing pays only the fees.
