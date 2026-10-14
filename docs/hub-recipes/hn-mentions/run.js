// Hacker News through Algolia's public HN search API. The team registers the host once as its own
// tool, with no secret:
//   treg tool add hn --base-url https://hn.algolia.com
// and lists "hn" in `uses`. treg makes the request; the script never opens a socket.
export default async function run(ctx) {
  const { query, days, kind, sort, min_points, limit } = ctx.inputs;
  const since = Math.floor(Date.now() / 1000) - days * 86400;
  const filters = ["created_at_i>" + since];
  if (min_points > 0) filters.push("points>=" + min_points);
  const tags = kind === "story" ? "story" : kind === "comment" ? "comment" : "(story,comment)";
  const path = sort === "top" ? "hn/api/v1/search" : "hn/api/v1/search_by_date";
  const r = await ctx.call(path, { query: { query, tags, numericFilters: filters.join(","), hitsPerPage: String(limit) } });
  if (r.status !== 200) throw new Error("hn.algolia.com answered " + r.status);
  const hits = (r.json.hits || []).map(h => {
    const isComment = (h._tags || []).includes("comment");
    return {
      kind: isComment ? "comment" : "story",
      title: h.title || h.story_title || null,
      text: String(h.comment_text || h.story_text || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 500) || null,
      url: h.url || null,
      hn_url: "https://news.ycombinator.com/item?id=" + h.objectID,
      story_hn_url: h.story_id ? "https://news.ycombinator.com/item?id=" + h.story_id : null,
      author: h.author, points: h.points ?? null, comments: h.num_comments ?? null, created_at: h.created_at,
    };
  });
  ctx.log(hits.length + " of " + r.json.nbHits + " matches returned");
  ctx.charge(0.002, "fee");                              // the price: $0.002 a run
  return { query, hits, count: hits.length, total_matches: r.json.nbHits };
}
