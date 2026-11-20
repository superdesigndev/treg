// Brand listening across Reddit and X. Keyword search on both is loose: "linear.app" returns posts
// from r/LinearTVSupporters, and X search matches link cards. So every post is kept only when its
// own text contains the term, which is the whole value of this tool over two raw searches.
// Dates arrive as unix seconds (Reddit) or "Thu Jun 18 09:48:59 +0000 2026" (X), which the
// sandbox's engine will not parse; anything unreadable becomes null instead of throwing.
const MONTHS = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
function iso(v) {
  if (v == null || v === "") return null;
  let ms = /^\d+(\.\d+)?$/.test(String(v)) ? Number(v) * 1000 : Date.parse(v);
  const m = String(v).match(/^\w{3} (\w{3}) (\d{1,2}) (\d{2}):(\d{2}):(\d{2}) \+0000 (\d{4})$/);
  if (m && m[1] in MONTHS) ms = Date.UTC(+m[6], MONTHS[m[1]], +m[2], +m[3], +m[4], +m[5]);
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

export default async function run(ctx) {
  const { term, timeframe, sources, limit } = ctx.inputs;
  const want = new Set(sources.split(",").map(s => s.trim().toLowerCase()));
  const needle = term.toLowerCase();
  const found = [];
  let scanned = 0;

  if (want.has("reddit")) {
    const r = await ctx.call("scrapecreators.reddit.search.posts",
      { query: { query: term, sort: "relevance", timeframe } });
    if (r.status === 200) {
      for (const p of (r.json && r.json.posts) || []) {
        scanned++;
        const text = [p.title, p.selftext].filter(Boolean).join("\n\n");
        found.push({
          source: "reddit", id: "reddit:" + p.id, text,
          url: p.permalink ? "https://www.reddit.com" + p.permalink : p.url,
          author: p.author, where: "r/" + p.subreddit,
          created_at: iso(p.created_utc),
          engagement: { score: Number(p.score) || 0, comments: Number(p.num_comments) || 0 },
        });
      }
    } else ctx.log("reddit answered " + r.status);
  }

  if (want.has("x")) {
    const r = await ctx.call("treg.x.search.posts", { method: "POST", body: { q: term } });
    if (r.status === 200) {
      const out = (r.json && r.json.output) || {};
      for (const t of out.posts || []) {
        scanned++;
        const id = t.tweet_id || t.id || t.rest_id;
        const user = t.screen_name || (t.user_info && t.user_info.screen_name) || (t.author && t.author.username);
        const urls = ((t.entities && t.entities.urls) || []).map(u => u.expanded_url || "").join(" ");
        found.push({
          source: "x", id: "x:" + id, text: [t.text || t.full_text || "", urls].join(" ").trim(),
          url: user && id ? `https://x.com/${user}/status/${id}` : null,
          author: user, where: "x",
          created_at: iso(t.created_at),
          engagement: { likes: Number(t.favorites || t.like_count) || 0, reposts: Number(t.retweets || t.retweet_count) || 0,
                        replies: Number(t.replies || t.reply_count) || 0, views: Number(t.views) || 0 },
        });
      }
    } else ctx.log("x answered " + r.status);
  }

  const seen = new Set();
  const kept = found.filter(m => {
    if (seen.has(m.id) || !m.text.toLowerCase().includes(needle)) return false;
    seen.add(m.id);
    return true;
  });
  kept.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const mentions = kept.slice(0, limit).map(m => ({ ...m, text: m.text.slice(0, 600) }));
  ctx.charge(mentions.length * 0.002, "per mention");  // the price: $0.002 per mention returned
  return { term, mentions, results: mentions.length, scanned, dropped_as_noise: found.length - kept.length };
}
