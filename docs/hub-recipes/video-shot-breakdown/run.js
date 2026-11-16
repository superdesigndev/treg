// A shot list from one model pass: the post resolves to its mp4, the model watches it at 5 frames
// a second and high resolution (cuts within about 0.1 s, settled values of animated numbers), and
// every shot's keyframe is a Cloudflare frame of the same mp4 at the moment the model chose.
const MAX_SECONDS = 120;
const FRAMES = "https://media.treg.to/cdn-cgi/media/mode=frame,time=";
const GEMINI = "google-ai.video-understand.gemini-3-8-flash";

const SHOT = {
  type: "object",
  properties: {
    start: { type: "string", description: "MM:SS.sss" },
    end: { type: "string", description: "MM:SS.sss" },
    keyframe: { type: "string", description: "MM:SS.sss inside the shot, after its animations settle" },
    shot_size: { type: "string" },
    motion: { type: "string", description: "camera and on-screen motion" },
    visual: { type: "string" },
    on_screen_text: { type: "string", description: "verbatim" },
    audio: { type: "string", description: "voice, music, sound effects" },
  },
  required: ["start", "end", "keyframe", "shot_size", "motion", "visual", "on_screen_text", "audio"],
};
const SCHEMA = {
  type: "object",
  properties: {
    shots: { type: "array", items: SHOT },
    analysis: {
      type: "object",
      properties: {
        hook: { type: "string" }, structure: { type: "string" }, pacing: { type: "string" },
        strengths: { type: "string" }, weaknesses: { type: "string" },
        suggestions: { type: "array", items: { type: "string" } },
      },
      required: ["hook", "structure", "pacing", "strengths", "weaknesses", "suggestions"],
    },
  },
  required: ["shots", "analysis"],
};

function prompt(language) {
  return [
    "Break this video down shot by shot, the way an editor does when studying it.",
    "A shot boundary is any point where the frame changes to a new layout or scene, including fades",
    "and transitions on the same background. Give start and end as MM:SS.sss. For each shot, pick a",
    "keyframe moment inside it after its animations settle. Numbers in a video often animate (count",
    "up, spin); always report the FINAL settled value, never an intermediate one. Copy on-screen text",
    "verbatim. Then analyse the whole video: the hook in its first seconds, its structure, its pacing",
    "(average shot length, where it speeds up or holds), what works, what does not, and concrete",
    `suggestions. Write descriptions and analysis in the language with code "${language}".`,
  ].join(" ");
}

function postId(url) {
  const m = /^https?:\/\/(?:www\.|mobile\.)?(?:x|twitter)\.com\/[^/]+\/status(?:es)?\/(\d+)/i.exec(String(url).trim());
  if (!m) throw new Error("url must be an x.com or twitter.com post, e.g. https://x.com/<user>/status/<id>");
  return m[1];
}

function seconds(tc) {
  const parts = String(tc).trim().split(":").map(Number);
  if (!parts.length || parts.some((p) => !Number.isFinite(p))) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

function round(n) { return Math.round(n * 100) / 100; }

function mp4s(variants) {
  return (variants || [])
    .filter((v) => v && typeof v.url === "string" && String(v.content_type).includes("mp4"))
    .map((v) => ({ url: v.url, bitrate: Number(v.bitrate) || 0, size: (/\/(\d+)x(\d+)\//.exec(v.url) || []).slice(1).map(Number) }))
    .sort((a, b) => a.bitrate - b.bitrate);
}

async function watch(ctx, url, language) {
  return ctx.call(GEMINI, {
    method: "POST",
    query: { model: "gemini-3.8-flash" },
    body: {
      contents: [{ parts: [
        { fileData: { mimeType: "video/mp4", fileUri: url }, videoMetadata: { fps: 5 } },
        { text: prompt(language) },
      ] }],
      // Low thinking: the same cuts and values as the default, about a third faster on a dense video.
      generationConfig: { responseMimeType: "application/json", responseJsonSchema: SCHEMA,
                          mediaResolution: "MEDIA_RESOLUTION_HIGH", thinkingConfig: { thinkingLevel: "low" } },
    },
  });
}

function answer(r) {
  const parts = (((r.json || {}).candidates || [])[0] || {}).content;
  const text = ((parts && parts.parts) || []).filter((p) => !p.thought).map((p) => p.text || "").join("");
  try { return JSON.parse(text); } catch (e) { return null; }
}

export default async function run(ctx) {
  const id = postId(ctx.inputs.url);
  const language = String(ctx.inputs.language || "en").slice(0, 16);

  const post = await ctx.call("tikhub.x.twitter-web-fetch-tweet-detail", { query: { tweet_id: id } });
  const data = post.status === 200 && post.json ? post.json.data : null;
  if (!data) throw new Error(`could not read post ${id} (status ${post.status})`);
  const video = ((data.media || {}).video || [])[0];
  if (!video) throw new Error("the post has no video");
  const duration = (Number(video.duration) || 0) / 1000;
  if (duration > MAX_SECONDS) throw new Error(`the video is ${Math.round(duration)} s; this tool reads up to ${MAX_SECONDS} s`);
  const files = mp4s(video.variants);
  // 720p reads small on-screen text; the 15 MB fetch limit can refuse it on a dense video, so the
  // next size down is the second try.
  const preferred = files.filter((f) => f.size.length && Math.min(...f.size) <= 720).reverse();
  if (!preferred.length) throw new Error("the post's video has no mp4 file");
  ctx.log(`post ${id}: ${round(duration)} s, ${preferred.length} mp4 sizes`);

  let chosen = null, model = null;
  for (const file of preferred.slice(0, 2)) {
    const r = await watch(ctx, file.url, language);
    ctx.log(`gemini on ${file.size.join("x")}: ${r.status}`);
    if (r.status === 200) { chosen = file; model = answer(r); break; }
    if (r.status !== 400) break;
  }
  if (!chosen) throw new Error("the model could not read the video");
  if (!model || !Array.isArray(model.shots)) throw new Error("the model's answer was not a shot list");

  const source = chosen.url.split("?")[0];
  const shots = model.shots.map((s, i) => {
    const start = seconds(s.start), end = seconds(s.end);
    let key = seconds(s.keyframe);
    if (key === null || start === null || end === null || key < start || key > end) {
      key = end !== null ? Math.max(start || 0, end - 0.3) : start;
    }
    const at = Math.min(Math.max(0, key || 0), Math.max(0, duration - 0.05));
    return {
      n: i + 1,
      start: start === null ? null : round(start),
      end: end === null ? null : round(end),
      duration: start === null || end === null ? null : round(end - start),
      keyframe_at: round(at),
      keyframe_url: `${FRAMES}${round(at)}s,width=640,format=jpg/${source}`,
      shot_size: s.shot_size, motion: s.motion, visual: s.visual,
      on_screen_text: s.on_screen_text, audio: s.audio,
    };
  });

  return {
    post: {
      url: `https://x.com/${(data.author || {}).screen_name || "i"}/status/${id}`,
      author: (data.author || {}).screen_name || null,
      text: data.text || "",
      created_at: data.created_at || null,
      views: Number(data.views) || null, likes: data.likes ?? null, reposts: data.retweets ?? null,
      quotes: data.quotes ?? null, replies: data.replies ?? null, bookmarks: data.bookmarks ?? null,
    },
    video: { url: chosen.url, duration_s: round(duration), width: chosen.size[0] || null, height: chosen.size[1] || null },
    shots,
    analysis: model.analysis || null,
  };
}
