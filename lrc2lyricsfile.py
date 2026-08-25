#!/usr/bin/env python3
"""
lrc2lyricsfile.py - Convert an ESLyric-style .lrc file into a Lyricsfile 1.0
(.lyricsfile.yaml) document, compatible with the spec.

If a sibling audio file with the same base name exists (.mp3 / .flac / .m4a /
.ogg / .opus), its tags (title, artist, album) and exact duration are read with
ffprobe and take priority. The positional arguments are then used only as a
fallback for any value missing from the audio file.

Usage:
  ./lrc2lyricsfile.py <lrc_path> [duration] [title] [artist] [lang]
  ./lrc2lyricsfile.py <lrc_path> --publish          # convert then publish
  ./lrc2lyricsfile.py <lrc_path> --convert-only     # (default) convert only

Dependencies: python3 (standard library only) for parsing/serialization; ffprobe
(optional) for reading audio tags; uploadlyricsfile.py (for --publish).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys


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


def ts_to_ms(minutes, seconds, frac_str):
    """Convert timestamp components to milliseconds.

    frac_str may be None/empty (no fractional part), 1 digit (tenths),
    2 digits (hundredths) or 3 digits (milliseconds).
    """
    if not frac_str:
        millis = 0
    else:
        frac_len = len(frac_str)
        if frac_len == 1:
            millis = int(frac_str) * 100
        elif frac_len == 2:
            millis = int(frac_str) * 10
        else:
            millis = int(frac_str)
    return (int(minutes) * 60 + int(seconds)) * 1000 + millis


def parse_lrc(content):
    """
    Parse an ESLyric-format LRC file.

    Format: [mm:ss.mmm]<mm:ss.mmm>char<mm:ss.mmm>char...
    Returns: {
        "id_tags": {"ti": ..., "ar": ..., "al": ...},
        "lines": [
            {"text", "start_ms", "end_ms"(optional),
             "words": [{"text", "start_ms", "end_ms"(optional)}, ...]},
            ...
        ]
    }
    """
    id_tags = {}
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # extract ID tags: [ti:Title] [ar:Artist] [al:Album] etc.
        id_match = re.match(r'^\[([a-zA-Z]+):(.+?)\]$', line)
        if id_match:
            id_tags[id_match.group(1).lower()] = id_match.group(2).strip()
            continue

        # skip other non-standard tags
        if re.match(r'^\[[a-zA-Z]+:', line):
            continue

        # parse line-level timestamp: [mm:ss], [mm:ss.mmm], or [mm:ssmmm]
        # (the fractional separator dot is optional in some variants)
        line_ts_match = re.match(
            r'^\[(\d{1,2}):(\d{1,2})(?:[.]?(\d{1,3}))?\](.*)', line)
        if not line_ts_match:
            continue

        line_start_ms = ts_to_ms(
            int(line_ts_match.group(1)),
            int(line_ts_match.group(2)),
            line_ts_match.group(3),
        )
        rest = line_ts_match.group(4)

        # parse word-level timestamps. Variants use either <...> or [...] as
        # delimiters, and the fractional part is optional:
        #   <mm:ss.mmm>word<mm:ss.mmm>word...   (test_eslyric.lrc)
        #   [mm:ss.mmm]word[mm:ss.mmm]word...   (test_eslyric_2.lrc)
        words = []
        word_pattern = re.compile(
            r'[<\[](\d{1,2}):(\d{1,2})(?:[.]?(\d{1,3}))?[>\]]'
            r'([^<\[]*)')
        word_matches = list(word_pattern.finditer(rest))

        if word_matches:
            # text before the first word-level timestamp is the first word,
            # using the line-level timestamp as its start.
            prefix = rest[:word_matches[0].start()]
            if prefix:
                words.append({
                    "text": prefix,
                    "start_ms": line_start_ms,
                })
            for m in word_matches:
                w_start_ms = ts_to_ms(
                    m.group(1),
                    m.group(2),
                    m.group(3),
                )
                words.append({
                    "text": m.group(4),
                    "start_ms": w_start_ms,
                })
            # drop empty-text entries (e.g. a trailing <ts>/[ts] with no word),
            # after they have supplied the previous word's end below.
            words = [w for w in words if w["text"]]
        else:
            # no word-level timestamps: treat the whole line as one word
            text = rest.strip()
            if text:
                words.append({
                    "text": text,
                    "start_ms": line_start_ms,
                })

        # set each word's end_ms = next word's start_ms
        for i in range(len(words)):
            if i < len(words) - 1:
                words[i]["end_ms"] = words[i + 1]["start_ms"]

        line_text = "".join(w["text"] for w in words)
        if not line_text:
            continue

        lines.append({
            "text": line_text,
            "start_ms": line_start_ms,
            "words": words,
        })

    # set each line's end_ms = next line's start_ms, and if a line's last word
    # has no explicit end timestamp, borrow the next line's start time too.
    for i in range(len(lines)):
        if i < len(lines) - 1:
            next_start = lines[i + 1]["start_ms"]
            lines[i]["end_ms"] = next_start
            wlist = lines[i]["words"]
            if wlist and "end_ms" not in wlist[-1]:
                wlist[-1]["end_ms"] = next_start

    return {"id_tags": id_tags, "lines": lines}


def q(s):
    """Single-quote a scalar, escaping embedded single quotes."""
    return "'" + s.replace("'", "''") + "'"


def build_lyricsfile_yaml(parsed, title, artist, album, duration_ms, lang):
    lines = parsed["lines"]

    out = []
    out.append("version: '1.0'")
    out.append("")
    out.append("metadata:")
    out.append("  title: " + q(title))
    out.append("  artist: " + q(artist))
    if album:
        out.append("  album: " + q(album))
    out.append("  duration_ms: " + str(duration_ms))
    if lang:
        out.append("  language: " + q(lang))
    out.append("  instrumental: false")
    out.append("")

    if lines:
        out.append("lines:")
        for ln in lines:
            out.append("  - text: " + q(ln["text"]))
            out.append("    start_ms: " + str(ln["start_ms"]))
            if "end_ms" in ln:
                out.append("    end_ms: " + str(ln["end_ms"]))
            if ln["words"]:
                out.append("    words:")
                for w in ln["words"]:
                    out.append("      - text: " + q(w["text"]))
                    out.append("        start_ms: " + str(w["start_ms"]))
                    if "end_ms" in w:
                        out.append("        end_ms: " + str(w["end_ms"]))
    out.append("")

    plain = "\n".join(ln["text"] for ln in lines)
    if plain:
        out.append("plain: |")
        for pl in plain.split("\n"):
            out.append("  " + pl)
    else:
        out.append("plain: ''")

    return "\n".join(out) + "\n"


def parse_duration(s):
    """Parse a duration string to milliseconds.

    Accepts a timecode [[H:]MM:]SS.mmm (-> ms), a bare integer (-> ms),
    or a float (-> seconds).
    """
    s = s.strip()
    if ":" in s:
        comps = [float(c) for c in s.split(":")]
        secs = 0.0
        for c in comps:
            secs = secs * 60 + c
        return int(round(secs * 1000))
    if "." in s:
        return int(round(float(s) * 1000))
    return int(s)


AUDIO_EXTS = ["mp3", "flac", "m4a", "ogg", "opus"]


def norm_name(s):
    """Normalize a base name for matching: drop spaces, parentheses, dots."""
    return re.sub(r'[\s().]', '', s)


def find_audio(lrc_path):
    """Find a sibling audio file whose normalized name matches the .lrc base."""
    if shutil.which("ffprobe") is None:
        return None
    directory = os.path.dirname(os.path.abspath(lrc_path))
    base = os.path.splitext(os.path.basename(lrc_path))[0]
    target = norm_name(base)
    for fn in os.listdir(directory):
        ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        if ext in AUDIO_EXTS and norm_name(fn.rsplit(".", 1)[0]) == target:
            return os.path.join(directory, fn)
    return None


def read_audio_tags(audio_path):
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", audio_path],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    fmt = data.get("format", {})
    tags = fmt.get("tags", {})
    dur = fmt.get("duration")
    duration_ms = int(round(float(dur) * 1000)) if dur else 0
    return {
        "title": tags.get("title", "") or "",
        "artist": tags.get("artist", "") or "",
        "album": tags.get("album", "") or "",
        "duration_ms": duration_ms,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Convert an ESLyric .lrc file into a Lyricsfile 1.0 YAML.",
    )
    parser.add_argument("lrc_path", help="Path to the ESLyric .lrc file")
    parser.add_argument("duration", nargs="?", default=None,
                        help="Duration fallback: timecode, ms, or seconds")
    parser.add_argument("title", nargs="?", default=None, help="Title fallback")
    parser.add_argument("artist", nargs="?", default=None, help="Artist fallback")
    parser.add_argument("lang", nargs="?", default=None,
                        help="ISO 639-1 language code (e.g. zh, en)")
    parser.add_argument("--publish", action="store_true",
                        help="Convert then publish with uploadlyricsfile.py")
    parser.add_argument("--instance", default="https://lrclib.net",
                        help="LRCLIB instance URL (used with --publish)")
    parser.add_argument("-o", "--output-dir", default=None,
                        help="Directory for the generated .lyricsfile.yaml "
                             "(default: next to the input file)")
    args = parser.parse_args()

    lrc_path = os.path.expanduser(args.lrc_path)
    if not os.path.isfile(lrc_path):
        print(f"[ERROR] File not found: {lrc_path}", file=sys.stderr)
        sys.exit(1)

    base = os.path.splitext(os.path.basename(lrc_path))[0]
    if args.output_dir:
        out_dir = os.path.expanduser(args.output_dir)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, base + ".lyricsfile.yaml")
    else:
        # Default: write next to the source input file.
        out_dir = os.path.dirname(os.path.abspath(lrc_path))
        out_path = os.path.join(out_dir, base + ".lyricsfile.yaml")

    # Resolve metadata (audio priority, manual fallback)
    audio = find_audio(lrc_path)
    a_title = a_artist = a_album = ""
    a_duration_ms = 0
    if audio:
        print(f"Reading tags from audio: {os.path.basename(audio)}")
        tags = read_audio_tags(audio)
        if tags:
            a_title = tags["title"]
            a_artist = tags["artist"]
            a_album = tags["album"]
            a_duration_ms = tags["duration_ms"]

    content = read_text(lrc_path)
    parsed = parse_lrc(content)
    id_tags = parsed.get("id_tags", {})

    title = a_title or (args.title or "") or id_tags.get("ti", "")
    artist = a_artist or (args.artist or "") or id_tags.get("ar", "")
    album = a_album or id_tags.get("al", "")
    duration_raw = str(a_duration_ms) if a_duration_ms else (args.duration or "")
    lang = args.lang or id_tags.get("language", "")

    if not title or not artist or not duration_raw:
        print("Error: missing required metadata.", file=sys.stderr)
        if not title:
            print("  title is missing (no audio tag, <title> arg, or [ti:] tag)", file=sys.stderr)
        if not artist:
            print("  artist is missing (no audio tag, <artist> arg, or [ar:] tag)", file=sys.stderr)
        if not duration_raw:
            print("  duration is missing (no audio file and no <duration> arg)", file=sys.stderr)
        sys.exit(1)

    yaml_content = build_lyricsfile_yaml(
        parsed, title, artist, album, parse_duration(duration_raw), lang)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    print(f"Wrote: {out_path}")
    print(f"  title: {title} / artist: {artist}" +
          (f" / album: {album}" if album else ""))
    print(f"  lines: {len(parsed['lines'])}, "
          f"words: {sum(len(ln['words']) for ln in parsed['lines'])}, "
          f"duration_ms: {parse_duration(duration_raw)}")
    if lang:
        print(f"  language: {lang}")

    if args.publish:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        upload_script = os.path.join(script_dir, "uploadlyricsfile.py")
        cmd = [sys.executable, upload_script, out_path]
        if args.instance != "https://lrclib.net":
            cmd += ["--instance", args.instance]
        print()
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
