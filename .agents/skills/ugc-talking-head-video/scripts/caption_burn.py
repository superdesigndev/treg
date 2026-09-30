#!/usr/bin/env python3
"""Burn a static emoji header + phrase captions (1-4 words, broken on sense) into a 9:16 talking-head clip.

Usage:
  caption_burn.py --video take.mp4 --header "deleting this video|in 24 hours 🤫😬" --out take_captioned.mp4 \
      [--phrases phrases.txt] [--words words.json]

- Word timings come from OpenAI Whisper (OPENAI_API_KEY) unless --words points at a saved verbose_json.
- --phrases: one caption per line, in order; every word of the transcript must be covered (asserted).
  Without it, words are auto-chunked (max 3, break on punctuation / long gaps / width) — worse than hand phrases.
- Header lines are split on "|". Emoji render through Apple Color Emoji.
- Audio is copied untouched, when the take has any: a silent take has no audio stream, so the map
  is conditional. Mapping 0:a on a video-only file fails the whole render.

Lessons baked in:
- ffmpeg between() is inclusive on both ends -> two captions render for one frame at every boundary. We subtract 2 ms.
- Whisper normalises "gonna" -> "going to"; captions follow the transcript, not the script.
- Never chunk by count alone: "you step by" / "lucky but do" read badly. Hand phrases win.
- -filter_complex_script was removed in FFmpeg 9. The graph is passed inline instead: the file-reading
  replacement, -/filter_complex, is absent from `ffmpeg -h full` and can only be found by running ffmpeg.
"""
import argparse, json, os, re, subprocess, sys, tempfile, urllib.request
try:  # only drawing needs Pillow; the caption graph and the ffmpeg command do not
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError:
    Image = ImageDraw = ImageFont = None

W, H = 720, 1280; MAXW = 580; HEADER_Y = 0.12; CAPTION_Y = 0.78
FONT = "/System/Library/Fonts/Helvetica.ttc"; EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"
meas = None

def _require_pillow():
    """Drawing is the only part that needs Pillow, and a measuring canvas is built on first use so
    the module still imports on a machine (or CI) without it."""
    if Image is None:
        sys.exit("caption_burn.py needs Pillow to draw overlays: python3 -m pip install pillow")

def text_img(txt, sz, stroke):
    _require_pillow()
    global meas
    if meas is None: meas = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    f = bold(sz); x0, y0, x1, y1 = meas.textbbox((0, 0), txt, font=f, stroke_width=stroke)
    im = Image.new("RGBA", (x1 - x0 + 2 * stroke + 8, y1 - y0 + 2 * stroke + 8), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((stroke + 4 - x0, stroke + 4 - y0), txt, font=f, fill="white", stroke_width=stroke, stroke_fill=(0, 0, 0, 235))
    return im

def bold(sz):
    _require_pillow()
    for idx in (1, 2, 3, 0):
        try:
            f = ImageFont.truetype(FONT, sz, index=idx)
            if "Bold" in f.getname()[1]: return f
        except Exception: pass
    return ImageFont.truetype(FONT, sz)

def emoji_img(e, sz):
    _require_pillow()
    f = ImageFont.truetype(EMOJI, 160); im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((20, 10), e, font=f, embedded_color=True); im = im.crop(im.getbbox())
    return im.resize((sz, sz), Image.LANCZOS)

EMO = re.compile(r"[\U0001F300-\U0001FAFF☀-➿\U0001F900-\U0001F9FF]")

def header_img(lines, sz=62):
    rows = []
    for line in lines:
        txt = EMO.sub("", line).rstrip(); emojis = EMO.findall(line)
        parts = [text_img(txt, sz, 5)] + [emoji_img(e, sz) for e in emojis]
        w = sum(p.width for p in parts) + 4 * (len(parts) - 1); h = max(p.height for p in parts)
        row = Image.new("RGBA", (w, h), (0, 0, 0, 0)); x = 0
        for p in parts: row.paste(p, (x, (h - p.height) // 2), p); x += p.width + 4
        rows.append(row)
    w = max(r.width for r in rows); h = sum(r.height for r in rows) - 6 * (len(rows) - 1)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0)); y = 0
    for r in rows: out.paste(r, ((w - r.width) // 2, y), r); y += r.height - 6
    return out

def whisper_words(video):
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
    r = subprocess.run(["curl", "-s", "https://api.openai.com/v1/audio/transcriptions", "-H", f"Authorization: Bearer {os.environ['OPENAI_API_KEY']}",
                        "-F", f"file=@{wav}", "-F", "model=whisper-1", "-F", "response_format=verbose_json",
                        "-F", "timestamp_granularities[]=word", "-F", "language=en"], capture_output=True, text=True)
    return json.loads(r.stdout)

norm = lambda s: re.sub(r"[^a-z0-9']", "", s.lower())

def chunk_by_phrases(words, phrases):
    """Match each phrase to the transcript by letters only, so 'full-time' == 'full' + 'time'."""
    i, out = 0, []
    for ph in phrases:
        target, acc, grp = norm(ph), "", []
        while acc != target:
            assert i < len(words) and target.startswith(acc + norm(words[i]["word"])), \
                f"phrase {ph!r} does not match transcript near word {i}: {words[i]['word'] if i < len(words) else 'EOF'!r}"
            acc += norm(words[i]["word"]); grp.append(words[i]); i += 1
        out.append((ph, grp))
    assert i == len(words), f"phrases cover {i} of {len(words)} words; remaining: {' '.join(w['word'] for w in words[i:])}"
    return out

def chunk_auto(words):
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w); txt = " ".join(x["word"] for x in cur)
        gap = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else 9
        nxt = txt + " " + words[i + 1]["word"] if i + 1 < len(words) else None
        if len(cur) == 3 or w["word"].endswith((".", ",")) or gap > 0.3 or (nxt and text_img(nxt, 54, 4).width > MAXW):
            out.append((txt.rstrip(","), cur)); cur = []
    if cur: out.append((" ".join(x["word"] for x in cur), cur))
    return out

def has_audio(path):
    """ffprobe the file for an audio stream. A video-only take is normal here, not an error."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return bool(r.stdout.strip())


def ffmpeg_command(video, overlay_inputs, graph, chunk_count, out, *, audio):
    """The render argv, kept separate from the drawing so it is checkable without ffmpeg.

    Two constraints are load-bearing here and both regressed once:
    - the graph is passed INLINE via -filter_complex. -filter_complex_script was removed in FFmpeg 9
      and its replacement (-/filter_complex) does not appear in `ffmpeg -h full`.
    - audio is mapped only when the take actually has a stream. A silent take has none, and mapping
      0:a anyway aborts the whole render.
    """
    audio_args = ["-map", "0:a", "-c:a", "copy"] if audio else []
    return ["ffmpeg", "-v", "error", "-y", "-i", video, *overlay_inputs, "-filter_complex", graph,
            "-map", f"[v{chunk_count}]", *audio_args, "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True); ap.add_argument("--header", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--phrases"); ap.add_argument("--words")
    a = ap.parse_args()
    data = json.load(open(a.words)) if a.words else whisper_words(a.video)
    words = data["words"]; json.dump(data, open(a.out + ".words.json", "w"))
    print("[captions] transcript:", data.get("text"), file=sys.stderr)
    chunks = chunk_by_phrases(words, [l.strip() for l in open(a.phrases) if l.strip()]) if a.phrases else chunk_auto(words)
    tmp = tempfile.mkdtemp(); header_img(a.header.split("|")).save(f"{tmp}/header.png")
    inputs = ["-i", f"{tmp}/header.png"]; parts = [f"[0:v][1:v]overlay=(W-w)/2:{int(H*HEADER_Y)}[v0]"]
    for k, (ph, c) in enumerate(chunks):
        sz = 54; im = text_img(ph, sz, 4)
        while im.width > MAXW and sz > 40: sz -= 2; im = text_img(ph, sz, 4)
        p = f"{tmp}/c{k:03d}.png"; im.save(p); inputs += ["-i", p]
        s = c[0]["start"]; e = (chunks[k + 1][1][0]["start"] if k + 1 < len(chunks) else c[-1]["end"] + 0.3) - 0.002
        parts.append(f"[v{k}][{k+2}:v]overlay=(W-w)/2:{int(H*CAPTION_Y)}-h/2:enable='between(t,{s:.3f},{max(e, s+0.15):.3f})'[v{k+1}]")
    subprocess.run(ffmpeg_command(a.video, inputs, ";".join(parts), len(chunks), a.out,
                                  audio=has_audio(a.video)), check=True)
    print(f"[captions] {len(chunks)} chunks -> {a.out}", file=sys.stderr)
    print("\n".join(ph for ph, _ in chunks))

if __name__ == "__main__": main()
