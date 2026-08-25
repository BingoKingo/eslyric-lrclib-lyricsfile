# LRCLIB Lyricsfile Tools

A small, dependency-light toolkit for working with **Lyricsfile 1.0** documents and
the **LRCLIB** lyrics database. It converts ESLyric-style `.lrc` files (including
word-level `<mm:ss.mmm>` timestamps) into Lyricsfile YAML and publishes them to a
LRCLIB instance.

Everything here runs on stock `python3` (standard library only) and optionally
`ffprobe`. No `curl`, `jq`, or third-party Python packages are required.

## Directory layout

```
scripts/
├── lrc2lyricsfile.py    # .lrc  -> .lyricsfile.yaml  (conversion)
├── uploadlyricsfile.py  # .lyricsfile.yaml -> LRCLIB (publish, no GUI)
├── README.md            # this file
└── tests/               # test helpers / sample data
    ├── generate_fake_music.py # generate dummy music files for testing
    ├── test_eslyric.lrc       # sample ESLyric LRC
    └── README.md
```

## Prerequisites

| Tool      | Used for                                            | Required? |
| --------- | --------------------------------------------------- | --------- |
| `python3` | Parsing LRC, emitting YAML, HTTP, proof-of-work      | yes       |
| `ffprobe` | Reading title/artist/album/duration from audio files | optional* |

\* If `ffprobe` is missing, the conversion script falls back to manual arguments
and/or `[ti:]`/`[ar:]`/`[al:]` tags embedded in the `.lrc` file.

## Quick start

```bash
# 1. Convert an .lrc next to its audio file (tags + duration read from the mp3/flac)
./lrc2lyricsfile.py "path/to/Song.lrc"

# 2. Publish the generated .lyricsfile.yaml to LRCLIB (implicit, see --publish)
./lrc2lyricsfile.py "path/to/Song.lrc" --publish
# or publish an already-generated file directly:
./uploadlyricsfile.py "path/to/Song.lrc"
```

`uploadlyricsfile.py` accepts either the `.lrc` path (it locates the sibling
`.lyricsfile.yaml`) or the `.lyricsfile.yaml` path directly.

## `lrc2lyricsfile.py` — conversion

```
./lrc2lyricsfile.py <lrc_path> [duration] [title] [artist] [lang]
./lrc2lyricsfile.py <lrc_path> --publish [--instance https://lrclib.net]
```

- If a sibling audio file with the same base name exists (`.mp3`, `.flac`,
  `.m4a`, `.ogg`, `.opus`, case-insensitive), its tags (`title`, `artist`,
  `album`) and exact duration are read with `ffprobe` and **take priority**.
- Otherwise, the positional arguments (`title`, `artist`, `duration`, `lang`)
  are used; as a last resort, `[ti:]`/`[ar:]`/`[al:]`/`[language:]` ID tags
  embedded in the `.lrc` file are read. If no audio file is present, `duration`,
  `title` and `artist` become required.
- `<duration>` accepts a timecode `[[H:]MM:]SS.mmm` (converted to milliseconds),
  a bare integer (milliseconds), or a float (seconds).
- `<lang>` is an optional ISO 639-1 code (e.g. `zh`, `en`).
- With `--publish`, the generated file is sent to LRCLIB via `uploadlyricsfile.py`.

Output: `<lrc_dir>/<lrc_basename>.lyricsfile.yaml`, conforming to the Lyricsfile
1.0 spec (`version`, `metadata`, `lines` with `words`, `plain` as a literal block).

Example without an audio file:

```bash
./lrc2lyricsfile.py song.lrc 3:55.755 "Summer" "Artist Name" zh
```

## `uploadlyricsfile.py` — publish

```
./uploadlyricsfile.py <path.lrc | path.lyricsfile.yaml>
LRCLIB_INSTANCE=https://lrclib.net ./uploadlyricsfile.py song.lrc
```

Steps performed automatically:

1. `POST /api/request-challenge` to obtain a PoW `prefix`/`target`.
2. Solve the proof-of-work locally (`SHA256(prefix + nonce) <= target`).
3. `POST /api/publish` with header `X-Publish-Token: prefix:nonce`, sending
   `trackName`, `artistName`, `albumName`, `duration` (seconds) and `lyricsfile`
   (the full YAML text). A `201` response means success.

The `LRCLIB_INSTANCE` environment variable overrides the target instance
(default: `https://lrclib.net`).

## Lyricsfile format

The generated YAML follows the [Lyricsfile](https://github.com/tranxuanthang/lyricsfile) 1.0 draft. Key points:

- `version: '1.0'`
- `metadata`: `title`, `artist`, optional `album`/`duration_ms`/`language`/`instrumental`
- `lines`: list of `{ text, start_ms, end_ms?, words: [{ text, start_ms, end_ms? }] }`
- `plain`: unsynchronized lyrics as a YAML literal block