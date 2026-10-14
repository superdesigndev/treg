---
name: make-ugc
description: Make AI UGC videos end to end through treg. Pull the trending TikTok and Instagram videos in a vertical, extract the hook patterns, create a character with the same vibe as a presenter the user picks, generate 5-10 talking-head hook clips on Seedance 2.5 (less-restriction route), add a voiced demo clip, burn captions, and report the bill per clip. Use when the user asks for UGC ads, creator-style product videos, TikTok/Reels hooks, or an AI presenter for their product.
---

# Make UGC videos

One loop, five steps, every model call through treg. The recipe behind {BASE}/ugc.

Input: the product (one line), the vertical (a few keywords), and optionally reference accounts, a
phone recording of the product, a voice-reference clip, or a character image the user already likes.
Output: a folder with `hooks.md`, the character image and its JSON prompt, one captioned clip per
hook, an optional voiced demo clip, and `bill.md` with what each step cost.

## Prerequisites

- **treg.** Every catalog call, upload and poll goes through treg; the vendor keys stay server-side
  and the charge lands on the team's prepaid balance. If `treg --version` fails:
  ```bash
  curl -fsSL {BASE}/install.sh | sh
  treg login
  ```
  `treg balance` before starting. A full run of 4 hook clips lands in the low single-digit dollars
  at 720p; say the estimate before each paid step.
- **Two sibling skills** this one delegates to. Read each before its step, do not paraphrase them:
  - `portrait-clone` (the character): https://raw.githubusercontent.com/agentara/skills/refs/heads/main/skills/aigc/portrait-clone/SKILL.md
  - `ugc-talking-head-video` (the clips and captions): https://raw.githubusercontent.com/superdesigndev/treg/main/.agents/skills/ugc-talking-head-video/SKILL.md
    with its `scripts/seedance_treg.py` and `scripts/caption_burn.py` beside it.
- `ffmpeg`, Python 3 with PIL. `OPENAI_API_KEY` only if you burn captions (Whisper word timings).

## Flow

Stop at the three marked points and let the user choose. Do not pick for them.

### 1. Find what's trending

Search by task, not vendor. `treg catalog search "tiktok search videos"` and
`treg catalog search "instagram reels search by keyword"` return the routed endpoints; call them
with the vertical's keywords, sorted by likes, last 30 days, and collect 50-150 videos. Pull
transcripts for the 10-15 most relevant (`treg catalog search "tiktok video transcript"`). If the
user names competitor brands, add their ads from the Meta ad library (`treg catalog search "meta ad
library"`).

Write `hooks.md`: one row per video with views, the first spoken line (0-3 s), when the product is
first named, and who is on camera. Then name the dominant pattern in one line. In agent and B2B
software niches it is usually: a stunt or claim, a specific number, the result, then "here's how";
the tool appears late, as the answer. Tell the user what the data said and what it cost.

**Stop 1.** Show 5-8 videos as candidates for the *presenter vibe* and for the *voice*. The user
picks one of each (they can be the same video).

### 2. Create the character

If the user already has a character image, skip to step 3.

Grab a clean frame of the chosen presenter (`ffmpeg -ss <t> -i src.mp4 -frames:v 1 ref.jpg`) and
run `portrait-clone` on it. It produces a locked JSON prompt: every default the image model would
otherwise fill in is pinned, which is what stops the doll eyes and the HDR sheen. Generate the same
JSON on at least two models through treg and let the user compare:

```bash
treg call reapi.image-gen.gemini-3-pro-image --data '{"prompt": "<json>", "size": "9:16", "resolution": "2K"}'
treg call reapi.image-gen.gpt-image-2-5    --data '{"model": "gpt-image-2.5-flare", "prompt": "<json>", "size": "1024x1536"}'
```

Gemini 3 Pro Image has been the most realistic (phone-camera softness, real pores, imperfect
teeth); GPT Image 2.5 keeps a doll pattern in the eyes. Say which is which but show both.

**Stop 2.** The user picks the character frame. Save it as `character.jpg` with its JSON.

### 3. Generate the hook clips

Draft 10 hooks in the pattern from step 1, each 45-75 words so it fits a 12-18 s take (the
talking-head skill's words/4 minus 1 rule). Put them in `hooks.md` under the table.

**Stop 3.** The user picks 3-5.

Cut the voice reference from the video chosen at stop 1: a 2-15 s animated stretch, mono mp3, as
`ugc-talking-head-video` § Voice reference describes. Then run that skill once per hook with
`character.jpg`, the voice clip and the script. Its runner uses the Seedance 2.5 less-restriction
route (`reapi.video-gen.seedance-2-5.unrestricted`), which accepts a realistic face and a voice clip
as references; the default route refuses them. Quote the per-second price from
`treg catalog get reapi.video-gen.seedance-2-5.unrestricted` before the first run, stay on 720p,
verify each take with the skill's checks, then caption with `caption_burn.py`. Failed tasks are
refunded, so a moderation error costs nothing but time.

### 4. The demo clip (optional)

The character does not need to be in the demo. If the user recorded the product on their phone,
add a voiceover and captions:

- **With a cloned voice**, if the user has a Fish Audio (or other TTS) account: clone from the same
  voice-reference clip, read the demo script, and tell them it runs on their key, not treg.
- **Without one**, skip the clone: the demo plays under the hook clip with captions only, or with
  the hook clip's own audio continuing. Say which you did.

Stitch demo and captions with ffmpeg and the skill's `caption_burn.py`, keeping 9:16 and 720p.

### 5. Put it together

For each picked hook: hook clip, then the demo clip if there is one, concatenated with ffmpeg
(`-c copy` when the encodes match, re-encode otherwise). Name the files by hook. Write `bill.md`
from `treg calls` (or the prices you quoted): the trend pull, the character runs, each clip, and
the total divided by the number of finished clips. Hand over the folder and say what you could not
verify yourself: voice likeness and lip-sync are judged by ear and eye, not by the transcript checks.

## Rules

- Every generation and reference upload goes through `treg call` and `treg host`. Never hand a
  vendor a paste-host link, and never hold a vendor key for this loop.
- State the price before each paid step. When you call an endpoint directly rather than through
  the runner, send treg's max-cost header so one task cannot exceed the price you quoted.
- Rerun one thing at a time; there is no seed, and a rerun can regress something else.
- The three stops are the product. A run that skips them makes clips nobody asked for.
