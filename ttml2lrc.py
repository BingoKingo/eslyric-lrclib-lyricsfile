#!/usr/bin/env python3
"""ttml2lrc.py

Convert Apple Music / AMLL style TTML lyrics into standard LRC format.

The logic mirrors this project's Rust implementation
(`ttml_processor` and `lyrics_helper_core`):
- TTML time-string parsing (parser/utils.rs::parse_ttml_time_to_ms)
- Timing mode detection: line vs word (parser/handlers.rs::process_tt_start)
- Recursive <p> / <span> parsing, distinguishing main vocals,
  background vocals (x-bg), translations (x-translation) and
  romanizations (x-roman) (parser/body.rs)
- Normalized lyric data structures (converter/types.rs)

Depends only on the Python standard library (xml.etree.ElementTree).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional


def read_text(path: "Path | str") -> str:
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


@dataclass
class Syllable:
    text: str
    start_ms: int = 0
    end_ms: int = 0
    ends_with_space: bool = False


@dataclass
class Word:
    syllables: list[Syllable] = field(default_factory=list)


@dataclass
class LyricTrack:
    words: list[Word] = field(default_factory=list)
    lang: Optional[str] = None
    scheme: Optional[str] = None

    def text(self) -> str:
        out = []
        for word in self.words:
            for syl in word.syllables:
                if syl.ends_with_space:
                    out.append(syl.text + " ")
                else:
                    out.append(syl.text)
        return "".join(out).strip()

    def is_empty(self) -> bool:
        return all(len(w.syllables) == 0 for w in self.words)


@dataclass
class AnnotatedTrack:
    content_type: str = "main"  # "main" | "background"
    content: LyricTrack = field(default_factory=LyricTrack)
    translations: list[LyricTrack] = field(default_factory=list)
    romanizations: list[LyricTrack] = field(default_factory=list)


@dataclass
class LyricLine:
    start_ms: int = 0
    end_ms: int = 0
    tracks: list[AnnotatedTrack] = field(default_factory=list)
    agent: Optional[str] = None
    song_part: Optional[str] = None
    itunes_key: Optional[str] = None


@dataclass
class ParseResult:
    lines: list[LyricLine] = field(default_factory=list)
    metadata: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    is_line_timing: bool = False
    # Per-line translations / romanizations, keyed by itunes:key (Apple-style <iTunesMetadata>)
    line_translations: dict[str, list[tuple[str, Optional[str]]]] = field(default_factory=dict)
    line_romanizations: dict[str, list[tuple[str, Optional[str]]]] = field(default_factory=dict)


class InvalidTime(Exception):
    pass


def _parse_decimal_ms_part(ms_str: str, original: str) -> int:
    if ms_str == "" or len(ms_str) > 3 or not ms_str.isdigit():
        raise InvalidTime(
            f"Invalid millisecond part '{ms_str}' in timestamp '{original}' (max 3 digits)"
        )
    val = int(ms_str)
    return val * (10 ** (3 - len(ms_str)))


def _parse_seconds_and_decimal(seconds_and_ms: str, original: str) -> tuple[int, int]:
    if seconds_and_ms == "":
        raise InvalidTime(f"Empty seconds part in time format '{original}'")
    dot_parts = seconds_and_ms.split(".", 1)
    seconds_str = dot_parts[0]
    if seconds_str == "":
        raise InvalidTime(f"Empty seconds part in time format '{original}' (e.g. '.mmm')")
    seconds = int(seconds_str)
    milliseconds = _parse_decimal_ms_part(dot_parts[1], original) if len(dot_parts) > 1 else 0
    return seconds, milliseconds


def parse_ttml_time_to_ms(time_str: str) -> int:
    """Parse a TTML time string into milliseconds."""
    time_str = time_str.strip()
    if time_str == "":
        raise InvalidTime("Empty time string")

    if time_str.endswith("s"):
        stripped = time_str[:-1]
        if stripped == "" or stripped.startswith(".") or stripped.endswith("."):
            raise InvalidTime(f"Invalid seconds format in timestamp '{time_str}'")
        if stripped.startswith("-"):
            raise InvalidTime(f"Negative timestamp not allowed: '{time_str}'")
        seconds, milliseconds = _parse_seconds_and_decimal(stripped, time_str)
        return seconds * 1000 + milliseconds

    # Formats: "HH:MM:SS.mmm" / "MM:SS.mmm" / "SS.mmm"
    parts = time_str.split(":")[::-1]  # Reverse order: seconds, minutes, hours
    if len(parts) > 3:
        raise InvalidTime(f"Too many parts in time format '{time_str}'")

    current = parts[0]
    if current.startswith("-"):
        raise InvalidTime(f"Negative timestamp not allowed: '{time_str}'")
    seconds, milliseconds = _parse_seconds_and_decimal(current, time_str)
    total_ms = seconds * 1000 + milliseconds

    if len(parts) > 1:
        minutes = int(parts[1])
        if minutes >= 60:
            raise InvalidTime(f"Invalid minutes value '{minutes}' (must be < 60) in '{time_str}'")
        total_ms += minutes * 60_000

    if len(parts) > 2:
        hours = int(parts[2])
        total_ms += hours * 3_600_000

    # When a colon is present, the "SS" part must be < 60
    if time_str.count(":") > 0 and seconds >= 60:
        raise InvalidTime(f"Invalid seconds value '{seconds}' (must be < 60) in '{time_str}'")

    return total_ms


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def local_name(tag: str) -> str:
    """Strip the namespace prefix, mirroring Rust's `local_name()`.

    Handles both forms:
    - Braced namespace: ``{http://...}key`` -> ``key``
    - Colon prefix: ``itunes:key`` -> ``key``
    """
    return tag.split("}", 1)[-1].split(":", 1)[-1]


def get_attr(elem: ET.Element, *names: str) -> Optional[str]:
    """Get an attribute value by its (namespace-stripped) local name."""
    local_names = {local_name(n) for n in names}
    for key, value in elem.attrib.items():
        if local_name(key) in local_names:
            return value
    return None


ROLE_TRANSLATION = "x-translation"
ROLE_ROMAN = "x-roman"
ROLE_BACKGROUND = "x-bg"


class _PBuilder:
    """Accumulates the parse result of one <p> element (mirrors Rust's CurrentPElementData)."""

    def __init__(self, start_ms: int, end_ms: int):
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.main_syllables: list[Syllable] = []
        self.bg_syllables: list[Syllable] = []
        self.translations: list[LyricTrack] = []
        self.romanizations: list[LyricTrack] = []
        self.is_line_timing: bool = False
        # Word-timing mode: tracks preceding whitespace and stray text
        self._line_text_buffer = ""

    # -- Word-timing mode: add a syllable --
    def add_syllable(
        self,
        text: str,
        start_ms: int,
        end_ms: int,
        content_type: str,
        external_space: bool = False,
    ) -> None:
        target = self.bg_syllables if content_type == "background" else self.main_syllables
        # Leading space: mark the previous syllable as space-terminated
        if text[:1].isspace() and target and not target[-1].ends_with_space:
            target[-1].ends_with_space = True
        trimmed = text.strip()
        if trimmed == "":
            return
        syl = Syllable(
            text=trimmed,
            start_ms=start_ms,
            end_ms=max(end_ms, start_ms),
            ends_with_space=text[-1:].isspace() or external_space,
        )
        target.append(syl)

    def finalize(self) -> Optional[AnnotatedTrack]:
        if self.is_line_timing:
            line_text = normalize_whitespace(self._line_text_buffer)
            self._line_text_buffer = ""
            if line_text:
                syl = Syllable(
                    text=line_text,
                    start_ms=self.start_ms,
                    end_ms=self.end_ms,
                    ends_with_space=False,
                )
                self.main_syllables = [syl]

        if not self.main_syllables and not self.bg_syllables:
            return None

        main_track = LyricTrack(words=[Word(syllables=self.main_syllables)])
        bg_track = LyricTrack(words=[Word(syllables=self.bg_syllables)])

        tracks = []
        if not main_track.is_empty():
            tracks.append(
                AnnotatedTrack(
                    content_type="main",
                    content=main_track,
                    translations=self.translations,
                    romanizations=self.romanizations,
                )
            )
        if not bg_track.is_empty():
            tracks.append(AnnotatedTrack(content_type="background", content=bg_track))
        return tracks


def _walk_p(
    element: ET.Element,
    builder: _PBuilder,
    role_stack: list[str],
    p_start: int,
    p_end: int,
    warnings: list[str],
) -> None:
    """Recursively walk a <p>'s children to build syllables and auxiliary tracks."""
    is_bg = ROLE_BACKGROUND in role_stack

    # Read role / language / scheme carried by the start tag
    role = get_attr(element, "role", "ttm:role")
    lang = get_attr(element, "xml:lang")
    scheme = get_attr(element, "xml:scheme")
    begin_str = get_attr(element, "begin")
    end_str = get_attr(element, "end")

    begin = end = None
    if begin_str is not None:
        try:
            begin = parse_ttml_time_to_ms(begin_str)
        except InvalidTime as e:
            warnings.append(f"Failed to parse timestamp '{begin_str}' ({e}); ignored.")
    if end_str is not None:
        try:
            end = parse_ttml_time_to_ms(end_str)
        except InvalidTime as e:
            warnings.append(f"Failed to parse timestamp '{end_str}' ({e}); ignored.")

    new_role_stack = role_stack
    if role in (ROLE_TRANSLATION, ROLE_ROMAN, ROLE_BACKGROUND):
        new_role_stack = role_stack + [role]

    # Process child elements first (recursion)
    # Translation / romanization spans: not part of main lyrics, stored separately
    if role in (ROLE_TRANSLATION, ROLE_ROMAN):
        text = normalize_whitespace(_collect_text(element))
        if text:
            track = LyricTrack(lang=lang, scheme=scheme)
            track.words = [Word(syllables=[Syllable(text=text)])]
            if role == ROLE_TRANSLATION:
                builder.translations.append(track)
            else:
                builder.romanizations.append(track)
        return

    for child in element:
        _walk_p(child, builder, new_role_stack, p_start, p_end, warnings)
        # In word mode, pure-whitespace text between siblings counts as a space
        if not builder.is_line_timing and child.tail:
            if child.tail.strip() == "" and child.tail.strip() != child.tail:
                # Contains whitespace (not just newlines) -> add space after previous syllable
                target = builder.bg_syllables if is_bg else builder.main_syllables
                if target and not target[-1].ends_with_space:
                    target[-1].ends_with_space = True

    # Handle this element's own text node
    text = element.text or ""
    if builder.is_line_timing:
        builder._line_text_buffer += text
    else:
        if text.strip():
            if role is None or role == ROLE_BACKGROUND:
                # Direct text of main / background (falls back to <p> time when no begin/end)
                s = begin if begin is not None else p_start
                e = end if end is not None else p_end
                ct = "background" if is_bg else "main"
                builder.add_syllable(text, s, e, ct)
            elif role in (ROLE_TRANSLATION, ROLE_ROMAN):
                # Translation/romanization text already handled in _collect_text; ignore here
                pass

    # Handle tail text (top-level <p> tail is not lyric content)
    return


def _collect_text(element: ET.Element) -> str:
    """Collect all text of an element and its descendants."""
    parts = [element.text or ""]
    for child in element:
        parts.append(_collect_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _has_timed_span(root: ET.Element) -> bool:
    for el in root.iter():
        if local_name(el.tag) == "span" and (
            get_attr(el, "begin") is not None or get_attr(el, "end") is not None
        ):
            return True
    return False


def parse_ttml(content: str) -> ParseResult:
    """Parse TTML content and return a ParseResult."""
    result = ParseResult()

    # Namespaces stay on tags during iteration; compare via local_name
    root = ET.fromstring(content)

    # ---- timing mode detection ----
    timing_attr = get_attr(root, "itunes:timing")
    if timing_attr == "line":
        is_line_timing = True
    elif not _has_timed_span(root):
        is_line_timing = True
        result.warnings.append(
            "No timed <span> found and no itunes:timing mode set; falling back to line timing."
        )
    else:
        is_line_timing = False
    result.is_line_timing = is_line_timing

    # ---- metadata ----
    _parse_metadata(root, result)
    _parse_itunes_metadata(root, result)

    # ---- body / div / p ----
    for p in root.iter():
        if local_name(p.tag) != "p":
            continue
        begin_str = get_attr(p, "begin")
        end_str = get_attr(p, "end")
        try:
            start_ms = parse_ttml_time_to_ms(begin_str) if begin_str else 0
        except InvalidTime as e:
            result.warnings.append(f"Failed to parse line start time '{begin_str}' ({e})")
            start_ms = 0
        try:
            end_ms = parse_ttml_time_to_ms(end_str) if end_str else 0
        except InvalidTime as e:
            result.warnings.append(f"Failed to parse line end time '{end_str}' ({e})")
            end_ms = 0

        agent = get_attr(p, "agent", "ttm:agent")
        song_part = get_attr(p, "itunes:song-part", "itunes:songPart")
        itunes_key = get_attr(p, "itunes:key")

        builder = _PBuilder(start_ms, end_ms)
        builder.is_line_timing = is_line_timing
        builder._line_text_buffer = ""

        _walk_p(p, builder, [], start_ms, end_ms, result.warnings)
        tracks = builder.finalize()
        if tracks:
            line = LyricLine(
                start_ms=start_ms,
                end_ms=end_ms,
                tracks=tracks,
                agent=agent,
                song_part=song_part,
                itunes_key=itunes_key,
            )
            _attach_itunes_aux(line, result)
            result.lines.append(line)

    result.lines.sort(key=lambda line: (line.start_ms, line.end_ms))
    return result


def _attach_itunes_aux(line: LyricLine, result: ParseResult) -> None:
    """Attach per-line translations / romanizations from <iTunesMetadata> (keyed by itunes:key)."""
    if line.itunes_key is None:
        return
    main_track = next((t for t in line.tracks if t.content_type == "main"), None)
    if main_track is None:
        return
    for text, lang in result.line_translations.get(line.itunes_key, []):
        main_track.translations.append(
            LyricTrack(lang=lang, words=[Word(syllables=[Syllable(text=text)])]))
    for text, lang in result.line_romanizations.get(line.itunes_key, []):
        main_track.romanizations.append(
            LyricTrack(lang=lang, words=[Word(syllables=[Syllable(text=text)])]))


def _parse_itunes_metadata(root: ET.Element, result: ParseResult) -> None:
    """Parse Apple-style <iTunesMetadata> per-line translations and romanizations.

    Translations/romanizations are keyed by itunes:key.
    """
    iTunes_md = None
    for el in root.iter():
        if local_name(el.tag) == "iTunesMetadata":
            iTunes_md = el
            break
    if iTunes_md is None:
        return

    for container in iTunes_md:
        cname = local_name(container.tag)
        if cname == "translations":
            aux_map = result.line_translations
        elif cname == "transliterations":
            aux_map = result.line_romanizations
        else:
            continue
        for translation in container:
            if local_name(translation.tag) not in ("translation", "transliteration"):
                continue
            lang = get_attr(translation, "xml:lang")
            for text_el in translation:
                if local_name(text_el.tag) != "text":
                    continue
                key = get_attr(text_el, "for")
                value = normalize_whitespace(text_el.text or "")
                if key and value:
                    aux_map.setdefault(key, []).append((value, lang))


def _parse_metadata(root: ET.Element, result: ParseResult) -> None:
    for meta in root.iter():
        if local_name(meta.tag) not in ("meta", "amll:meta"):
            continue
        key = get_attr(meta, "key")
        if not key:
            continue
        value = get_attr(meta, "value")
        if value is None:
            value = (meta.text or "").strip()
        if value:
            result.metadata.setdefault(key, []).append(value)

    # xml:lang used as language metadata
    lang = get_attr(root, "xml:lang")
    if lang:
        result.metadata.setdefault("Language", []).append(lang)


def format_lrc_time(ms: int) -> str:
    """Format milliseconds as [mm:ss.xx] (centiseconds)."""
    ms = max(0, int(ms))
    minutes = ms // 60_000
    seconds = (ms % 60_000) // 1000
    centi = (ms % 1000) // 10
    return f"[{minutes:02d}:{seconds:02d}.{centi:02d}]"


def _format_ms3(ms: int) -> str:
    """Format milliseconds with 3-digit precision: [mm:ss.xxx]."""
    ms = max(0, int(ms))
    minutes = ms // 60_000
    seconds = (ms % 60_000) // 1000
    milli = ms % 1000
    return f"[{minutes:02d}:{seconds:02d}.{milli:03d}]"


def _format_ms3_inline(ms: int) -> str:
    """Format milliseconds with 3-digit precision for inline syllable tags: <mm:ss.xxx>."""
    ms = max(0, int(ms))
    minutes = ms // 60_000
    seconds = (ms % 60_000) // 1000
    milli = ms % 1000
    return f"<{minutes:02d}:{seconds:02d}.{milli:03d}>"


def _syllables_of(track: LyricTrack) -> list[Syllable]:
    return [s for w in track.words for s in w.syllables]


def _is_word_timed(syllables: list[Syllable]) -> bool:
    """A track is word-timed when it has more than one syllable with real timing."""
    if len(syllables) <= 1:
        return False
    return any(s.end_ms > s.start_ms for s in syllables)


def _format_enhanced(line_start_ms: int, line_end_ms: int, syllables: list[Syllable]) -> str:
    """Render a word-timed line in Enhanced LRC: [line]<syl>text..<line_end>."""
    if not syllables:
        return ""
    line_start = line_start_ms or syllables[0].start_ms
    # The trailing tag must carry a real end time. When the source TTML omits
    # the <p> end attribute (common for the very last line), line_end_ms is 0
    # and would emit a spurious "<00:00.000>" tag; fall back to the last
    # syllable's own end time so the line always terminates correctly.
    if line_end_ms and line_end_ms > line_start:
        line_end = line_end_ms
    else:
        line_end = max((s.end_ms for s in syllables), default=line_start)
    parts = [_format_ms3(line_start)]
    for s in syllables:
        parts.append(f"{_format_ms3_inline(s.start_ms)}{s.text}")
        if s.ends_with_space:
            parts.append(" ")
    # Trailing tag carries the line's end time (matches test_eslyric.lrc style)
    parts.append(_format_ms3_inline(line_end))
    return "".join(parts)


def _compute_lyric_end(result: ParseResult) -> int:
    """Return the latest end time across all lines/syllables (the lyric end)."""
    end = 0
    for line in result.lines:
        candidates = [line.end_ms]
        for t in line.tracks:
            for s in _syllables_of(t.content):
                candidates.append(s.end_ms)
        line_end = max(candidates)
        if line_end > end:
            end = line_end
    return end


def _metadata_header(result: ParseResult) -> list[str]:
    lines: list[str] = []
    md = result.metadata

    titles = md.get("musicName") or md.get("title") or md.get("ti")
    if titles:
        lines.append(f"[ti:{titles[0]}]")

    artists = md.get("artists") or md.get("artist") or md.get("ar")
    if artists:
        lines.append(f"[ar:{'/'.join(artists)}]")

    albums = md.get("album") or md.get("al")
    if albums:
        lines.append(f"[al:{albums[0]}]")

    lang = md.get("Language")
    if lang:
        lines.append(f"[language:{lang[0]}]")

    return lines


def generate_lrc(
    result: ParseResult,
    include_translation: bool = True,
    include_romanization: bool = False,
    include_background: bool = True,
    enhanced: bool = True,
) -> str:
    out: list[str] = []
    out.extend(_metadata_header(result))
    if out:
        out.append("")

    for line in result.lines:
        main_track = next((t for t in line.tracks if t.content_type == "main"), None)
        if main_track is None:
            continue
        main_syllables = _syllables_of(main_track.content)
        main_text = main_track.content.text()
        if not main_text:
            continue

        if enhanced and _is_word_timed(main_syllables):
            out.append(_format_enhanced(line.start_ms, line.end_ms, main_syllables))
        else:
            out.append(f"{format_lrc_time(line.start_ms)}{main_text}")

        if include_background:
            bg_track = next(
                (t for t in line.tracks if t.content_type == "background"), None
            )
            if bg_track is not None:
                bg_syllables = _syllables_of(bg_track.content)
                bg_text = bg_track.content.text()
                if bg_text:
                    if enhanced and _is_word_timed(bg_syllables):
                        out.append(_format_enhanced(line.start_ms, line.end_ms, bg_syllables))
                    else:
                        out.append(f"{format_lrc_time(line.start_ms)}{bg_text}")

        if include_translation:
            for tr in main_track.translations:
                tr_text = tr.text()
                if tr_text:
                    out.append(f"{format_lrc_time(line.start_ms)}{tr_text}")

        if include_romanization:
            for rom in main_track.romanizations:
                rom_text = rom.text()
                if rom_text:
                    out.append(f"{format_lrc_time(line.start_ms)}{rom_text}")

    if result.lines:
        out.append(_format_ms3(_compute_lyric_end(result)))

    return "\n".join(out) + ("\n" if out else "")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert TTML lyrics into Enhanced/word-timed LRC "
            "(mirrors this project's lyrics_helper logic)."
        )
    )
    parser.add_argument("input", help="Path to the input TTML file")
    parser.add_argument(
        "--translation",
        action="store_true",
        help="Include translation lines (disabled by default)",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Include background vocal lines (disabled by default)",
    )
    parser.add_argument(
        "--romanization",
        action="store_true",
        help="Include romanization lines",
    )
    parser.add_argument(
        "--print-warnings",
        action="store_true",
        help="Print parsing warnings to stderr",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Directory for the generated .lrc (default: next to the input file)",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input).expanduser().resolve()
    try:
        content = read_text(input_path)
    except OSError as e:
        print(f"Could not read input file: {e}", file=sys.stderr)
        return 1

    try:
        result = parse_ttml(content)
    except ET.ParseError as e:
        print(f"XML parse failed: {e}", file=sys.stderr)
        return 1

    if args.print_warnings and result.warnings:
        for w in result.warnings:
            print(f"Warning: {w}", file=sys.stderr)

    lrc = generate_lrc(
        result,
        include_translation=args.translation,
        include_background=args.background,
        include_romanization=args.romanization,
        enhanced=True,
    )

    if args.output_dir:
        out_dir = Path(args.output_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / (input_path.stem + ".lrc")
    else:
        # Default: write next to the source input file.
        output_path = input_path.with_suffix(".lrc")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(lrc)
    print(f"Wrote {output_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
