#!/usr/bin/env python3
"""Host a local file at a public URL that PiAPI can fetch, and PROVE the hosted bytes are identical.

Usage: host_file.py <path>   -> prints the URL on stdout, exits 1 if no host served identical bytes.

Why this exists (lessons from the first build):
- replicate.delivery URLs served fine to us but PiAPI's fetcher got "fetch object not found".
- catbox stored a TRUNCATED copy and de-duplicates by hash, so re-uploading returned the same broken file.
- tmpfiles.org returned HTML on download.
So: upload, download back, byte-compare, only then trust the URL. Try hosts in order.
"""
import subprocess, sys, json, os, filecmp, tempfile

UA = "Mozilla/5.0 (Macintosh) ugc-skill"

def curl(*args):
    return subprocess.run(["curl", "-s", "-A", UA, *args], capture_output=True, text=True).stdout

def verify(url, path):
    if not url.startswith("http"): return False
    with tempfile.NamedTemporaryFile(delete=False) as t: tmp = t.name
    subprocess.run(["curl", "-sL", "-A", UA, "-o", tmp, url])
    ok = os.path.getsize(tmp) == os.path.getsize(path) and filecmp.cmp(path, tmp, shallow=False)
    os.unlink(tmp); return ok

def uguu(path):
    r = curl("-F", f"files[]=@{path}", "https://uguu.se/upload")
    try: return json.loads(r)["files"][0]["url"]
    except Exception: return ""

def catbox(path):
    return curl("-F", "reqtype=fileupload", "-F", f"fileToUpload=@{path}", "https://catbox.moe/user/api.php").strip()

def litterbox(path):
    return curl("-F", "reqtype=fileupload", "-F", "time=72h", "-F", f"fileToUpload=@{path}",
                "https://litterbox.catbox.moe/resources/internals/api.php").strip()

def main():
    path = sys.argv[1]
    for name, fn in (("uguu", uguu), ("catbox", catbox), ("litterbox", litterbox)):
        url = fn(path)
        if verify(url, path):
            print(url); print(f"[host_file] {name} OK: {url}", file=sys.stderr); return 0
        print(f"[host_file] {name} failed or served different bytes: {url[:80]!r}", file=sys.stderr)
    print("[host_file] no host served identical bytes", file=sys.stderr); return 1

if __name__ == "__main__": sys.exit(main())
