#!/usr/bin/env python3
"""Generate a talking-head clip with Seedance 2.5 THROUGH TREG from a character image + voice-reference audio.

Usage:
  seedance_treg.py --image char.jpg --audio voice_ref.mp3 --prompt-file prompt.txt \
      --duration 18 --out take.mp4 [--provider reapi|piapi] [--resolution 720p] [--tier relaxed|standard] [--dry-run]

No vendor key on this machine: every request is `treg call <catalog-id>`, so the credential stays server-side and
the charge lands on the team's treg balance. Saves <out>.request.json next to the output so the run is reproducible.

Rules baked in (from the first two builds):
- reference files are hosted with `treg host` (public URL, 7-day TTL, byte-exact); host_file.py is the fallback for an
  older registry. Paste hosts failed vendor probes at random: catbox unreachable from reAPI's probe, tmpfiles serving
  HTML, uguu timing out. Five submissions for one take on 2026-09-14.
- audio reference limits differ: PiAPI 2-15 s, reAPI 2-30 s. Refused here instead of after the upload.
- a failed task is not charged; a moderation failure on the standard tier -> rerun on the relaxed tier.
- reAPI is the default: same model, same voice clip accepted, 24% cheaper at 720p, and it has 1080p.
"""
import argparse, json, os, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# USD per second of output, from the catalog price tables (treg catalog get <id>).
RATE = {
    "reapi": {"standard": {"480p": .1186, "720p": .26683, "1080p": .462},
              "relaxed": {"480p": .1186, "720p": .26683, "1080p": .462}},
    "piapi": {"standard": {"480p": .15, "720p": .35, "1080p": .80},
              "relaxed": {"480p": .165, "720p": .385, "1080p": .88}},
}
AUDIO_MAX_S = {"reapi": 30, "piapi": 15}
SUBMIT = {("reapi", "standard"): "reapi.video-gen.seedance-2-5",
          ("reapi", "relaxed"): "reapi.video-gen.seedance-2-5.unrestricted",
          ("piapi", "standard"): "piapi.video-gen.seedance-2-5",
          ("piapi", "relaxed"): "piapi.video-gen.seedance-2-5.less-restriction"}


def treg(*args):
    r = subprocess.run(["treg", *args], capture_output=True, text=True)
    out = r.stdout.strip()
    try:
        return json.loads(out)
    except ValueError:
        sys.exit(f"treg {' '.join(args[:2])} failed (exit {r.returncode}):\n{out}\n{r.stderr.strip()}")


def host(path):
    if path.startswith("http"): return path
    r = subprocess.run(["treg", "host", path], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip().startswith("http"):
        return r.stdout.strip()
    print(f"[seedance] treg host unavailable ({r.stderr.strip()[:120]}); falling back to host_file.py", file=sys.stderr)
    r = subprocess.run([sys.executable, f"{HERE}/host_file.py", path], capture_output=True, text=True)
    if r.returncode: sys.exit(r.stderr)
    return r.stdout.strip()


def dur(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                capture_output=True, text=True).stdout.strip())


def body_for(provider, tier, prompt, duration, resolution, img, aud):
    if provider == "reapi":
        return {"model": "doubao-seedance-2.5-face", "prompt": prompt, "duration": duration, "size": "9:16",
                "resolution": resolution, "generate_audio": True, **({"content_filter": False} if tier == "relaxed" else {}),
                "image_urls": [img], **({"audio_urls": [aud]} if aud else {})}
    return {"model": "seedance", "task_type": "seedance-2.5" + ("-less-restriction" if tier == "relaxed" else ""),
            "input": {"prompt": prompt, "duration": duration, "aspect_ratio": "9:16", "resolution": resolution,
                      "image_urls": [img], **({"audio_urls": [aud]} if aud else {})}}


def poll(provider, tid):
    """Returns (status, video_url, cost_usd, raw). Polling is free on both providers."""
    while True:
        if provider == "reapi":
            d = treg("call", "reapi.tasks.get", "--query", f"id={tid}")
            st = (d.get("status") or "").lower()
            if st in ("completed", "succeeded", "failed", "error"):
                return st, ((d.get("output") or {}).get("video_urls") or [None])[0], (d.get("usage") or {}).get("credits", 0) / 1000, d
        else:
            d = treg("call", "piapi.task.get", "--query", f"task_id={tid}").get("data") or {}
            st = (d.get("status") or "").lower()
            if st in ("completed", "success", "failed"):
                return st, (d.get("output") or {}).get("video"), ((d.get("meta") or {}).get("usage") or {}).get("consume", 0) / 1e7, d
        time.sleep(10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True); ap.add_argument("--audio")
    ap.add_argument("--prompt-file", required=True); ap.add_argument("--duration", type=int, required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--resolution", default="720p", choices=["480p", "720p", "1080p"])
    ap.add_argument("--provider", default="reapi", choices=list(RATE))
    ap.add_argument("--tier", default="relaxed", choices=["relaxed", "standard"]); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    prompt = open(a.prompt_file).read().strip()
    assert len(prompt) <= 4000, f"prompt is {len(prompt)} chars; PiAPI caps at 4000"
    assert 4 <= a.duration <= 30
    if a.audio and not a.audio.startswith("http"):
        d = dur(a.audio); lim = AUDIO_MAX_S[a.provider]
        assert 2 <= d <= lim, f"audio reference is {d:.1f}s; {a.provider} allows 2-{lim} s. Cut it first."
    est = a.duration * RATE[a.provider][a.tier][a.resolution]
    print(f"[seedance] estimated cost ${est:.2f} ({a.duration}s x {a.resolution} x {a.provider}/{a.tier})", file=sys.stderr)
    if a.dry_run:
        print(json.dumps(body_for(a.provider, a.tier, prompt, a.duration, a.resolution, a.image, a.audio), indent=2)); return
    img = host(a.image); aud = host(a.audio) if a.audio else None
    body = body_for(a.provider, a.tier, prompt, a.duration, a.resolution, img, aud)
    json.dump(body, open(a.out + ".request.json", "w"), indent=2)
    endpoint = SUBMIT[(a.provider, a.tier)]
    t = treg("call", endpoint, "--data", json.dumps(body))
    tid = t.get("id") if a.provider == "reapi" else (t.get("data") or {}).get("task_id")
    if not tid: sys.exit(f"[seedance] submit refused (not charged):\n{json.dumps(t, indent=2)}")
    print(f"[seedance] task {tid} via {endpoint}", file=sys.stderr)
    st, url, cost, raw = poll(a.provider, tid)
    if st in ("failed", "error") or not url:
        sys.exit(f"[seedance] FAILED (not charged): {json.dumps(raw.get('error') or raw, indent=2)[:800]}")
    urllib.request.urlretrieve(url, a.out)
    print(f"[seedance] done -> {a.out}  cost ${cost:.2f}", file=sys.stderr)


if __name__ == "__main__": main()
