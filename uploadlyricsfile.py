#!/usr/bin/env python3
"""
uploadlyricsfile.py - Publish a Lyricsfile (.lyricsfile.yaml) to a LRCLIB
instance, without any GUI. Performs the proof-of-work challenge and the
publish request.

Usage:
  ./uploadlyricsfile.py <path.lrc | path.lyricsfile.yaml>
  LRCLIB_INSTANCE=https://lrclib.net ./uploadlyricsfile.py song.lrc

Dependencies: python3 (standard library only) - urllib for HTTP, hashlib for
the proof-of-work. No curl/jq/pyyaml required.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request


def read_text(path):
    """Read a text file, tolerating common non-UTF-8 encodings (e.g. GBK/GB18030).

    Tries UTF-8 (with/without BOM) first, then the common CJK codecs, and
    finally falls back to latin-1 (lossless) / replacement so a read never
    raises UnicodeDecodeError.
    """
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def http_post(url, payload, headers):
    """POST JSON and return (status_code, response_body)."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def solve_challenge(prefix, target_hex):
    """Brute-force the nonce: SHA256(prefix + nonce) <= target."""
    target = bytes.fromhex(target_hex)
    n = 0
    while True:
        if hashlib.sha256(f"{prefix}{n}".encode()).digest() <= target:
            return f"{prefix}:{n}"
        n += 1


def parse_metadata(text):
    """Extract scalar metadata (title/artist/album/duration_ms) from the YAML."""
    meta = {}
    in_meta = False
    for line in text.splitlines():
        if re.match(r'^metadata:\s*$', line):
            in_meta = True
            continue
        if in_meta:
            m = re.match(r'^(\s+)(\w+):\s*(.*)$', line)
            if m and len(m.group(1)) >= 2:
                meta[m.group(2)] = m.group(3).strip()
            else:
                in_meta = False
    return meta


def clean(v):
    """Strip single quotes from a scalar value."""
    v = v.strip()
    if len(v) >= 2 and v.startswith("'") and v.endswith("'"):
        return v[1:-1]
    return v


def resolve_yaml(path):
    if path.endswith(".lyricsfile.yaml"):
        return path
    if path.endswith(".lrc"):
        directory = os.path.dirname(os.path.abspath(path))
        base = os.path.splitext(os.path.basename(path))[0]
        return os.path.join(directory, base + ".lyricsfile.yaml")
    print("Error: input must be a .lrc or .lyricsfile.yaml file", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Publish a Lyricsfile to a LRCLIB instance.",
    )
    parser.add_argument("path", help="Path to a .lrc or .lyricsfile.yaml file")
    parser.add_argument("--instance", default=os.environ.get(
        "LRCLIB_INSTANCE", "https://lrclib.net"),
        help="LRCLIB instance URL")
    args = parser.parse_args()

    instance = args.instance.rstrip("/")
    yaml_path = resolve_yaml(os.path.expanduser(args.path))
    if not os.path.isfile(yaml_path):
        print(f"Error: lyricsfile not found: {yaml_path}", file=sys.stderr)
        print("       (run lrc2lyricsfile.py first to generate it)", file=sys.stderr)
        sys.exit(1)

    text = read_text(yaml_path)

    meta = parse_metadata(text)
    track_name = clean(meta.get("title", ""))
    artist_name = clean(meta.get("artist", ""))
    album_name = clean(meta.get("album", ""))
    dm = clean(meta.get("duration_ms", "0"))
    duration_ms = int(dm) if dm not in ("", "0") else 0
    duration = round(duration_ms / 1000)

    if not track_name or not artist_name:
        print("Error: metadata.title and metadata.artist are required", file=sys.stderr)
        sys.exit(1)
    if duration <= 0:
        print("Warning: duration is 0; provide an accurate duration for matching",
              file=sys.stderr)

    print(f"[1/3] Requesting challenge from {instance} ...")
    status, body = http_post(f"{instance}/api/request-challenge", {}, {})
    if status != 200:
        print(f"Error: request-challenge failed (HTTP {status}):", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(1)
    challenge = json.loads(body)
    prefix = challenge["prefix"]
    target = challenge["target"]
    print(f"  prefix={prefix} target={target}")

    print("[2/3] Solving proof-of-work ...")
    token = solve_challenge(prefix, target)
    print(f"  token={token}")

    print("[3/3] Publishing ...")
    payload = {
        "trackName": track_name,
        "artistName": artist_name,
        "albumName": album_name,
        "duration": duration,
        "lyricsfile": text,
    }
    status, body = http_post(
        f"{instance}/api/publish",
        payload,
        {"X-Publish-Token": token},
    )

    if status == 201:
        print("SUCCESS: lyrics published to " + instance)
        print("         (may take up to 24h to appear in search)")
        sys.exit(0)
    else:
        print(f"FAIL: HTTP {status}", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
