# domain-authority

How strong a site is in Google, in one call.

## What it does

Three Serpstat reports at the same time, folded into one:

- **backlinks summary**: Serpstat's domain rank (0-100), backlinks, dofollow backlinks, referring
  domains and IPs, malicious referring domains, and each number's change since the last check;
- **ranked keywords**: how many keywords the domain ranks for, the top 20 by traffic, how many of
  those sit in Google's top 10, and their summed estimated monthly traffic;
- **linking domains**: the top 20 sites linking here, with their own rank and linking-page count.

## The verdict

A plain rule, in code, so it can be argued with:

- **strong**: 10,000+ referring domains, or rank 60+, or 100,000+ ranked keywords
- **medium**: 500+ referring domains, or rank 30+, or 5,000+ ranked keywords
- **weak**: below that
- **unknown**: no report answered

## What it costs

$0.0025 + $0.0005 + $0.0005 = **$0.0035** in Serpstat calls, plus **$0.01** for the tool (`ctx.charge(0.01, "fee")`).
Serpstat is a database, not a scraper: a run takes a few seconds.

## Limits

- A report that does not answer in 40 s is listed in `failed`; the others still make the card.
- `keywords_top10` and `estimated_monthly_traffic` are computed over the top 20 keywords fetched,
  not the whole set. `keywords_ranked` is the whole set.
