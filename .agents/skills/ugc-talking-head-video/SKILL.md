---
name: ugc-talking-head-video
description: "Turn a locked character image (from portrait-clone) into a UGC-style 9:16 talking-head video: pick a voice-reference clip, size the script to the duration, write the Seedance 2.5 prompt, generate through treg (reAPI or PiAPI), then burn a static emoji header and phrase captions with code. Use after portrait-clone when the user wants the character to speak a script on camera."
---

# UGC talking-head video

Input: one character image (the frame you and the user picked from `portrait-clone`) and a script.
Output: `take_N.mp4` (raw) and `take_N_captioned.mp4`, plus the request JSON that made it.

Model: **Seedance 2.5 through treg**, reAPI route by default, PiAPI as the alternative (`scripts/seedance_treg.py`). No vendor key on this machine. Captions: **ffmpeg + PIL** (`scripts/caption_burn.py`).
Both scripts are self-contained; read their docstrings before changing them.

## Prerequisites

- **treg.** Every generation call, reference upload and status poll goes through `treg call`; the vendor
  key is injected server-side and the charge lands on the team's prepaid treg balance. If `treg --version`
  fails, install and sign in first:
  ```bash
  curl -fsSL https://treg.to/install.sh | sh
  treg login
  ```
  Then `treg balance` to confirm there is credit for the run (a take costs a few dollars, § Run).
- **A character image.** If the user has no locked character yet, generate one first with the
  `portrait-clone` skill (sibling folder `.agents/skills/portrait-clone/`, or the published copy at
  https://raw.githubusercontent.com/agentara/skills/refs/heads/main/skills/aigc/portrait-clone/SKILL.md).
  Read it, follow it, then come back here with the chosen frame.
- `ffmpeg`, Python 3 with PIL, and `OPENAI_API_KEY` in the environment for Whisper word timings (captions only).

## Flow

1. Lock the script and duration (§ Script and duration). Show the user the word count and the proposed seconds before anything else.
2. Get a voice-reference clip (§ Voice reference). Never generate without one.
3. Write the prompt from the template (§ Prompt). Paste the whole thing to the user before the first run.
4. Run `seedance_treg.py`. Quote the estimated cost in the same message you announce the run.
5. Verify with the checks in § Verify, send the raw clip, and only then caption.
6. Caption with `caption_burn.py` using hand-written phrases.

## Script and duration

The model fills every second it is given. Too long and the delivery goes flat because there is
nowhere to breathe; too short and words get dropped. Size the duration from the script, not the
other way round.

| Words | Duration | Feel |
|---|---|---|
| 45-55 | 12-14 s | punchy hook |
| 60-70 | 16-17 s | standard UGC talking head |
| 70-80 | 18-19 s | dense; 18 s reads noticeably quicker than 19 s |
| 90+ | split into two clips | one Seedance take over 20 s of continuous speech drifts |

Rule of thumb: **words / 4 = seconds**, then subtract 1 s. Our 74-word script felt slow at 19 s
and right at 18 s. Confirm the script verbatim with the user first, including slang ("gonna",
"brain cell"): the captions later follow the *transcript*, and Whisper normalises "gonna" to
"going to", so warn them the on-screen text may differ from the script spelling.

Join sentences with commas, not periods, if you do not want a hold at the sentence turn. The
model treats a period as permission to stop for half a second.

## Voice reference

Cadence comes from the audio reference, not from the prompt. We proved this: the same script and
near-identical prompts gave a flat delivery without a reference and a natural one with it, while a
prompt full of beat-by-beat pacing directions moved the pace by a few percent at most and
introduced whispering and long dead pauses the user hated.

1. Ask the user for a video whose delivery they like. If they have none, find candidates through
   treg and let them choose. Use the routed endpoints and say what you want, not a vendor:
   `catalog_search("instagram reels search by keyword")` -> `treg.instagram.search.reels`
   (measured 86% success), or `catalog_search("tiktok search videos")` / `treg.douyin.search.videos`.
   Search the niche ("money tips talking head", "creator secret"), pick 3-5 results with a
   play URL, send the links, and ask which voice and pace they want. Do not pick for them.
2. Extract a **2-15 s** slice, mono mp3. PiAPI rejects longer references (reAPI allows up to 30 s). Choose the most
   animated stretch, not the opening line: run Whisper with segment timestamps, read the
   segments, pick a run with a rise and fall in it.
   `ffmpeg -ss <start> -to <end> -i src.mp4 -vn -ac 1 -ar 44100 -b:a 128k voice_ref.mp3`
3. Keep the clip and its source timestamps next to the outputs; the user will ask where it came from.

## Prompt

Under 4000 characters. Five parts, in this order. Fill the braces; keep the rest verbatim, it is tuned.

```
The woman in @image1 talks directly to the camera in a vertical smartphone selfie video shot from a
phone on a fixed tripod: same room, same {top}, same {prop} held in her right hand just below her
chin, same soft bright window daylight.
She speaks with the exact voice, timbre, energy and speaking rhythm of @audio1: upbeat, quick,
conversational creator delivery with natural rise and fall, no whispering, no dramatic pauses. She
keeps talking straight through the sentence breaks without stopping, only quick breaths, so the
whole line flows as one continuous take.
She says, in English, lips precisely synced to every word: "{script}"
Her free left hand gestures outward toward the camera or rests near the {prop}; she never points at
herself, never touches her face or lips. Natural head movement, eye contact with the lens
throughout.
Camera locked off, no handheld sway, no cuts, no zoom, no captions, no on-screen text, no music,
only her voice and quiet room tone.
```

Why each line is there:

- `@image1` / `@audio1` are the reference placeholders on both routes; a placeholder for media you did not attach is a 400.
- The tone sentence is guardrails only. Do **not** add beat-by-beat stage directions, "(pause)",
  "whispers", "slowly", or split the script into directed lines. That is what produced the take the
  user rejected. Cadence is the audio reference's job.
- "keeps talking straight through the sentence breaks" fixed a 0.74 s hold at a sentence turn.
- The hand line exists because the model pointed at her own face on "if you see this video" and
  touched her lips at the end when the prompt said only "small hand gestures". Say what the hand
  does, not that it gestures.
- "Camera locked off, no handheld sway": vlog cameras do not move; people do. Our takes had a
  slight sway because the prompt asked for it. Ask for a locked camera by default.
- Everything after the script is a negative list phrased positively. Neither route has a negative prompt field.

## Run

```bash
python3 .claude/skills/ugc-talking-head-video/scripts/seedance_treg.py \
  --image char.jpg --audio voice_ref.mp3 --prompt-file prompt.txt --duration 18 --out take_1.mp4
```

- Everything goes through `treg call`; the charge lands on the team's treg balance and the vendor
  key never touches this machine. `treg catalog get reapi.video-gen.seedance-2-5.unrestricted`
  shows the live price table.
- Cost at 720p: reAPI $0.267/s on both tiers (18 s = $4.80); PiAPI $0.35/s standard, $0.385/s
  less-restriction (18 s = $6.30 / $6.93). Say the number before running. `--provider piapi` only
  when reAPI is down; reAPI accepted the same face image and voice clip and is 24% cheaper.
- `--tier relaxed` (default) is the route that accepts a real person's face and a voice clip. If a
  standard-tier run fails with a moderation error ("quit your job" scripts do), rerun relaxed. A
  failed task is never charged.
- References are hosted with `treg host <file>` (public URL, 7-day TTL, byte-exact). The script does
  it for you and falls back to `host_file.py` only on a registry too old to have `treg host`. Never
  hand a vendor a paste-host link: catbox was unreachable from reAPI's probe, tmpfiles served HTML on
  its download link, uguu timed out, and a `replicate.delivery` URL 404'd for PiAPI. Five dead
  submissions across two builds.
- Poll with `treg call reapi.tasks.get --query id=<task>` (or `piapi.task.get --query task_id=`).
  A catalog id takes its parameters in `--query` or `--data`, never as a URL path.
- There is no seed. Every run is a fresh roll for gestures and expression. Fix one thing per rerun
  and tell the user a rerun can regress something else.
- 1080p costs 1.7x on reAPI (PiAPI 2.3x) and the source reference is 720p anyway. Stay on 720p unless asked.

## Verify before sending

Read numbers and frames; you cannot hear the audio, so say so.

```bash
ffmpeg -i take.mp4 -vn -ac 1 -ar 16000 a.wav   # then Whisper verbose_json with word timestamps
```

- Speech span should end within ~0.3 s of the duration. Ending 1 s early means it rushed.
- List gaps >= 0.25 s. Zero or one at a natural breath is good. A 0.7 s gap is a hold the user will notice.
- Words per minute: 230-250 is the UGC sweet spot for English.
- Check the transcript for doubled words ("job infinitely and make infinitely") -> a stutter, rerun.
- Frame strip: `ffmpeg -i take.mp4 -vf "select='not(mod(n\,50))',scale=220:-1,tile=9x1" -frames:v 1 strip.jpg`
  and look at hands (self-point, face touch), prop drift, identity drift.
- Send the raw clip and tell the user what you could not verify (voice likeness, lip-sync accuracy).

## Captions and header

```bash
python3 .claude/skills/ugc-talking-head-video/scripts/caption_burn.py \
  --video take_1.mp4 --header "deleting this video|in 24 hours 🤫😬" \
  --phrases phrases.txt --out take_1_captioned.mp4
```

- Style: white Helvetica Bold with a dark outline, header at 12% height, captions at 78%, audio untouched.
- Write `phrases.txt` by hand, one caption per line, 1-4 words, broken on sense
  ("step by step", "full-time job", "and your brain cell"). The script asserts the phrases cover
  the transcript exactly, so write them against the transcript it prints, not the original script.
  Auto-chunking (no `--phrases`) breaks mid-phrase ("you step by", "lucky but do"); use it only for a draft.
- Each caption shows from its first word's start to the next caption's start. The 2 ms trim is
  deliberate: ffmpeg's `between()` is inclusive at both ends and doubled captions for one frame.
- Header emoji render via Apple Color Emoji through PIL with `embedded_color=True`; ffmpeg's
  drawtext cannot draw colour emoji, which is why header and captions are PNG overlays.
- Reuse the saved `--words` JSON when re-rendering captions so you do not pay Whisper twice.
- Whisper hears "Claude Code" as "cloud code" every time. Fix the word in the saved words JSON
  before burning (the phrase matcher compares letters, so the phrase must then say "Claude").

## Mistakes from the first build, so you do not repeat them

1. Wrote beat-by-beat pacing directions into the prompt (whispers, holds, "slowly"). Result: long
   dead pauses and a whisper the user hated, and pace moved only a few percent. Audio reference instead.
2. Left the sentence period in, got a 0.74 s hold. Comma plus "keeps talking through the breaks".
3. Said "small hand gestures", got a self-point and a lip touch. Name the gesture and forbid the rest.
4. Asked for "slight handheld sway". Vlog cameras are static; the user noticed. Lock the camera.
5. Chose 19 s for 74 words. 18 s read better. Words / 4 minus 1.
6. Chunked captions by word count; they broke mid-phrase. Hand phrases.
7. Captions doubled for one frame at every boundary; inclusive `between()`. Trim 2 ms.
8. Trusted a `replicate.delivery` URL that PiAPI could not fetch, then a catbox upload that
   was truncated and hash-deduplicated. Two dead submissions. Always byte-verify the host.
9. Reported "done" on video I could only inspect by frames and timestamps. Say what was not heard.
10. Chose the reference audio slice by reading Whisper segments, which worked, but forgot the 15 s
    cap once and had to recut. The script now asserts it.
11. Hosted references on paste hosts. reAPI's probe rejected catbox, tmpfiles and uguu in turn; four
    dead submissions before one landed. `treg host` now, host_file.py only as a fallback.
12. Polled with `treg call reapi.tasks.get tasks/<id>` and got "no tool in this org": a catalog id
    takes `--query id=<id>`, never a path. The registry now says so in the error.
13. Held a PiAPI key in `.env` and called the vendor directly, which is the credential-holding
    pattern treg exists to remove. The runner now goes through `treg call` only.
