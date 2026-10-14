# ai-visibility

Ask four AI answer engines the same question. Find out who they mention.

## What it does

1. Sends your question to **ChatGPT, Gemini, Copilot and Google AI Mode**, all at
   the same time. One engine failing never stops the others. The question is wrapped to ask for a
   short list of the top 5 names with a few words each, no introduction and no conclusion: the
   engines answer faster, and "did it name us" is the whole point.
2. For every answer, a judgment model (Jev, through `openrouter.ai-judge.decide`) decides whether
   your **brand** and each **competitor** is referred to as a company or product. This is what a
   text search cannot do: a brand called "Linear" must not match "a linear process". Each name is
   judged on its own, as a probability; 0.7 and above counts as a mention.
3. When you give `brand_domain`, it also reports whether each engine **cited your site** in its
   sources. That needs no judgment: it is a URL match.

## What you get

One row per engine: `status` (answered, skipped, failed, timed_out), the `answer` text, `brand_mentioned`,
`brand_probability`, `brand_cited`, `competitors_mentioned`, `competitor_probabilities`, and
`decided_by` (`judged`, or `text_match` when the judgment call failed and a whole-word match
decided instead). Plus the totals: `brand_mentioned_by`, `brand_cited_by`,
`competitors_mentioned_by`, and a one-line `summary`.

## What it costs

- The four engine calls, at cost: about **1.1 cents** together.
- Four judgment calls, at cost: about **0.01 cents** together.
- Plus **$0.02** for the tool (`ctx.charge(0.02, "fee")`, cap `max_price_usd` $0.02).

## Limits

- At most 8 calls a run. Up to 8 competitors.
- An engine that has not answered in 75 seconds is reported as `timed_out`; the others still count.
- Perplexity is left out for now: it failed half its calls over two weeks. It comes back when its
  provider is stable.
- Each engine refuses a few countries (for example CN, RU). An engine that does not serve your
  `country` is **skipped** and says so; the run still returns the others.
- The judge reads the first 6,000 characters of each answer.
- A run takes as long as the slowest engine, usually 30 to 50 seconds.
