---
name: portrait-clone
description: "Turn any n reference images (with at least one person) into one exhaustively locked, always de-slopped, JSON-only AIGC image prompt whose every variable is pinned so each generation is nearly identical. Use for replicating a person's look, extracting portrait features into a prompt, locking a character, or revising such a JSON after comparing a generated image to the reference."
---

# Portrait Clone

Convert n reference images (at least one contains a person) into ONE AIGC image prompt in JSON.

**The single goal: pin down every variable an image model could otherwise choose, so the same prompt yields a nearly identical, non-AI-looking image on every run.** Any detail left unspecified is a detail the model will randomize. Stability is measured by how little freedom the model has left.

## Non-negotiables

1. **Lock everything.** Every visible or inferable attribute gets exactly one concrete value. Never write "or", ranges ("20-30"), "e.g.", "optional", "[slot]", "various", "some", "natural-looking" without specifics, or any alternative.
2. **Lock absence too.** Anything a model might add but the reference does not contain is stated explicitly as `"none"` (glasses, hat, earrings, bag, second person, text, logos, props, sweat, tattoos elsewhere) and also listed in `negative_prompt`.
3. **Fields are open-ended.** The Schema below is a minimum floor, not a limit. Add any key, nested object or array needed to pin a variable. Prefer splitting one vague field into several precise sub-fields. More locked fields is always better than fewer.
4. **Always de-slopped.** Every output, version and revision applies the full De-slop step, regardless of the reference's style, medium or polish.
5. **JSON only.** Reply with exactly one ```json code block and nothing else. Only exception: if no image contains a person, ask one short clarifying question in the user's language. Generating the image is a separate step that runs only after the user approves the JSON (§ Generate through treg).
6. **Top-level constraints.** `critical_constraints` and `negative_prompt` are top-level keys, right after `prompt_id` / `prompt_language`.
7. **Complete output.** Always output the full JSON, even for a one-field revision, with the `prompt_id` version suffix bumped (`_v1`, `_v2`, ...).

## Conventions

- All JSON values in English.
- Quantify whenever possible: degrees for rotation and tilt, cm for sizes and distances, mm for small details (nail length, liner flick, chain thickness), percent for framing, ratios for proportions, counts for countable things (rings, visible teeth, buttons, strands over each shoulder), hex for every color.
- Left/right from the subject's own perspective ("her left wrist"); frame positions as "frame left" / "frame right".
- Positions of small items are given by anatomical landmark ("sitting 2 cm below the collarbones", "over the right front hip pocket").

## Workflow

### 1. Inventory (internal, never output)

For each image note: shot type (face close-up / half body / full body / detail / scene / style reference), aspect ratio and pixel size, capture medium (video frame grab, phone photo, DSLR, film scan, render), and which traits it shows reliably.

### 2. Choose the anchor image

Pose, framing, lighting, camera, aspect ratio and resolution come from ONE anchor: the image the user names, otherwise the clearest medium shot. All other images only refine identity traits (face, hair, body, outfit construction, jewelry, marks).
When images conflict, trust the sharpest, most frontal, least compressed evidence. Motion blur, compression and lens distortion are not traits of the person.
Traits not visible in any image are still locked: choose the single most plausible value consistent with the rest and state it definitively.

### 3. Variable audit — pin every one

Walk this list and give each item a concrete value (or "none" / "not visible"). Add any further variable the specific images introduce.

- **Subject**: count of people, gender presentation, broad regional appearance (only if useful for rendering), apparent age as one number, height in cm, build (shoulder width, arm thickness, bust, waist-hip difference, hips), posture, weight distribution.
- **Face geometry**: shape, length/width ratio, forehead height, cheekbone height and width, midface flatness, jaw angle, chin shape, philtrum length, eye spacing, face asymmetry details.
- **Eyes** (highest drift risk — geometric): crease type and height, epicanthic fold, shape, tilt, size relative to face, height/width ratio, lid coverage, iris visibility fraction, open/blink state, smile deformation, iris hex, catchlight, liner length in mm, shadow, lash length and curl, under-eye.
- **Brows**: shape, height above eye, thickness, hex, hair density, stray hairs.
- **Nose, mouth**: nose bridge height and tip shape, nostril width; lip size, upper/lower ratio, color hex and gradient, finish, mouth opening in mm, number of visible teeth, tongue visibility.
- **Skin**: tone hex, undertone, pore visibility zones, peach fuzz, redness zones, under-eye color, shine zones, blemish count, freckles, moles (count and location), sweat.
- **Makeup**: foundation coverage, blush hex and placement, highlighter (or none), contour (or none), lip product.
- **Hair**: color hex and highlight hex, length landmark, texture per zone (roots / mids / ends), density, volume per zone, cut and layers, fringe length landmark and side, part side and position in cm from center, crown shape, placement relative to each shoulder, tucked behind ears (yes/no per side), flyaway locations, clumping, shine level.
- **Ears, neck**: ear visibility per side, earlobe visible, neck length, visible tendons.
- **Hands**: which hand is in front, finger states per hand, nail length in mm, shape, color, knuckle creases, ring count per finger per hand.
- **Jewelry and accessories**: each item with metal, thickness in mm, length or size, position landmark, side; explicit none for every common accessory not present.
- **Body marks**: tattoos, moles, scars — each with side, location, size in cm, style; or none.
- **Outfit, per garment**: item, color hex, fabric and weight, fit per zone (shoulder, chest, waist), neckline shape and depth landmark, sleeve length landmark, seams, hem position relative to waistband, tuck state, skin gap (yes/no), logo size and position, wrinkle locations, wear, how it drapes.
- **Lower body and footwear**: garment details as above; shoes or "not visible"; leg position even if cropped.
- **Pose**: stance, torso rotation, shoulder tilt, head turn and tilt in degrees, gaze target and off-lens angle, each arm (shoulder angle, elbow angle, hand height landmark, wrist angle), gesture meaning, moment type.
- **Expression**: mouth, eyes, brows, cheek lift, emotion, intensity out of 10, smile symmetry.
- **Scene**: location, background material, hex, texture, variation, distance from subject in cm, props (or none), floor visibility, visible edges (none).
- **Lighting**: key light type, size, position (angle and height), distance; fill type and ratio; background light; hair/rim light (or none); shadow direction and softness; color temperature; highlight clipping zones; catchlight shape and clock position.
- **Camera**: capture pipeline (body, lens, aperture, shutter, ISO, codec or film stock, profile), camera height in cm, subject-to-camera distance in m, focal plane, depth of field, lens distortion, horizon tilt, shot type with top/bottom crop landmarks, subject position on thirds, headroom %, lead room, sharpness, motion blur zones, noise.
- **Color**: grading style, saturation, contrast, white balance and any cast, black level, palette hexes.
- **Output and generation**: aspect ratio, exact resolution, orientation, image count, seed, guidance, steps, sampler, reference image usage.
- **Post-processing**: exact steps.

### 4. Counter the model's default prior

Image models pull every person toward a default "beautiful AI person". Any trait of the reference that differs from that default will be ignored unless escalated. For each deviation, do all three:

1. Describe it precisely and geometrically in its field.
2. Add a one-line imperative to `critical_constraints`, prefixed with an uppercase label (`EYES:`, `BODY:`, `FACE:`, `HAIR:`, `POSE:`, `TOP:`, `LOOK:`, `MEDIUM:` ...).
3. Add the default-prior version of that trait to `negative_prompt`.

Check these drift axes every time:

| Axis | Model default | Typical real-reference deviation |
|---|---|---|
| Eyes | big, round, double eyelid, aegyo sal | small, narrow, monolid, heavy lid |
| Face | V-line, doll face, perfect symmetry | long oval, flat midface, asymmetry |
| Body | curvy, busty, hourglass | slim straight frame, flat chest |
| Clothing | tighter, more skin, cleavage, midriff | coverage exactly as in reference |
| Hair | voluminous, glossy, perfect waves | flatter, straighter, clumped, frizz |
| Skin | poreless, glowing | pores, fuzz, redness |
| Stance | seated or model pose | the reference stance |
| Look | idol / model / glamour | ordinary real person |
| Background | saturated | muted exact hex |
| Additions | earrings, extra props, text | explicit none |

Counter-steer toward the reference's features, not toward plainness: if the person in the reference is glamorous, describe their features faithfully and skip anti-glamour constraints. This affects features only; De-slop still applies in full.

### 5. De-slop (mandatory, every output)

- **No quality boosters in positive fields**: 4K, 8K, ultra-detailed, hyper-detailed, ultra-realistic, photorealistic render, masterpiece, best quality, sharp focus, crisp, intricate details, flawless, stunning, perfect. List them in `negative_prompt`.
- **Anti-slop negatives always present**: oversharpened, high clarity, HDR, high micro-contrast, glowing / luminous / radiant skin, bloom; flawless / poreless / waxy / airbrushed / plastic skin; perfect symmetry, perfect teeth, every hair strand defined, shiny hair highlights; pristine wrinkle-free clothing, perfectly even background; stock photo, advertising photo, magazine cover, professional retouching, beauty campaign.
- **Capture pipeline instead of adjectives**, matched to the reference medium. `style.medium` states a real photograph or frame grab, never a render.
- **Imperfections always present and located**, calibrated to the reference but never zero: skin micro-texture and color variation, facial asymmetry, hair clumps and flyaways, fabric creases, backdrop unevenness, slight highlight clipping, sensor noise and compression, motion blur on moving hands. Each imperfection names where it occurs, so it is locked, not random.
- **Sharpness**: `camera.sharpness` specifies no sharpening and low micro-contrast; `camera.focus` never uses "sharp focus" or "crisp".
- **Grading**: flat or natural grading, muted-to-moderate saturation, low contrast unless the reference clearly shows otherwise.
- **Generation params**: `guidance_scale` 4, `steps` 28, fixed seed, fixed sampler.
- **Post-processing always present**, matched to medium:
  - Video still: downscale 50% bilinear then upscale back; 2-3% monochrome grain; re-export JPEG quality 80.
  - Film scan: 4-6% film grain; slight halation on highlights; JPEG quality 90.
  - Phone photo: mild noise-reduction smear in shadows; very faint edge halo; JPEG quality 85.
  - DSLR / mirrorless still: 1-2% grain; downscale 75% then upscale back; JPEG quality 88.
- `critical_constraints` always ends with a `MEDIUM:` line stating the image must read as an unretouched real capture in the reference medium, never as a render or retouched photo.

### 6. Assemble

Start from the Schema, fill every key, then add every extra key the audit produced. Keep related keys together in the most relevant section.

### 7. Self-check (internal) before output

- **Freedom check**: scan every value for words that leave a choice to the model (some, various, natural, typical, stylish, casual, nice, a bit, several, around, approximately without a number, or). Replace each with a concrete value.
- **Absence check**: every common addition not in the reference is set to none and appears in `negative_prompt`.
- **De-slop check**: no booster word in any positive field, anti-slop negatives present, capture pipeline present, located imperfections present, sharpness and grading compliant, guidance 4 / steps 28, post_processing present, `MEDIUM:` constraint present.
- **Consistency check**: no contradiction between positive fields and `negative_prompt` (e.g. positive noise vs negative "film grain" → write "heavy film grain"); ring, bracelet and tattoo sides agree across all sections; `output.resolution` matches `output.aspect_ratio`; every color has a hex and the palette lists the main ones.
- **Drift check**: every drift axis found in step 4 appears in both `critical_constraints` and `negative_prompt`.

## Generate through treg

Once the user approves the JSON, generate the image **through treg**, never with a vendor key on this machine. treg injects the credential server-side and the charge lands on the team's prepaid balance. If `treg --version` fails, install it first:

```bash
curl -fsSL https://treg.to/install.sh | sh
treg login
```

1. Host the reference images: `treg host ref1.jpg` prints a public URL that serves raw image bytes (7-day TTL). Never use paste hosts; vendors probe the URL and reject HTML file pages.
2. Serialize the approved JSON as the prompt string and submit. Say the price before running; `treg catalog get <id>` shows the live table.

| Endpoint | Price (1K) | Notes |
|---|---|---|
| `reapi.image-gen.gemini-3-pro-image` | $0.03 | default: up to 14 reference URLs, best identity hold |
| `reapi.image-gen.gpt-image-2` | $0.08 | flat rate, edit or generate |
| `reapi.image-gen.gpt-image-2-5` | $0.41 | highest fidelity, use for the final locked frame |
| `piapi.image-gen.gemini-3-pro-image` | $0.105 | same model, when reAPI is down |

```bash
treg call reapi.image-gen.gemini-3-pro-image --method POST --data '{
  "model": "gemini-3-pro-image-preview",
  "prompt": "<the approved JSON, minified>",
  "size": "9:16", "resolution": "1K",
  "image_urls": ["<treg host URL of the anchor image>"]
}'
# -> {"id": "task_...", "status": "processing"}
treg call reapi.tasks.get --query id=task_...   # poll every 5 s, free; output.image_urls when status == completed
```

- A catalog id takes its parameters in `--data` or `--query`, never as a URL path.
- Output URLs live about 7 days; download the frame you keep next to the JSON that made it.
- A failed task is not charged. There is no seed on these routes: every run is a fresh roll, so compare against the reference and go to § Iteration mode rather than re-rolling blind.
- The chosen frame is the input to the `ugc-talking-head-video` skill.

## Iteration mode

When the user shows a generated image next to the reference, or says a trait does not match:

1. Diff generated vs reference variable by variable across the whole audit list, not only the trait the user named. Also check stance, clothing coverage, hair volume, background saturation, unwanted additions, and remaining AI-slop signs.
2. Every difference means a variable was under-locked: split it into finer sub-fields with more concrete geometry, add or strengthen its `critical_constraints` line, and add the drifted appearance to `negative_prompt`.
3. For each remaining slop sign: strengthen the matching De-slop fields and negatives.
4. If the user asks for a patch-style change, still merge it and output the complete JSON.
5. Output the full updated JSON with the version bumped.

## Boundaries

- Never identify or name the person, never add a real name, celebrity comparison or "looks like X" to the JSON.
- Use appearance descriptors only; never infer nationality, religion or other personal attributes.
- If the person may be under 18: body description limited to height and neutral build, no bust or hip descriptors, nothing sexualizing, and no revealing clothing constraints beyond what is visible.
- Text-only prompts lock a consistent lookalike character, not a verified identity; the output is meant for a consistent original character.

## Schema (minimum floor; fixed order for these keys; extend freely)

```json
{
  "prompt_id": "{snake_case_name}_locked_v1",
  "prompt_language": "en",
  "critical_constraints": [
    "{LABEL}: {one-line imperative per drift axis}",
    "MEDIUM: must read as an unretouched {reference medium}, never as a render or retouched photo"
  ],
  "negative_prompt": [
    "ultra-detailed, hyper-detailed, 8K, 4K, masterpiece, best quality, ultra-realistic, photorealistic render, sharp focus, crisp, intricate details",
    "oversharpened, high clarity, HDR, high micro-contrast, glowing skin, luminous, radiant, dreamy glow, soft glow bloom",
    "flawless skin, poreless, smooth skin, perfect skin, porcelain, waxy, glossy highlights on skin, airbrushed, retouched, plastic skin",
    "perfect symmetry, perfect teeth, perfect hair, every strand defined, shiny hair highlights",
    "pristine clothing, wrinkle-free fabric, perfectly even background, studio perfection",
    "stock photo, advertising photo, magazine cover, professional retouching, beauty campaign",
    "{one line per drift axis: the model-default version}",
    "{every absent item: glasses, hat, earrings, bag, second person, props, text}",
    "{wrong garments, colors, jewelry}",
    "{wrong hand poses}",
    "extra fingers, missing fingers, fused fingers, deformed hands, extra arms",
    "{wrong framing and angles}",
    "{wrong background}",
    "{wrong lighting}",
    "{wrong grading}",
    "illustration, 3D render, CGI, painting, cartoon, watermark, text, subtitles"
  ],
  "subject": {"count": 1, "gender": "", "ethnicity": "", "apparent_age": "", "attractiveness_level": "", "height_impression": "", "build": "", "posture": "", "weight_distribution": ""},
  "face": {
    "shape": "", "length_width_ratio": "", "forehead": "", "cheekbones": "", "midface": "", "jaw": "", "chin": "", "philtrum": "", "eye_spacing": "", "asymmetry": "",
    "skin": {"tone": "{desc}, hex #", "undertone": "", "texture": "", "color_variation": "", "shine_zones": "", "blemishes": "", "freckles": "", "moles": "", "sweat": "none"},
    "eyes": {"type": "", "shape": "", "tilt": "", "size": "", "height_width_ratio": "", "lid": "", "aperture": "", "blink_state": "", "smile_behavior": "", "iris_color": "", "liner": "", "eyeshadow": "", "lashes": "", "under_eye": ""},
    "eyebrows": {"shape": "", "position": "", "thickness": "", "color": "", "texture": ""},
    "nose": {"bridge": "", "tip": "", "nostrils": ""},
    "mouth": {"lip_shape": "", "lip_ratio": "", "lip_color": "", "lip_finish": "", "opening": "", "visible_teeth": "", "tongue": "not visible"},
    "makeup": {"foundation": "", "blush": "", "highlighter": "", "contour": ""},
    "ears": {"left": "", "right": ""}
  },
  "hair": {"color": "", "highlight_color": "", "length": "", "texture": {"roots": "", "mids": "", "ends": ""}, "density": "", "volume": "", "cut": "", "fringe": "", "part": "", "crown": "", "placement": {"left_shoulder": "", "right_shoulder": ""}, "behind_ears": {"left": "", "right": ""}, "imperfections": "", "shine": ""},
  "neck": "",
  "hands": {"front_hand": "", "nails": {"length_mm": "", "shape": "", "color": ""}, "rings": {"left_hand": "", "right_hand": ""}, "fingers": ""},
  "jewelry": {"necklace": "", "bracelet": "", "earrings": "", "watch": "", "other": "none"},
  "accessories": {"glasses": "none", "headwear": "none", "bag": "none", "other": "none"},
  "body_marks": {"tattoos": "", "moles": "", "scars": "none"},
  "outfit": {
    "top": {"item": "", "color": "", "fabric": "", "fit": {"shoulders": "", "chest": "", "waist": ""}, "neckline": "", "sleeves": "", "construction": "", "hem": "", "tuck": "", "skin_gap": "", "logo": "", "wrinkles": "", "wear": ""},
    "bottom": {"item": "", "color": "", "fabric": "", "fit": "", "rise": "", "details": "", "wrinkles": ""},
    "belt": "", "shoes": ""
  },
  "pose": {"stance": "", "legs": "", "body_orientation": "", "shoulder_tilt": "", "head": "", "gaze": "", "right_arm": "", "left_arm": "", "gesture_meaning": "", "moment": ""},
  "expression": {"mouth": "", "eyes": "", "brows": "", "cheeks": "", "smile_symmetry": "", "emotion": "", "intensity": "{n} out of 10"},
  "scene": {"location": "", "background": "", "background_texture": "", "background_variation": "", "background_distance": "", "props": "none", "floor": "", "visible_edges": "none"},
  "lighting": {"key": "", "fill": "", "background_light": "", "hair_light": "", "shadows": "", "color_temperature": "", "clipping": "", "catchlights": ""},
  "camera": {"capture_pipeline": "", "camera_height": "", "subject_distance": "", "shot_type": "", "crop_top": "", "crop_bottom": "", "subject_position": "", "headroom": "", "lead_room": "", "angle": "", "horizon_tilt": "0 degrees", "lens": "", "lens_distortion": "", "aperture": "", "shutter": "", "iso": "", "focus": "", "depth_of_field": "", "sharpness": "no sharpening, low micro-contrast, {calibrated softness}", "motion": "", "noise": ""},
  "color_grading": {"style": "", "saturation": "", "contrast": "", "black_level": "", "white_balance": "", "palette": ["#"]},
  "style": {"medium": "real photograph: {medium}", "genre": "", "realism": "unretouched, unpolished, true-to-life, no idealization", "overall_vibe": ""},
  "output": {"aspect_ratio": "", "resolution": "", "orientation": "", "num_images": 1},
  "generation_params": {"seed": 20260911, "guidance_scale": 4, "steps": 28, "sampler": "DPM++ 2M Karras", "reference_image": "none", "note_to_model": "critical_constraints override any default beauty or quality bias. Follow every field literally; do not add, remove, beautify, sharpen or reinterpret any attribute. Anything not described does not exist in the image."},
  "post_processing": {"step_1": "", "step_2": "", "step_3": ""}
}
```