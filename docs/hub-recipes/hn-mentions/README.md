# hn-mentions

Hacker News stories and comments that mention a term, from Algolia's public HN search. For launch
tracking, competitor watching, or finding threads to answer. `sort: new` lists the newest first;
`top` ranks by points. `min_points` drops quiet stories. Each hit carries the HN link, the story's
link for a comment, points, comment count and the first 500 characters of text.

The host is the maker's own tool, registered once with no key:

    treg tool add hn --base-url https://hn.algolia.com

A caller never needs the tool; the run makes the request as the maker. No provider fee; $0.002 per
call.
