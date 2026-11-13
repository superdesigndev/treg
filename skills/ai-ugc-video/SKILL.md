---
name: "ai-ugc-video"
description: "Make realistic AI UGC videos cheaply: viral research, locked assets, scripts, Seedance/Avatar IV generation, and final edit. Use for AI UGC ads, talking-head videos, or AIGC short films."
---

# AI UGC video production

Six stages, run in order. Each stage ends with the user approving something; nothing moves to a more expensive stage until the cheap one passes.

**First, work out which stage we're in** and say so in one line. If the user brings assets (a repo, a folder, an earlier video, a reference post), read them before asking anything: never ask for what is already there, and never regenerate what exists.

| Stage | Run it when |
| --- | --- |
| 1 Research | No reference post decoded yet, or the user names a niche or reference accounts |
| 2 Assets | Reference decoded, but no locked characters, wardrobe, locations or product |
| 3 Voices | Characters locked, no approved voice references |
| 4 Shoot plan | Assets and voices ready, no approved shoot plan |
| 5 Video | Shoot plan approved |
| 6 Finish | Clips approved; dubbing, captions, assembly |

## Rules for every stage

**Cost.** Four tiers, each about 10x the one before: text (fractions of a cent) → stills (~$0.03) → 480p drafts (~$0.12/s) → final (720p ~$0.27/s, 1080p ~$0.46/s). Settle every decision at the cheapest tier that can settle it. Quote the cost before any paid call and wait for a go-ahead. Budget 3 generations per usable shot and say so up front. Video is refused on free treg credit; check the paid balance first. A refused or failed job is not charged; a finished take that misses is.

**One document: the shoot plan.** Everything the model receives lives in one plan (stage 4). Opinions, history, risks and costs of past drafts go in a separate production log, never in the plan. A sentence the model will not read does not belong in the plan.

**Realism.** Camera description goes first in every prompt. Every reference gets an explicit job: what it controls and what it must not supply. Name devices, Kelvin values and visible light sources; never write "cinematic", "8K", "masterpiece" or "professional lighting". Ask for flaws: handheld tremor, focus hunting, sensor noise, uneven exposure, visible pores. Paste locked descriptions word for word; anything rewritten from memory drifts between shots.

**Ethics.** Remix the structure of a viral post, never the people in it. No real creator's face, voice or footage goes into a generation; no celebrity-imitation voices.

**You cannot hear.** Say so. Judge video from a timestamped 2 fps frame contact sheet and list what frames cannot show (bounce, lip sync, voice, pronunciation). Hand every audio check to the user before spending on the next tier.

## Tools (all via treg; `catalog_get` for live prices)

| Job | Endpoint | Notes |
| --- | --- | --- |
| Reference post stats, transcript, video file | `scrapecreators.tiktok.video.detail` | ~$0.002; download the video from `play_addr` for frame grabs |
| Trending posts, comments | `tikhub.tiktok.search.videos`, `tikhub.tiktok.video.comments` | only when the user has no reference |
| Stills | `reapi.image-gen.gemini-3-pro-image` | 2K, up to 14 reference images |
| Voice clone + TTS | `fishaudio.voices.create` (free; multipart `voices` upload) then `fishaudio.tts.s2-1-pro` with `reference_id` | fractions of a cent per line |
| Video with sound from stills | `reapi.video-gen.seedance-2-5.unrestricted` | `image_with_roles` (first_frame + reference_image) + `audio_urls`; no video input |
| Video from a video (remake/edit) | `piapi.video-gen.seedance-2-5.less-restriction` | `video_urls` + `audio_urls`; duration in 5 s steps; billed input + output (~1.4x reAPI) |
| Lip sync from a still | `openrouter.x.heygen-avatar-iv` | $0.05/s; locked camera |
| Silent video | `minimax.video-gen.from_image` (Hailuo 2.3) | no 480p; draft at 768P |

Seedance input rules: every reference image between 0.4:1 and 2.5:1 (pad wide cast sheets); every audio file 2–30 s; PiAPI allows 15 s of audio references in total; hosted files must return raw bytes (a GitHub raw URL works); first_frame requires `size: adaptive`.

---

# Stage 1 · Research and decode

If the user brings one reference post, decode it and skip the trend pull: fetch stats and transcript, download the video, grab frames at 2 fps, and write:
- `[HOOK]` first 3 s: what is on screen, words if any
- `[BEATS]` one line per beat: time range → one action (this is the structure we copy)
- `[CAMERA]` selfie / handheld / tripod / walking / phone or camera, lens feel, focus behaviour
- `[LIGHT]` source, direction, quality, time of day
- `[AUDIO]` talking, music, ambience; who speaks
- `[LENGTH]` and aspect ratio
- `[REPRODUCIBLE?]` what we rebuild, what we drop (real faces, real places, real footage)

If the user has no reference: pull ~30 trending posts in the niche from the last 30–60 days, show the 15 strongest (link · date · views · first 3 s · hook type · length), decode the user's 3–5 picks as above, add `[WHY IT WORKS]` from the comments, and check each pattern repeats across 3+ accounts.

# Stage 2 · Assets

Lock characters, wardrobe, locations and product on image credits.

If the user has a repo of cast sheets and plates, map them to the roles in the decoded reference and confirm the mapping in one table (reference person → cast file → role). Do not regenerate what exists. Pad cast sheets wider than 2.5:1 before they go into video calls.

For new assets, one question at a time, and for every uploaded reference ask what it controls (identity, outfit, space, product shape, or only camera, light and texture):

**Face portraits.** CTLT prompt, camera first: `Camera:` device, lens, framing, height, horizon, "candid, unposed". `Tone:` who, doing what, where. `Lighting:` quality, direction, Kelvin, motivated by a visible source; one dominant source; ordinary UGC light. `Texture:` visible pores, fine freckles, creases at the eye corners, slight asymmetry, T-zone sheen, unretouched skin, sensor noise. Avoid: plastic skin, poreless faces, perfect symmetry, flat light, glossy finish, stock smiles, readable text. Reject waxy or mirrored faces; the video model copies them.

**Character sheets.** From the locked face: 16:9, neutral grey studio background, top row four full-body views, bottom row four close-ups, plain outfit, same person in every panel. Reject sheets whose panels disagree.

**Wardrobe.** One sheet per look, item by item with a name.

**Locations.** Per location a slugline, a locked description naming concrete objects and where they sit, a locked lighting sentence. Generate 4 plates, empty of people, from the angles the script needs. Reject readable text or impossible geometry.

**Product.** Packshot plus in-hand shot; check label text letter by letter.

Outputs: per character face + sheet; per look sheet + item list; per location plate + slugline + description + lighting sentence; per product packshot + in-hand + exact label text.

# Stage 3 · Voices

One reference file per speaking character, never a combined dialogue track (the model cannot tell whose voice is whose).

1. Ask the user for a voice sample per character, or shortlist Fish Audio library voices. Clone a sample into a private Fish voice (free).
2. Build each character's reference, 5–12 s: the user's sample first, then a short cloned clip that says every hard word in the script: brand names, prices, numbers. The video model pronounces a word correctly only when it hears it in a reference; a phonetic spelling in the prompt alone does not work.
3. Send the references to the user to approve by ear before any video call.
4. Record in the shoot plan: character · file · length · what it contains.

Expect drift: Seedance treats a voice reference as a guide, not a clone. Keep each character's clean script lines (cloned TTS) as a dubbing fallback.

# Stage 4 · Shoot plan

**Interview first**, one question at a time, only about what the reference and assets do not already answer. Offer 2–4 concrete options each time:
- **Script:** the lines, who says each, the one number. Write numbers out so they cannot be misread ("nine dollars forty-five", not "9:45" or "nine forty-five").
- **Camera:** handheld by a person (selfie, walking backward, dancing along, passed between friends) or mechanical (tripod, gimbal, slider)? Phone or named camera and lens? Minimum distance to a face (closer than about 0.7 m stretches faces)?
- **Aspect, length, resolution** for the platform. Keep the repo's aspect unless the user says otherwise.
- **Movement while speaking:** does a speaker stand still, move naturally, or keep dancing? Do listeners keep moving?
- **Background people:** how many; how big their moves are (still / gentle / semi / dramatic); the style by name (e.g. current TikTok-trend moves, not retro); where they look (the leads, each other, never the lens); whether they drift closer.
- **Ending:** how the take ends (palm covers the lens to black, a tap, a swipe, a walk-off, a freeze) and the second it starts.
- **Props and furniture:** anything in the assets that should NOT appear.

**Then write the shoot plan** as one document with exactly these sections and nothing else:
1. **Script** table: speaker · line · start time.
2. **Timeline** table: # · time · what happens · camera. Every second covered, including the last one; the provider may render longer than the plan, so describe the tail.
3. **Inputs** table: slot (@image1…, @audio1…) · file · length · what it controls · what it must not supply.
4. **Settings:** model and route, duration, aspect, resolution, cost at 480p / 720p / 1080p.
5. **Prompt**, in this block order:
   - 【Generation Goal】 one sentence; clean frame (no REC, timestamp, borders, subtitles, text)
   - 【Reference Asset Roles】 one line per input: what it controls, what it must not supply
   - 【Subjects】 exact head count; each person by visible features (hair, outfit), never by role name alone; who stands where; whose outfit belongs to whom
   - 【Location】 the plate as concrete objects; what is absent (no furniture)
   - 【Event Script】 time range → action paired with the spoken line; every second covered
   - 【Camera and Light】 device, lens, how it is held and by whom, the moves at their times, light source and Kelvin
   - 【Performance】 one rule per person or group: what they do while speaking, while silent, where they look; small mouths; irregular blinking; visible breathing
   - 【Sound】 which voice file for which lines and at what times; hard-word pronunciations; ambience; no music
   - 【Maintain Consistency】 same faces, outfits, street; one unbroken take; clean frame
   - 【Avoid】 one list; add an item for every change the user asked for, phrased as the thing to avoid ("the blonde woman swaying while she speaks")
6. **After generation:** trims, dubs, fades to do in the edit.

Send the plan for review before generating. Apply each user change to the plan, say in two lines what changed, and re-send the file. Never improvise a change at call time.

**Camera moves to offer:** handheld tremor · walking backward · camera handover · crash zoom · whip pan · frame block (someone crosses the lens, autofocus hunts) · exposure shift · focus fail · palm over the lens to black.

**Remaking an earlier take at higher resolution:** give the take as `@video1` on the PiAPI route, mark every timeline row "as @video1" or "CHANGE", and expect small drift in faces and hands. A remake is a new generation, not an upscale; a fixed seed is not available on this route.

# Stage 5 · Video

1. **First frame still** (~$0.03): cast sheet(s) + plate, camera-first prompt, the opening pose the plan calls for (a calm closed-mouth frame unless the plan wants a laugh; the model carries the first frame's energy forward). Check head count, faces, outfits, street, no text. Middle keyframes are noise: the model cannot pin an image to a second. Use a last frame only when the ending must land exactly and you accept stiffer motion before it.
2. **480p draft** from the approved plan. Build the 2 fps contact sheet and report: head count, outfits, each timeline hit (frame block, ending), what frames cannot show.
3. The user checks voices, pronunciation, lip sync and whether speakers really stop moving. If one voice drifts, dub that character's clean line over it in the edit (free) before touching the plan.
4. Fix the plan, not the prompt in your head; one change per iteration where possible.
5. **Final** at 720p or 1080p from the same plan. Log each draft in the production log: date · route · resolution · cost · audio input · result.

**Routes.** Route A (Seedance with audio references) gives the best motion and live lip sync but voices drift and pronunciation follows the references. Route B (Avatar IV from a locked still + the exact MP3, Hailuo for silent shots) gives exact voices but a locked camera; add handheld wobble in the edit. Chaining clips: hand off on a stable frame, never more than 3 links.

# Stage 6 · Finish

1. Dub drifted lines with the clean TTS at the planned times; check lips on close-ups.
2. Trim or fade: if the palm-to-black landed late, fade the last frames to black.
3. Captions from word timestamps (MiniMax Speech 2.8 HD `subtitle_type: word`, or Fish with the character's `reference_id`); music and on-screen text added here, never generated.
4. Join hook and demo if there is one; `loudnorm` across the seam.
5. One low-res full assembly before export; final realism pass (natural light, skin texture, irregular blinking, hands and hair clean, same faces across the batch, no baked-in UI).
6. Deliver as `hook-number_pattern_character.mp4`.