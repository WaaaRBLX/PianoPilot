#!/usr/bin/env python3
"""
Roblox Piano Auto Player  -  phase 1 (core engine)

Plays MIDI files (.mid) on Roblox pianos by pressing the keys for you,
using the standard 61-key Roblox / Virtual Piano key layout.

HOW TO RUN (Windows)
  1. Put this file and your .mid files in the same folder.
  2. Open that folder, click the address bar at the top, type  cmd  and
     press Enter. A black command window opens in that folder.
  3. Type one of these and press Enter:
        python piano_player.py              (pick a song from a window)
        python piano_player.py song.mid     (play a specific song)
        python piano_player.py --test       (short calibration test)
  4. Click into Roblox and sit at the piano. Playback starts 3 seconds
     after Roblox is the active window.

CONTROLS WHILE PLAYING
  F6   pause / resume
  F7   stop
  Clicking away from Roblox pauses automatically, and it picks back up
  a second after you click back in.

OPTIONS (add after the file name)
  --speed 1.25      play 25% faster   (0.5 = half speed)
  --transpose -2    move every note down 2 semitones (+3 = up 3)
  --tracks 1,3      only play these tracks (see --info for the list)
  --info            show what's inside a MIDI file, then exit
  --dry-run         show the keys it would press, without pressing any
  --test            play the calibration test instead of a song
  --pedal song      sustain pedal: off, song (from the MIDI), auto, always
  --pedal-method tap   how the pedal key works: hold (default), tap, notes
  --range 88        notes outside the piano: octave (default), skip, 88
  --max-notes 4     most keys pressed at once (default: no limit)
  --style legato    note length: real (default), short, legato
  --no-cleanup      play the file exactly, without merging duplicate notes
  sheet.txt --bpm 100   play a text piano sheet ([tu] y o | p) at 100 BPM
  --export-sheet    print a song as a text sheet instead of playing it

Nothing to install: this only uses Python's built-in modules.
"""

import argparse
import bisect
import gc
import math
import ntpath
import os
import queue
import random
import struct
import sys
import time
from dataclasses import dataclass, field

# ============================ SETTINGS ============================
# Safe to change. Save the file and run it again after editing.

COUNTDOWN_SECONDS = 3        # wait this long after Roblox is focused
PAUSE_KEY = "F6"             # pause / resume
STOP_KEY = "F7"              # stop
AUTO_PAUSE_WHEN_UNFOCUSED = True   # never type into other windows

# Black keys need Shift. If black keys come out as the wrong (white)
# notes in your piano, raise this to about 20.
SHIFT_GAP_MS = 0

CHORD_WINDOW_MS = 4          # notes starting this close together = one chord
MIN_HOLD_MS = 20             # shortest time a key is held down
SKIP_DRUMS = True            # ignore the MIDI drum channel

# Key layout of a 61-key Roblox piano, lowest note first.
# Uppercase letters and the symbols ! @ $ % ^ * ( are black keys (Shift).
KEY_LAYOUT = "1!2@34$5%6^78*9(0qQwWeErtTyYuiIoOpPasSdDfgGhHjJklLzZxcCvVbBnm"
LOWEST_NOTE = 36             # MIDI note of the "1" key ("t" = middle C)
# ==================================================================

HIGHEST_NOTE = LOWEST_NOTE + len(KEY_LAYOUT) - 1
SHIFT_SYMBOLS = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
                 "&": "7", "*": "8", "(": "9", ")": "0"}
SHIFTED_LOOK = {v: k for k, v in SHIFT_SYMBOLS.items()}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(pitch):
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}"


def fold_into_range(pitch):
    """Move a note by whole octaves until it fits on the piano."""
    while pitch < LOWEST_NOTE:
        pitch += 12
    while pitch > HIGHEST_NOTE:
        pitch -= 12
    return pitch


def key_for_pitch(pitch):
    """Return (physical key, needs_shift) for an in-range MIDI note."""
    char = KEY_LAYOUT[pitch - LOWEST_NOTE]
    if char in SHIFT_SYMBOLS:
        return SHIFT_SYMBOLS[char], True
    if char.isupper():
        return char.lower(), True
    return char, False


EXTRA_MARK = "~"          # sheet prefix for the 88-key extras (Ctrl + key)


def key_label(key, modifier):
    """How a key looks on a piano sheet: black keys shifted (T, !), 88-key extras as ~key.
    ("^" can't mark them: it's a real key, Shift+6.)"""
    if not modifier:
        return key
    if modifier == "ctrl":
        return EXTRA_MARK + key
    return SHIFTED_LOOK.get(key, key.upper())


def pitch_for_label(label):
    """Inverse of key_label: the MIDI pitch a sheet label plays (or None)."""
    if label.startswith(EXTRA_MARK) and len(label) == 2:
        k = label[1]
        if k in LOW_88_KEYS:
            return LOWEST_88 + LOW_88_KEYS.index(k)
        if k in HIGH_88_KEYS:
            return HIGHEST_NOTE + 1 + HIGH_88_KEYS.index(k)
        return None
    i = KEY_LAYOUT.find(label)
    return LOWEST_NOTE + i if i >= 0 else None


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


# ========================== MIDI READER ===========================

AUDIO_MARK = "PianoPilot audio import"     # text the audio converter writes into its MIDI files


class MidiError(Exception):
    pass


@dataclass
class Note:
    start: float      # seconds
    end: float        # seconds
    pitch: int
    velocity: int
    channel: int      # 0-15 (9 = drums)
    track: int        # 1-based


@dataclass
class TrackInfo:
    number: int
    name: str
    notes: int = 0
    channels: set = field(default_factory=set)
    low: int = 127
    high: int = 0


@dataclass
class Song:
    name: str
    notes: list
    pedal: list       # (seconds, channel, track, value) for sustain pedal
    tracks: list
    duration: float
    tempo_bpm: float = 0.0      # the file's starting tempo (0 when it has none)
    tempo_changes: int = 0
    source: str = ""            # "audio" when it was made from a recording (MP3 to MIDI)


_DATA_BYTES = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}


def _read_varlen(data, pos, end):
    value = 0
    for _ in range(4):
        if pos >= end:
            raise MidiError("track ended in the middle of a number")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, pos
    raise MidiError("bad variable-length number")


def _parse_track(data, pos, end):
    """Return (name, events, last_tick). Tolerates common file errors."""
    events = []
    name = ""
    tick = 0
    running = None
    while pos < end:
        try:
            delta, pos = _read_varlen(data, pos, end)
        except MidiError:
            break
        tick += delta
        if pos >= end:
            break
        status = data[pos]

        if status == 0xFF:                      # meta event
            if pos + 1 >= end:
                break
            mtype = data[pos + 1]
            try:
                length, p = _read_varlen(data, pos + 2, end)
            except MidiError:
                break
            payload = data[p:p + length]
            pos = p + length
            if mtype == 0x51 and length == 3:
                events.append((tick, "tempo", int.from_bytes(payload, "big")))
            elif mtype == 0x01 and AUDIO_MARK.encode("ascii") in payload:
                events.append((tick, "mark", "audio"))
            elif mtype == 0x03 and not name:
                try:
                    name = payload.decode("utf-8").strip(" \x00")
                except UnicodeDecodeError:
                    name = payload.decode("latin-1", "replace").strip(" \x00")
            elif mtype == 0x2F:
                break
            continue

        if status in (0xF0, 0xF7):              # sysex: skip it
            try:
                length, p = _read_varlen(data, pos + 1, end)
            except MidiError:
                break
            pos = p + length
            continue

        if status >= 0x80:
            pos += 1
            if status >= 0xF0:                  # stray system message
                pos += {0xF1: 1, 0xF2: 2, 0xF3: 1}.get(status, 0)
                continue
            running = status
        elif running is None:
            pos += 1                            # garbage byte, skip
            continue
        else:
            status = running                    # running status

        count = _DATA_BYTES[status & 0xF0]
        if pos + count > end:
            break
        d1 = data[pos]
        d2 = data[pos + 1] if count == 2 else 0
        pos += count
        kind, channel = status & 0xF0, status & 0x0F
        if kind == 0x90 and d2 > 0:
            events.append((tick, "on", channel, d1, d2))
        elif kind == 0x80 or kind == 0x90:
            events.append((tick, "off", channel, d1))
        elif kind == 0xB0:
            events.append((tick, "cc", channel, d1, d2))
    return name, events, tick


def _tempo_map(tempos, tpq):
    """(ticks -> seconds function, starting BPM, number of tempo changes) for these tempo events."""
    seg_ticks, seg_secs, seg_tempo = [0], [0.0], [500000]
    for t, tempo in sorted(tempos, key=lambda x: x[0]):   # stable: same tick keeps file order
        if tempo <= 0:
            continue
        if t == seg_ticks[-1]:
            seg_tempo[-1] = tempo
            continue
        seg_secs.append(seg_secs[-1] + (t - seg_ticks[-1]) * seg_tempo[-1] / 1e6 / tpq)
        seg_ticks.append(t)
        seg_tempo.append(tempo)

    def to_sec(t):
        i = bisect.bisect_right(seg_ticks, t) - 1
        return seg_secs[i] + (t - seg_ticks[i]) * seg_tempo[i] / 1e6 / tpq
    return to_sec, round(60e6 / seg_tempo[0], 1), len(seg_tempo) - 1


def load_midi(path):
    with open(path, "rb") as f:
        data = f.read()
    start = data.find(b"MThd")
    if start < 0 or start + 14 > len(data):
        raise MidiError("This isn't a MIDI file (no MIDI header found).")
    header_len = struct.unpack_from(">I", data, start + 4)[0]
    _fmt, _ntracks, division = struct.unpack_from(">HHH", data, start + 8)
    pos = start + 8 + header_len

    raw_tracks = []
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        chunk_len = struct.unpack_from(">I", data, pos + 4)[0]
        pos += 8
        if chunk_id == b"MTrk":
            raw_tracks.append(_parse_track(data, pos, min(pos + chunk_len, len(data))))
        elif not chunk_id.isalnum():
            break                               # not a real chunk, stop
        pos += chunk_len
    if not raw_tracks:
        raise MidiError("No tracks found in this MIDI file.")

    # --- ticks -> seconds, following every tempo change ---
    if division & 0x8000:                       # SMPTE timing (rare)
        ticks_per_frame = division & 0xFF
        if not ticks_per_frame:
            raise MidiError("This MIDI file's timing information is broken (0 ticks per frame).")
        fps = 256 - (division >> 8)
        fps = 29.97 if fps == 29 else fps
        per_tick = 1.0 / (fps * ticks_per_frame)

        def to_sec(t):
            return t * per_tick
        clocks = [to_sec] * len(raw_tracks)
        offsets = [0.0] * len(raw_tracks)
        start_bpm, changes = 0.0, 0
    else:
        tpq = division or 480

        def tempo_events(tracks):
            return [(ev[0], ev[2]) for _n, evs, _l in tracks for ev in evs if ev[1] == "tempo"]
        if _fmt == 2 and len(raw_tracks) > 1:
            # Format 2: each track is its own piece with its own tempo. Play them one after another.
            clocks, offsets, at = [], [], 0.0
            for track in raw_tracks:
                clock, _bpm, _changes = _tempo_map(tempo_events([track]), tpq)
                clocks.append(clock)
                offsets.append(at)
                at += clock(track[2])
            _clock, start_bpm, changes = _tempo_map(tempo_events(raw_tracks[:1]), tpq)
        else:
            clock, start_bpm, changes = _tempo_map(tempo_events(raw_tracks), tpq)
            clocks = [clock] * len(raw_tracks)
            offsets = [0.0] * len(raw_tracks)

    # --- pair note-ons with note-offs ---
    notes, pedal, infos = [], [], []
    for number, (name, events, last_tick) in enumerate(raw_tracks, start=1):
        clock, offset = clocks[number - 1], offsets[number - 1]

        def to_sec(t, clock=clock, offset=offset):
            return clock(t) + offset
        info = TrackInfo(number, name or f"Track {number}")
        open_notes = {}
        found = []
        for ev in events:
            if ev[1] == "on":
                open_notes.setdefault((ev[2], ev[3]), []).append((ev[0], ev[4]))
            elif ev[1] == "off":
                waiting = open_notes.get((ev[2], ev[3]))
                if waiting:
                    on_tick, vel = waiting.pop(0)
                    found.append((on_tick, ev[0], ev[3], vel, ev[2]))
            elif ev[1] == "cc" and ev[3] == 64:
                pedal.append((to_sec(ev[0]), ev[2], number, ev[4]))
        for (channel, pitch), waiting in open_notes.items():
            for on_tick, vel in waiting:             # never released: end at track end
                found.append((on_tick, max(last_tick, on_tick), pitch, vel, channel))
        for on_tick, off_tick, pitch, vel, channel in found:
            notes.append(Note(to_sec(on_tick), to_sec(off_tick), pitch, vel, channel, number))
            info.notes += 1
            info.channels.add(channel)
            info.low = min(info.low, pitch)
            info.high = max(info.high, pitch)
        infos.append(info)

    notes.sort(key=lambda n: (n.start, n.pitch))
    pedal.sort(key=lambda p: p[0])
    duration = max((n.end for n in notes), default=0.0)
    song_name = os.path.splitext(os.path.basename(path))[0]
    from_audio = any(ev[1] == "mark" for _n, evs, _l in raw_tracks for ev in evs) or \
        any(name == "Pianoscribe Piano" for name, _e, _l in raw_tracks)       # older Pianoscribe files
    return Song(song_name, notes, pedal, infos, duration, start_bpm, changes, "audio" if from_audio else "")


# ================== LAYOUT, SHEETS, SONG DETAILS ===================

STANDARD_LAYOUT = KEY_LAYOUT
STANDARD_LOWEST = LOWEST_NOTE


def check_layout(layout, lowest):
    """None if this key layout can be played, otherwise a sentence saying what's wrong."""
    if not isinstance(layout, str) or len(layout) < 12:
        return "A layout needs at least 12 keys."
    if len(layout) > 88:
        return "A layout can have at most 88 keys."
    seen = set()
    for ch in layout:
        if ch in SHIFT_SYMBOLS:
            combo = (SHIFT_SYMBOLS[ch], True)
        elif ch.isascii() and (ch.isdigit() or ch.islower()):
            combo = (ch, False)
        elif ch.isascii() and ch.isupper():
            combo = (ch.lower(), True)
        else:
            return f"“{ch}” isn't a key PianoPilot can press. Use letters, numbers and ! @ # $ % ^ & * ( )."
        if combo in seen:
            return f"“{ch}” is used twice."
        seen.add(combo)
    if not isinstance(lowest, int) or not 12 <= lowest <= 108 - len(layout) + 1:
        return "The lowest note must be between 12 and 108."
    return None


def set_layout(layout=None, lowest=None):
    """Use a different key layout (None restores the standard Roblox layout)."""
    global KEY_LAYOUT, LOWEST_NOTE, HIGHEST_NOTE
    layout = STANDARD_LAYOUT if layout is None else layout
    lowest = STANDARD_LOWEST if lowest is None else lowest
    problem = check_layout(layout, lowest)
    if problem:
        raise ValueError(problem)
    KEY_LAYOUT, LOWEST_NOTE = layout, lowest
    HIGHEST_NOTE = LOWEST_NOTE + len(KEY_LAYOUT) - 1


def is_standard_layout():
    return KEY_LAYOUT == STANDARD_LAYOUT and LOWEST_NOTE == STANDARD_LOWEST


MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def estimate_key(notes):
    """Best-matching key (Krumhansl-Kessler), e.g. "A minor", or "" if unclear."""
    weights = [0.0] * 12
    for n in notes:
        weights[n.pitch % 12] += max(0.05, min(2.0, n.end - n.start))
    if sum(weights) == 0:
        return ""

    def corr(a, b):
        ma, mb = sum(a) / 12, sum(b) / 12
        num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
        return num / den if den else 0.0
    best = max(((corr(weights, MAJOR_PROFILE[-k:] + MAJOR_PROFILE[:-k]), k, "major") for k in range(12)),
               key=lambda x: x[0])
    best = max(best, *((corr(weights, MINOR_PROFILE[-k:] + MINOR_PROFILE[:-k]), k, "minor") for k in range(12)),
               key=lambda x: x[0])
    return f"{NOTE_NAMES[best[1]]} {best[2]}" if best[0] > 0.3 else ""


def guess_hands(song):
    """{track number: "right hand" / "left hand" / "both hands"} from how high each track sits."""
    tracks = [t for t in song.tracks if t.notes and t.channels != {9}]
    if len(tracks) == 1:
        return {tracks[0].number: "both hands"}
    avg = {}
    for n in song.notes:
        if n.channel != 9:
            total, count = avg.get(n.track, (0, 0))
            avg[n.track] = (total + n.pitch, count + 1)
    means = {t: total / count for t, (total, count) in avg.items() if count}
    if not means:
        return {}
    ordered = sorted(means, key=means.get)
    out = {t: ("left hand" if means[t] < 60 else "right hand") for t in ordered}
    out[ordered[-1]] = "right hand"
    if len(ordered) > 1:
        out[ordered[0]] = "left hand"
    return out


def song_details(song):
    return {"bpm": song.tempo_bpm, "tempo_changes": song.tempo_changes,
            "key": estimate_key([n for n in song.notes if n.channel != 9]),
            "hands": {str(k): v for k, v in guess_hands(song).items()}}


# ---- text piano sheets (the Virtual Piano style people share for Roblox) ----

def _label_to_pitch(label):
    """Pitch for a sheet character in the current layout, or None."""
    if label.startswith(EXTRA_MARK) and len(label) == 2:
        return pitch_for_label(label) if is_standard_layout() else None
    i = KEY_LAYOUT.find(label)
    return LOWEST_NOTE + i if i >= 0 else None


def parse_sheet(text, bpm=120):
    """Turn a text sheet into notes.

    Rules: a letter is a note; [abc] is a chord; [a b c] is a quick roll; notes written together
    (asdf) are twice as fast as notes with spaces (a s d f); | or - adds a pause. Lines starting
    with "# " are comments. Returns (notes, problems)."""
    beat = 60.0 / max(20.0, min(400.0, float(bpm)))
    tokens = []           # (labels, roll, joined_to_next) or ("rest", 1)
    unknown = {}
    lines = [ln for ln in str(text).replace("\r", "").split("\n") if not ln.lstrip().startswith("# ")]
    src = "\n".join(lines)
    i = 0
    while i < len(src):
        ch = src[i]
        if ch in " \t\n":
            i += 1
            continue
        if ch in "|-":
            tokens.append(("rest", 1.0))
            i += 1
            continue
        if ch == "[":
            close = src.find("]", i + 1)
            if close < 0:
                unknown["["] = unknown.get("[", 0) + 1
                i += 1
                continue
            inner = src[i + 1:close]
            roll = " " in inner.strip()
            labels, j = [], 0
            body = inner.replace(" ", "")
            while j < len(body):
                if body[j] == EXTRA_MARK and j + 1 < len(body):
                    labels.append(body[j:j + 2])
                    j += 2
                else:
                    labels.append(body[j])
                    j += 1
            i = close + 1
            tokens.append(("notes", labels, roll, i < len(src) and src[i] not in " \t\n|-"))
            continue
        if ch == EXTRA_MARK and i + 1 < len(src) and src[i + 1] not in " \t\n":
            label = src[i:i + 2]
            i += 2
        else:
            label = ch
            i += 1
        tokens.append(("notes", [label], False, i < len(src) and src[i] not in " \t\n|-"))

    notes, t = [], 0.0
    for tok in tokens:
        if tok[0] == "rest":
            t += tok[1]
            continue
        _kind, labels, roll, joined = tok
        step = 0.5 if joined else 1.0
        pitches = []
        for label in labels:
            p = _label_to_pitch(label)
            if p is None:
                unknown[label] = unknown.get(label, 0) + 1
            else:
                pitches.append(p)
        for k, p in enumerate(pitches):
            start = (t + (0.1 * k if roll else 0.0)) * beat
            notes.append(Note(start, start + step * beat * 0.92, p, 90, 0, 1))
        t += step
    problems = []
    if unknown:
        listed = ", ".join(f"“{k}”" for k in list(unknown)[:8])
        count = sum(unknown.values())
        problems.append(f"{count} {'character was' if count == 1 else 'characters were'} skipped because "
                        f"{'it isn’t a piano key' if count == 1 else 'they aren’t piano keys'}: {listed}.")
    return notes, problems


def export_sheet(song, transpose=0, tracks=None, range_mode="octave", line_tokens=24):
    """Write a song as a text sheet. Returns (text, bpm)."""
    chosen = [n for n in playable_notes(song, tracks)]
    labelled = []
    for n in chosen:
        mapped = map_pitch(n.pitch + transpose, range_mode if is_standard_layout() else "octave")
        if mapped is None:
            continue
        key, mod, _moved = mapped
        labelled.append((n.start, n.pitch + transpose, key_label(key, mod)))
    if not labelled:
        return "", 120
    groups = []
    for start, pitch, label in sorted(labelled):
        if groups and start - groups[-1][0] <= 0.03:
            if label not in [lb for _p, lb in groups[-1][1]]:
                groups[-1][1].append((pitch, label))
        else:
            groups.append([start, [(pitch, label)]])
    gaps = sorted(b[0] - a[0] for a, b in zip(groups, groups[1:]) if b[0] - a[0] > 0.04)
    unit = gaps[len(gaps) // 2] if gaps else 0.5
    unit = min(1.5, max(0.1, unit))
    if 60.0 / unit > 240:          # very fast: write the quick notes together (asdf) at half the tempo
        unit *= 2
    bpm = round(60.0 / unit)
    out, line_count = [], 0
    for k, (start, members) in enumerate(groups):
        members.sort()
        labels = [lb for _p, lb in members]
        out.append(labels[0] if len(labels) == 1 else "[" + "".join(labels) + "]")
        line_count += 1
        if k + 1 == len(groups):
            break
        ratio = (groups[k + 1][0] - start) / unit
        if ratio < 0.75:
            sep = ""
        elif ratio < 1.5:
            sep = " "
        else:
            sep = " " + "| " * min(4, int(round(ratio - 1)))
        if line_count >= line_tokens and sep:
            sep = sep.rstrip() + "\n"
            line_count = 0
        out.append(sep)
    return "".join(out).strip() + "\n", bpm


def write_midi_file(notes, path, title="Song", bpm=120):
    """Save notes as a standard MIDI file (one track)."""
    tpq = 480
    tempo_us = int(round(60e6 / max(20.0, min(400.0, float(bpm)))))
    tick = lambda sec: int(round(max(0.0, sec) * tpq * 1e6 / tempo_us))
    msgs = []
    for n in notes:
        on = tick(n.start)
        msgs.append((on, 1, bytes([0x90, n.pitch & 0x7F, max(1, min(127, n.velocity))])))
        msgs.append((max(on + 1, tick(n.end)), 0, bytes([0x80, n.pitch & 0x7F, 0])))
    msgs.sort(key=lambda m: (m[0], m[1]))

    def varlen(v):
        out = [v & 0x7F]
        v >>= 7
        while v:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        return bytes(reversed(out))
    name = str(title).encode("utf-8")[:120]
    body = bytearray(b"\x00\xFF\x03" + varlen(len(name)) + name)
    body += b"\x00\xFF\x51\x03" + tempo_us.to_bytes(3, "big") + b"\x00\xC0\x00"
    last = 0
    for t, _o, m in msgs:
        body += varlen(t - last) + m
        last = t
    body += b"\x00\xFF\x2F\x00"
    data = b"MThd" + struct.pack(">IHHH", 6, 0, 1, tpq) + b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
    tmp = str(path) + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ========================= PLAYBACK PLAN ==========================

# 88-key mode: the extra notes below and above the 61-key layout, played with Ctrl held.
LOW_88_KEYS = "1234567890qwert"      # MIDI 21-35 (A0 to B1), Ctrl + key
HIGH_88_KEYS = "yuiopasdfghj"        # MIDI 97-108 (C#7 to C8), Ctrl + key
LOWEST_88, HIGHEST_88 = 21, 108


@dataclass
class PlayOptions:
    """How a song is turned into key presses. Defaults match the original behaviour."""
    range_mode: str = "octave"       # notes outside the piano: octave | skip | 88
    max_notes: int = 0               # most keys pressed at once (0 = no limit)
    note_style: str = "real"         # real | short | legato
    cleanup: bool = True             # merge duplicate notes, drop glitch notes
    pedal_mode: str = "off"          # off | song | auto | always
    pedal_output: str = "key"        # key = press the sustain key, notes = hold the notes longer
    chord_window_ms: float = CHORD_WINDOW_MS
    min_hold_ms: float = MIN_HOLD_MS
    humanize: object = None          # filled in by the humanizer (Phase 4)


@dataclass
class Step:
    time: float
    releases: list = field(default_factory=list)   # (key, note_id)
    presses: list = field(default_factory=list)    # (key, modifier, note_id); modifier None/"shift"/"ctrl"
    pedal: list = field(default_factory=list)      # True = pedal down, False = pedal up, in order


@dataclass
class Plan:
    steps: list
    note_count: int
    folded: int
    duration: float
    labels: list = field(default_factory=list)     # (time, text)
    times: list = field(default_factory=list)      # step times, for seeking
    pedal_changes: list = field(default_factory=list)   # (time, down?) in plan time
    report: dict = field(default_factory=dict)     # what the planner changed, for the app

    def pedal_at(self, t):
        """Should the pedal be down at plan time t?"""
        i = bisect.bisect_right([c[0] for c in self.pedal_changes], t) - 1
        return i >= 0 and self.pedal_changes[i][1]


def map_pitch(pitch, range_mode="octave"):
    """(key, modifier, moved) for a MIDI pitch, or None when the note is skipped."""
    if LOWEST_NOTE <= pitch <= HIGHEST_NOTE:
        key, shift = key_for_pitch(pitch)
        return key, ("shift" if shift else None), False
    if range_mode == "skip":
        return None
    moved = False
    if range_mode == "88" and is_standard_layout():
        while pitch < LOWEST_88:
            pitch += 12
            moved = True
        while pitch > HIGHEST_88:
            pitch -= 12
            moved = True
        if pitch < LOWEST_NOTE:
            return LOW_88_KEYS[pitch - LOWEST_88], "ctrl", moved
        if pitch > HIGHEST_NOTE:
            return HIGH_88_KEYS[pitch - (HIGHEST_NOTE + 1)], "ctrl", moved
        key, shift = key_for_pitch(pitch)
        return key, ("shift" if shift else None), moved
    placed = fold_into_range(pitch)
    key, shift = key_for_pitch(placed)
    return key, ("shift" if shift else None), True


def clean_notes(notes, merge_window=0.015):
    """Merge the same note struck twice at once, and drop tiny silent glitches.
    Returns (notes, removed_count)."""
    by_pitch = {}
    for n in notes:
        by_pitch.setdefault(n.pitch, []).append(n)
    kept = []
    for group in by_pitch.values():
        group.sort(key=lambda n: n.start)
        last = None
        for n in group:
            if last is not None and n.start - last.start <= merge_window:
                last.end = max(last.end, n.end)
                last.velocity = max(last.velocity, n.velocity)
                continue
            if n.end - n.start < 0.008 and n.velocity < 20:
                continue
            last = Note(n.start, n.end, n.pitch, n.velocity, n.channel, n.track)
            kept.append(last)
    kept.sort(key=lambda n: (n.start, n.pitch))
    return kept, len(notes) - len(kept)


def onset_groups(notes, window):
    """Notes that start together (within window seconds), in time order."""
    groups = []
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        if groups and n.start - groups[-1][0].start <= window:
            groups[-1].append(n)
        else:
            groups.append([n])
    return groups


def thin_chords(notes, max_notes, window):
    """Keep at most max_notes per chord: always the top (melody) and bottom (bass),
    then the loudest. Returns (notes, removed_count)."""
    if not max_notes or max_notes <= 0:
        return notes, 0
    kept, removed = [], 0
    for group in onset_groups(notes, window):
        if len(group) <= max_notes:
            kept.extend(group)
            continue
        ordered = sorted(group, key=lambda n: n.pitch)
        must = [ordered[-1]] + ([ordered[0]] if max_notes >= 2 else [])
        rest = sorted((n for n in ordered[1:-1]), key=lambda n: (-n.velocity, -(n.end - n.start)))
        chosen = (must + rest)[:max_notes]
        kept.extend(chosen)
        removed += len(group) - len(chosen)
    kept.sort(key=lambda n: (n.start, n.pitch))
    return kept, removed


def style_notes(notes, style, window):
    """Change how long keys are held: real lengths, short taps, or smooth (legato)."""
    if style == "short":
        return [Note(n.start, n.start + 0.06, n.pitch, n.velocity, n.channel, n.track) for n in notes]
    if style == "legato":
        out = []
        by_track = {}
        for n in notes:
            by_track.setdefault(n.track, []).append(n)
        for group in by_track.values():
            starts = sorted({round(n.start, 6) for n in group})
            for n in group:
                i = bisect.bisect_right(starts, n.start + window)
                nxt = starts[i] if i < len(starts) else n.end
                out.append(Note(n.start, max(n.end, min(nxt, n.start + 4.0)), n.pitch, n.velocity,
                                n.channel, n.track))
        out.sort(key=lambda n: (n.start, n.pitch))
        return out
    return notes


PEDAL_LIFT = 0.07       # how long the pedal comes up when re-pedalling (seconds)


def pedal_intervals(song, notes, mode, tracks=None):
    """(down, up) times for the sustain pedal, in song seconds."""
    if mode == "off" or not notes:
        return []
    first = min(n.start for n in notes)
    last = max(n.end for n in notes)
    if mode == "always":
        return [(first, last)]
    if mode == "song":
        events = [p for p in song.pedal if tracks is None or p[2] in tracks] or list(song.pedal)
        events.sort(key=lambda p: p[0])
        spans, down_at = [], None
        for t, _ch, _tr, value in events:
            if value >= 64 and down_at is None:
                down_at = t
            elif value < 64 and down_at is not None:
                spans.append((down_at, t))
                down_at = None
        if down_at is not None:
            spans.append((down_at, max(last, down_at)))
        return spans
    # auto: hold through each harmony, re-pedal when the bass changes, lift in rests
    groups = onset_groups(notes, 0.03)
    spans = []
    down_at = groups[0][0].start
    bass = min(n.pitch for n in groups[0])
    sounding_until = max(n.end for n in groups[0])
    last_change = down_at
    for g in groups[1:]:
        start = g[0].start
        low = min(n.pitch for n in g)
        if start - sounding_until > 0.35:                   # a rest: let the sound stop
            spans.append((down_at, sounding_until))
            down_at, bass, last_change = start, low, start
        elif low < 60 and low % 12 != bass % 12 and start - last_change >= 0.25:
            spans.append((down_at, start))                  # new harmony: lift and press again
            down_at, bass, last_change = start + PEDAL_LIFT, low, start
        sounding_until = max(sounding_until, max(n.end for n in g))
    spans.append((down_at, max(sounding_until, down_at)))
    return [(a, b) for a, b in spans if b > a]


MIN_PEDAL_LIFT = 0.05    # shorter lifts can fall inside one Roblox frame and be missed
MIN_PEDAL_HOLD = 0.05


def tidy_pedal(spans):
    """Make every lift and every press long enough for the game to notice."""
    out = []
    for down, up in sorted(spans):
        if out and down - out[-1][1] < MIN_PEDAL_LIFT:
            down = out[-1][1] + MIN_PEDAL_LIFT          # press again a moment later
        if up - down < MIN_PEDAL_HOLD:
            continue
        out.append((down, up))
    return out


def plan_pedal_spans(spans, speed, window):
    """Pedal spans in plan time (after speed). Every lift and press lasts at least MIN_PEDAL_LIFT /
    MIN_PEDAL_HOLD of real time, and longer than the chord window, so a lift and the press after it
    always land in different steps (and so in different key events the game can see)."""
    lift = max(MIN_PEDAL_LIFT, window + 0.002)
    hold = max(MIN_PEDAL_HOLD, window + 0.002)
    out = []
    for down, up in sorted(spans):
        down, up = down / speed, up / speed
        if out and down - out[-1][1] < lift:
            down = out[-1][1] + lift
        if up - down < hold:
            continue
        out.append((down, up))
    return out


def hold_notes_through_pedal(notes, spans):
    """For pianos without a pedal: keep keys down while the pedal would be down."""
    if not spans:
        return notes
    downs = [s[0] for s in spans]
    out = []
    for n in notes:
        i = bisect.bisect_right(downs, n.end) - 1
        end = n.end
        if i >= 0 and spans[i][0] <= n.end < spans[i][1]:
            end = spans[i][1]
        out.append(Note(n.start, end, n.pitch, n.velocity, n.channel, n.track))
    return out


def build_plan(song, speed=1.0, transpose=0, tracks=None, skip_drums=SKIP_DRUMS, options=None):
    opts = options or PlayOptions()
    window = max(0.0, opts.chord_window_ms) / 1000.0
    min_hold = max(opts.min_hold_ms / 1000.0, 2 * window)
    report = {"cleaned": 0, "thinned": 0, "skipped": 0, "moved": 0}

    chosen = [Note(n.start, n.end, n.pitch + transpose, n.velocity, n.channel, n.track)
              for n in song.notes
              if (tracks is None or n.track in tracks) and not (skip_drums and n.channel == 9)]
    if opts.cleanup:
        chosen, report["cleaned"] = clean_notes(chosen)
    chosen, report["thinned"] = thin_chords(chosen, opts.max_notes, max(window, 0.004))
    spans = tidy_pedal(pedal_intervals(song, chosen, opts.pedal_mode, tracks))
    chosen = style_notes(chosen, opts.note_style, max(window, 0.004))
    if spans and opts.pedal_output == "notes":
        chosen = hold_notes_through_pedal(chosen, spans)
        spans = []
    if opts.humanize is not None:
        chosen, spans = opts.humanize(chosen, spans)
        report["human"] = dict(opts.humanize.report)

    actions = []        # (time, order, kind, key, modifier, note_id); order: release, pedal up, press, pedal down
    release_at = {}
    note_count = 0
    for n in chosen:
        mapped = map_pitch(n.pitch, opts.range_mode)
        if mapped is None:
            report["skipped"] += 1
            continue
        key, mod, moved = mapped
        if moved:
            report["moved"] += 1
        note_id = note_count
        note_count += 1
        t_on = n.start / speed
        t_off = max(n.end / speed, t_on + min_hold)
        release_at[note_id] = t_off
        actions.append((t_on, 2, "press", key, mod, note_id))
        actions.append((t_off, 0, "release", key, mod, note_id))
    pedal_changes = []
    for down, up in plan_pedal_spans(spans, speed, window):
        pedal_changes.append((down, True))
        pedal_changes.append((up, False))
        actions.append((down, 3, "pedal", None, True, None))
        actions.append((up, 1, "pedal", None, False, None))
    pedal_changes.sort(key=lambda c: (c[0], c[1]))
    actions.sort(key=lambda a: (a[0], a[1]))

    steps = []
    for t, _order, kind, key, mod, note_id in actions:
        if not steps or t - steps[-1].time > window:
            steps.append(Step(t))
        step = steps[-1]
        if kind == "release":
            step.releases.append((key, note_id))
        elif kind == "press":
            step.presses.append((key, mod, note_id))
        else:
            step.pedal.append(mod)

    # Same key twice in one chord (doubled notes): keep the longer one.
    for step in steps:
        best = {}
        for key, mod, note_id in step.presses:
            old = best.get((key, mod))
            if old is None or release_at[note_id] > release_at[old]:
                best[(key, mod)] = note_id
        step.presses = [(k, m, nid) for (k, m), nid in best.items()]

    duration = max((s.time for s in steps), default=0.0)
    report["notes"] = note_count
    return Plan(steps, note_count, report["moved"], duration, times=[s.time for s in steps],
                pedal_changes=pedal_changes, report=report)


# ============================ HUMANIZER ===========================

HUMAN_PRESETS = {            # name: (feel, mistakes)
    "Perfect": (0, 0),
    "Pro": (20, 2),
    "Casual": (45, 12),
    "Beginner": (70, 50),
}
BLACK_PITCH_CLASSES = {1, 3, 6, 8, 10}


class Humanizer:
    """Makes playback sound like a person: timing feel and occasional mistakes.

    feel and mistakes go from 0 (machine-perfect) to 100. The same seed always gives
    the same take, so a take you like can be repeated."""

    def __init__(self, feel=0, mistakes=0, seed=None):
        self.feel = max(0.0, min(100.0, float(feel))) / 100.0
        self.mistakes = max(0.0, min(100.0, float(mistakes))) / 100.0
        self.seed = random.randrange(1, 2 ** 31) if seed is None else int(seed)
        self.report = {"mistakes": 0, "missed": 0, "wrong": 0, "slips": 0, "extra": 0}

    @property
    def active(self):
        return self.feel > 0 or self.mistakes > 0

    # ---- time warp (rubato) ----
    def _make_warp(self, rnd):
        """A smooth, always-forward time warp: local tempo drifts by up to about 4%."""
        amp = 0.04 * self.feel
        waves = [(rnd.uniform(5.0, 13.0), rnd.uniform(0, 2 * math.pi)) for _ in range(3)]
        each = amp / len(waves)

        def warp(t):
            # integral of (1 + each * cos(2*pi*t/P + phi)) dt
            return t + sum(each * P / (2 * math.pi) * (math.sin(2 * math.pi * t / P + ph) - math.sin(ph))
                           for P, ph in waves)
        return warp

    def _make_drift(self, rnd):
        """Slow per-hand offset, up to about 12 ms."""
        amp = 0.012 * self.feel
        waves = [(rnd.uniform(1.5, 4.5), rnd.uniform(0, 2 * math.pi)) for _ in range(2)]
        return lambda t: sum(amp / 2 * math.sin(2 * math.pi * t / P + ph) for P, ph in waves)

    @staticmethod
    def _neighbor(pitch, rnd):
        """A key next to this one, the way a finger slips: white to white, black to either side."""
        if pitch % 12 in BLACK_PITCH_CLASSES:
            return pitch + rnd.choice((-1, 1))
        step = rnd.choice((-1, 1))
        n = pitch + step
        if n % 12 in BLACK_PITCH_CLASSES:
            n += step
        return n

    def __call__(self, notes, spans):
        if not self.active or not notes:
            return notes, spans
        rnd = random.Random(self.seed)
        warp = self._make_warp(rnd) if self.feel > 0 else (lambda t: t)
        middle = sorted(n.pitch for n in notes)[len(notes) // 2]
        drifts = {}
        one_track = len({x.track for x in notes}) == 1      # then split the hands at the middle note

        def hand_of(n):
            return (n.track, n.pitch >= middle) if one_track else n.track

        groups = onset_groups(notes, 0.02)
        starts = [g[0].start for g in groups]
        counted = [0]                                         # notes in groups[:k], for quick window sums
        for g in groups:
            counted.append(counted[-1] + len(g))
        out = []
        for gi, group in enumerate(groups):
            t = group[0].start
            # how busy is it around here? (notes per second within +-1 s)
            lo = bisect.bisect_left(starts, t - 1.0)
            hi = bisect.bisect_right(starts, t + 1.0)
            density = (counted[hi] - counted[lo]) / 2.0
            busy = min(2.0, max(0.5, density / 6.0))
            shared = rnd.gauss(0, 0.008 * self.feel) if self.feel else 0.0
            shared = max(-0.024, min(0.024, shared))
            ordered = sorted(group, key=lambda n: n.pitch)
            if rnd.random() < 0.2:
                ordered.reverse()
            roll = rnd.uniform(0.3, 1.0) * 0.025 * self.feel if len(ordered) > 1 else 0.0
            mistake_done = False
            for k, n in enumerate(ordered):
                hand = hand_of(n)
                if hand not in drifts:
                    drifts[hand] = self._make_drift(rnd) if self.feel > 0 else (lambda _t: 0.0)
                offset = shared + drifts[hand](t) + (roll * k / max(1, len(ordered) - 1))
                offset += rnd.gauss(0, 0.002 * self.feel) if self.feel else 0.0
                length = (n.end - n.start) * (1 + rnd.uniform(-0.1, 0.1) * self.feel)
                start = max(0.0, warp(n.start) + offset)
                end = start + max(0.02, length)
                note = Note(start, end, n.pitch, n.velocity, n.channel, n.track)

                p = 0.06 * self.mistakes * busy * (0.5 if n.end - n.start > 0.8 else 1.0)
                if not mistake_done and self.mistakes > 0 and rnd.random() < min(0.5, p):
                    mistake_done = True
                    self.report["mistakes"] += 1
                    kind = rnd.choices(("wrong", "slip", "missed", "extra"), (35, 30, 20, 15))[0]
                    wrong = self._neighbor(n.pitch, rnd)
                    if kind == "missed":
                        self.report["missed"] += 1
                        continue
                    if kind == "wrong":
                        self.report["wrong"] += 1
                        note = Note(start, end, wrong, n.velocity, n.channel, n.track)
                    elif kind == "slip":
                        self.report["slips"] += 1
                        slip_len = rnd.uniform(0.05, 0.09)
                        out.append(Note(start, start + slip_len, wrong, n.velocity, n.channel, n.track))
                        fix = start + slip_len + rnd.uniform(0.01, 0.04)
                        note = Note(fix, max(fix + 0.03, end), n.pitch, n.velocity, n.channel, n.track)
                    else:
                        self.report["extra"] += 1
                        out.append(Note(start, start + (end - start) * 0.6, wrong, n.velocity, n.channel, n.track))
                out.append(note)
        out.sort(key=lambda n: (n.start, n.pitch))
        new_spans = [(max(0.0, warp(a)), max(0.0, warp(b))) for a, b in spans]
        return out, new_spans


def playable_notes(song, tracks=None, skip_drums=SKIP_DRUMS):
    return [n for n in song.notes
            if (tracks is None or n.track in tracks) and not (skip_drums and n.channel == 9)]


def best_fit_transpose(song, tracks=None, limit=12, low=None, high=None):
    """Transpose (semitones) that puts the most notes on the piano without moving them.
    Ties go to the smallest shift, so a song that already fits stays at 0."""
    low = LOWEST_NOTE if low is None else low
    high = HIGHEST_NOTE if high is None else high
    pitches = [n.pitch for n in playable_notes(song, tracks)]
    if not pitches:
        return 0
    best = None
    for shift in sorted(range(-limit, limit + 1), key=lambda s: (abs(s), s < 0)):
        fits = sum(1 for p in pitches if low <= p + shift <= high)
        if best is None or fits > best[0]:
            best = (fits, shift)
    return best[1]


DIFFICULTY_LEVELS = ((4.0, "Easy"), (8.0, "Medium"), (13.0, "Hard"), (float("inf"), "Expert"))


def song_stats(song, tracks=None):
    """Length, note count and a difficulty rating from how busy the busiest parts are."""
    notes = playable_notes(song, tracks)
    if not notes:
        return {"notes": 0, "duration": 0.0, "nps": 0.0, "difficulty": "Empty"}
    starts = sorted(n.start for n in notes)
    duration = max(n.end for n in notes)
    # notes per second in 2-second windows; rate the song by its busy (90th percentile) windows
    window = 2.0
    counts = [0] * (int(duration // window) + 1)
    for s in starts:
        counts[int(s // window)] += 1
    busy = sorted(c / window for c in counts if c)
    nps = busy[min(len(busy) - 1, int(len(busy) * 0.9))]
    label = next(name for limit, name in DIFFICULTY_LEVELS if nps < limit)
    return {"notes": len(notes), "duration": duration, "nps": round(nps, 1), "difficulty": label}


# ======================== KEYBOARD OUTPUT =========================

SCAN_CODES = {
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07,
    "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15,
    "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23,
    "j": 0x24, "k": 0x25, "l": 0x26,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31,
    "m": 0x32, "shift": 0x2A, "space": 0x39, "ctrl": 0x1D,
}

HOTKEY_CODES = {f"F{i}": 0x6F + i for i in range(1, 13)}
HOTKEY_CODES.update({"HOME": 0x24, "END": 0x23, "INSERT": 0x2D, "DELETE": 0x2E, "PAUSE": 0x13,
                     "PAGEUP": 0x21, "PAGEDOWN": 0x22, "SCROLLLOCK": 0x91})


class WindowsKeyboard:
    """Presses keys with SendInput (scan codes, which games read reliably)."""

    def __init__(self, hotkeys=None, timing=True):
        """hotkeys: {name: virtual-key code} polled by poll_hotkeys (default F6 pause, F7 stop).
        timing=False skips the 1 ms timer and high priority (for helpers that only watch keys)."""
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.wintypes = wintypes
        ulong_ptr = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ulong_ptr)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class _UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]

        self.INPUT = INPUT
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        self.user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        self.kernel32.GetConsoleWindow.restype = wintypes.HWND
        self.console = self.kernel32.GetConsoleWindow()

        if hotkeys is None:
            hotkeys = {"pause": HOTKEY_CODES.get(PAUSE_KEY.upper(), 0x75),
                       "stop": HOTKEY_CODES.get(STOP_KEY.upper(), 0x76)}
        self.hotkeys = dict(hotkeys)
        self._was_down = {name: True for name in self.hotkeys}   # ignore keys held at start
        self._focus_cache = {}
        self._warned = False
        self.on_blocked = None

        # 1 ms timer resolution + higher priority = steadier timing.
        self._timer = False
        if not timing:
            return
        try:
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
            self._timer = True
        except OSError:
            self._timer = False
        try:
            self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            self.kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            self.kernel32.SetPriorityClass(self.kernel32.GetCurrentProcess(), 0x80)  # HIGH
        except Exception:
            pass

    def send(self, ops):
        count = len(ops)
        batch = (self.INPUT * count)()
        for i, (key, down) in enumerate(ops):
            batch[i].type = 1                                  # keyboard
            batch[i].ki.wScan = SCAN_CODES[key]
            batch[i].ki.dwFlags = 0x0008 | (0 if down else 0x0002)   # scancode, keyup
        sent = self.user32.SendInput(count, batch, self.ctypes.sizeof(self.INPUT))
        if sent != count and not self._warned:
            self._warned = True
            print("\n  WARNING: Windows blocked some key presses. If Roblox is running"
                  "\n  as administrator, run this command window as administrator too.")
            if self.on_blocked:
                try:
                    self.on_blocked()
                except Exception:
                    pass

    def _window_is_roblox(self, hwnd):
        if hwnd in self._focus_cache:
            return self._focus_cache[hwnd]
        ctypes, wintypes = self.ctypes, self.wintypes
        length = self.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        exe = ""
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel32.OpenProcess(0x1000, False, pid.value)
        if handle:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                exe = ntpath.basename(path.value).lower()     # a Windows path on any system
            self.kernel32.CloseHandle(handle)
        # Only the game itself: the Roblox player (RobloxPlayerBeta.exe, also when started through
        # Bloxstrap and the like), or the Microsoft Store version, whose window belongs to
        # ApplicationFrameHost and is titled "Roblox". Roblox Studio must NOT count: songs and
        # hotkeys would type into scripts. A folder called "Roblox" open in Explorer must not count.
        # If the program name can't be read (Roblox running as administrator), trust the title.
        result = exe == "robloxplayerbeta.exe" or (
            exe in ("", "applicationframehost.exe", "windows10universal.exe") and title == "Roblox")
        if len(self._focus_cache) > 50:
            self._focus_cache.clear()
        self._focus_cache[hwnd] = result
        return result

    def is_focused(self):
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd or hwnd == self.console:
            return False
        return self._window_is_roblox(hwnd)

    def foreground(self):
        """'roblox', 'app' (a PianoPilot window) or None."""
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return None
        if hwnd != self.console and self._window_is_roblox(hwnd):
            return "roblox"
        buf = self.ctypes.create_unicode_buffer(64)
        self.user32.GetWindowTextW(hwnd, buf, 64)
        return "app" if buf.value.startswith("PianoPilot") else None

    def poll_hotkeys(self):
        pressed = set()
        for name, vk in self.hotkeys.items():
            down = bool(self.user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not self._was_down[name]:
                pressed.add(name)
            self._was_down[name] = down
        return pressed

    def close(self):
        if self._timer:
            try:
                self.ctypes.WinDLL("winmm").timeEndPeriod(1)
            except OSError:
                pass


class DryRunKeyboard:
    """Pretends to press keys and prints what it would do."""

    def __init__(self, print_limit=300):
        self.log = []
        self.print_limit = print_limit
        self.printed = 0
        self.clock = None
        self.shift = False
        self.ctrl = False

    def send(self, ops):
        now = self.clock() if self.clock else 0.0
        self.log.append((now, list(ops)))
        if self.printed >= self.print_limit:
            if self.printed == self.print_limit:
                print("  ... (more keys not shown)")
                self.printed += 1
            return
        self.printed += 1
        parts = []
        for key, down in ops:
            if key == "shift":
                self.shift = down
                parts.append("[Shift" + (" down]" if down else " up]"))
            elif key == "ctrl":
                self.ctrl = down
                parts.append("[Ctrl" + (" down]" if down else " up]"))
            elif key == "space":
                parts.append("[Pedal" + (" down]" if down else " up]"))
            elif down:
                parts.append(key_label(key, "ctrl" if self.ctrl else ("shift" if self.shift else None)))
            else:
                parts.append(f"({key} up)")
        print(f"  {now:8.3f}s  " + " ".join(parts))

    def is_focused(self):
        return True

    def poll_hotkeys(self):
        return set()

    def close(self):
        pass


# ============================= PLAYER =============================

_JUMP = "jump"          # returned by the wait loop when the position changed
_KEEP = "keep"          # a seek that leaves the loop as it is
STALL_LIMIT = 0.05      # a step this late (a hiccup in Windows or Python) restarts the clock instead of
                        # firing every missed note at once


class Player:
    """Plays a Plan. Safe to control from another thread with the request_* methods."""

    # With a tap-style pedal the game keeps its own on/off state, even between songs, and a tap
    # only reaches the game while Roblox is in front. This is what the game is believed to have.
    game_tap_pedal = False

    def __init__(self, keyboard, clock=time.perf_counter, sleep=time.sleep,
                 show_progress=True, countdown=COUNTDOWN_SECONDS, on_status=None,
                 shift_gap_ms=None, pedal_key="space", pedal_style="hold"):
        self.kb = keyboard
        self.shift_gap = (SHIFT_GAP_MS if shift_gap_ms is None else shift_gap_ms) / 1000.0
        self.pedal_key = pedal_key
        self.pedal_style = pedal_style      # hold = keep the key down; tap = tap on, tap off
        self.pedal_on = Player.game_tap_pedal if pedal_style == "tap" else False
        self.clock = clock
        self.sleep = sleep
        self.show_progress = show_progress
        self.countdown = countdown
        self.on_status = on_status
        self.held = {}              # key -> note_id currently holding it down
        self._missed_ups = set()    # keys released while another window was in front
        self.stopped = False
        self.manual_pause = False
        self.plan = None
        self.total = 0.0
        self.state = "idle"
        self.base_clock = 0.0       # song position = base_pos + (clock - base_clock)
        self.base_pos = 0.0
        self._anchor = (0.0, 0.0)   # (base_clock, base_pos) as one value, for position() from other threads
        self.paused_pos = 0.0
        self._cmds = queue.SimpleQueue()
        self._pending_jump = None   # (position, new_plan_or_None)
        self.loop = None            # (start, end) in plan time, or None
        self.loops_done = 0
        self.last_check = -1.0
        self.last_print = -1.0
        self.last_status = -1.0
        self.lateness = []

    # ---------- control from other threads ----------
    def request_stop(self):
        self._cmds.put(("stop",))

    def request_pause(self):
        self._cmds.put(("pause",))

    def request_resume(self):
        self._cmds.put(("resume",))

    def request_seek(self, position):
        self._cmds.put(("seek", float(position), None, _KEEP))

    def request_loop(self, start=None, end=None):
        """Repeat plan time start..end (both None turns looping off). Takes effect immediately."""
        self._cmds.put(("loop", start, end))

    def request_replace(self, plan, position, loop=None):
        """Swap in a new plan (new speed/transpose/tracks) and continue at position.
        loop is the new plan's (start, end) loop or None, applied together with the plan."""
        self._cmds.put(("seek", float(position), plan, loop))

    def position(self):
        if self.state in ("playing",):
            base_clock, base_pos = self._anchor
            return max(0.0, base_pos + (self.clock() - base_clock))
        return self.paused_pos

    # ---------- small helpers ----------
    def _say(self, text):
        if self.show_progress:
            print("\r" + " " * 70 + "\r" + text, flush=True)

    def _status(self, state, **extra):
        self.state = state
        if state != "playing":
            self.paused_pos = extra.get("pos", self.paused_pos)
        if self.on_status:
            info = {"state": state, "pos": extra.pop("pos", self.position()), "total": self.total}
            info.update(extra)
            try:
                self.on_status(info)
            except Exception:
                pass

    def _release_all(self):
        ops = [(key, False) for key in self.held]
        ops.append(("shift", False))
        ops.append(("ctrl", False))
        if not self._focused():         # these key-ups go to another window; Roblox still needs them later
            self._missed_ups.update(self.held)
            if self.pedal_style == "hold" and self.pedal_on:
                self._missed_ups.add(self.pedal_key)
        self.held.clear()
        self.kb.send(ops)
        self._set_pedal(False)

    def _resend_missed_ups(self):
        """Key-ups that went to another window never reached Roblox. Send them again while it's in front,
        so no key stays down in the game. (A key-up for a key that isn't down does nothing.)"""
        if self._missed_ups and self._focused():
            self.kb.send([(key, False) for key in sorted(self._missed_ups)])
            self._missed_ups.clear()

    def _set_pedal(self, down, ops=None):
        """Put the sustain pedal down or up. Sends now unless an ops list is given (hold style)."""
        if down == self.pedal_on:
            return
        if self.pedal_style == "tap":
            if not self._focused():
                return              # the tap would land in another window; the game keeps its state
            self.pedal_on = Player.game_tap_pedal = down
            if ops:
                self.kb.send(ops)
                ops.clear()
            self._resend_missed_ups()       # a Space still "down" in the game would swallow this tap
            self.kb.send([(self.pedal_key, True)])
            self._precise_sleep(0.03)
            self.kb.send([(self.pedal_key, False)])
            if not self._focused():         # focus left mid-tap: the key-up went elsewhere
                self._missed_ups.add(self.pedal_key)
            return
        self.pedal_on = down
        if ops is not None:
            ops.append((self.pedal_key, down))
        else:
            self.kb.send([(self.pedal_key, down)])

    def _sync_pedal(self, pos):
        """After a jump or resume, put the pedal where the song wants it: the state right before the next
        step to play (a step's own pedal change may come a few ms after the step's time)."""
        if self.plan is None or not self.plan.pedal_changes:
            return
        i = self._index_for(pos)
        t = self.plan.times[i] - 1e-9 if i < len(self.plan.times) else self.total + 1.0
        self._set_pedal(self.plan.pedal_at(t))

    def _precise_sleep(self, seconds):
        end = self.clock() + seconds
        while self.clock() < end:
            pass

    def _set_position(self, pos):
        self.base_clock = self.clock()
        self.base_pos = max(0.0, pos)
        self._anchor = (self.base_clock, self.base_pos)

    def _index_for(self, pos):
        times = self.plan.times
        return bisect.bisect_left(times, pos - 1e-9)

    def _read_inputs(self):
        """Hotkeys + requests from the app. Updates flags; returns nothing."""
        hot = self.kb.poll_hotkeys()
        if "stop" in hot:
            self.stopped = True
        if "pause" in hot:
            self.manual_pause = not self.manual_pause
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                break
            if cmd[0] == "stop":
                self.stopped = True
            elif cmd[0] == "pause":
                self.manual_pause = True
            elif cmd[0] == "resume":
                self.manual_pause = False
            elif cmd[0] == "loop":
                self._set_loop(cmd[1], cmd[2])
            elif cmd[0] == "seek":
                self._pending_jump = (cmd[1], cmd[2])
                if cmd[3] != _KEEP:
                    self._set_loop(*(cmd[3] or (None, None)))

    def _set_loop(self, a, b):
        self.loop = (max(0.0, a), b) if a is not None and b is not None and b - a >= 0.3 else None

    def _apply_jump(self):
        """Apply a pending seek/replace. Returns the new position."""
        pos, new_plan = self._pending_jump
        self._pending_jump = None
        if new_plan is not None:
            self.plan = new_plan
            self.total = new_plan.duration
        return min(max(0.0, pos), self.total)

    def _focused(self):
        return self.kb.is_focused() or not AUTO_PAUSE_WHEN_UNFOCUSED

    # ---------- start / pause handling ----------
    def _wait_for_start(self):
        """Wait until Roblox is focused, then count down. Returns True to start."""
        while True:
            self._read_inputs()
            if self.stopped:
                return False
            if self._pending_jump:
                self.paused_pos = self._apply_jump()
            if not self.kb.is_focused():
                if self.state != "waiting":
                    self._say("  Click into the Roblox window to start...  "
                              f"({STOP_KEY} = cancel)")
                    self._status("waiting", pos=self.paused_pos)
                self.sleep(0.05)
                continue
            ok = True
            for n in range(self.countdown, 0, -1):
                self._say(f"  Starting in {n}...")
                self._status("countdown", count=n, pos=self.paused_pos)
                until = self.clock() + 1.0
                while self.clock() < until:
                    self._read_inputs()
                    if self.stopped:
                        return False
                    if self._pending_jump:
                        self.paused_pos = self._apply_jump()
                    if AUTO_PAUSE_WHEN_UNFOCUSED and not self.kb.is_focused():
                        ok = False
                        break
                    self.sleep(0.02)
                if not ok:
                    break
            if ok:
                return True
            self.state = "idle"          # lost focus during countdown: wait again

    def _pause_loop(self):
        """Hold here while paused. Returns True if the position changed."""
        pos = self.position()
        self._release_all()
        jumped = False
        shown = None
        focus_since = None
        while True:
            self._read_inputs()
            if self.stopped:
                self.paused_pos = pos
                return jumped
            if self._pending_jump:
                pos = self._apply_jump()
                jumped = True
                shown = None
            focused = self._focused()
            if self.manual_pause:
                state, reason = f"  Paused. Press {PAUSE_KEY} to resume, {STOP_KEY} to stop.", "manual"
                focus_since = None
            elif not focused:
                state, reason = "  Paused: Roblox isn't the active window. Click back into Roblox.", "focus"
                focus_since = None
            else:
                state, reason = "  Resuming...", "resuming"
                if focus_since is None:
                    focus_since = self.clock()
                elif self.clock() - focus_since >= 1.0:
                    break
            if state != shown:
                self._say(state)
                self._status("paused", reason=reason, pos=pos)
                shown = state
            self.sleep(0.02)
        self._resend_missed_ups()
        self._set_position(pos)
        self._sync_pedal(pos)
        self._status("playing", pos=pos)
        return jumped

    def _check_controls(self):
        """Returns None to stop, _JUMP if the position changed, or True to carry on."""
        self._read_inputs()
        if self.stopped:
            return None
        jumped = False
        if self._pending_jump:
            self._release_all()
            self._set_position(self._apply_jump())
            self._sync_pedal(self.base_pos)
            jumped = True
        if self.manual_pause or not self._focused():
            jumped = self._pause_loop() or jumped
            if self.stopped:
                return None
        return _JUMP if jumped else True

    # ---------- timing ----------
    def _wait_until(self, step_time):
        """Wait until the step is due. Returns the target clock time, _JUMP, or None if stopped."""
        while True:
            now = self.clock()
            if now - self.last_check >= 0.015:
                self.last_check = now
                result = self._check_controls()
                if result is None or result == _JUMP:
                    return result
                now = self.clock()
            target = self.base_clock + (step_time - self.base_pos)
            remaining = target - now
            if remaining <= 0:
                return target
            if remaining > 0.004:
                self._progress(now)
                self.sleep(min(remaining - 0.002, 0.010))
            # under 4 ms: spin for accuracy

    def _progress(self, now):
        if now - self.last_status >= 0.2 and self.on_status:
            self.last_status = now
            self._status("playing", pos=self.base_pos + (now - self.base_clock))
        if not self.show_progress or now - self.last_print < 0.25:
            return
        self.last_print = now
        song_time = self.base_pos + (now - self.base_clock)
        print(f"\r  Playing  {fmt_time(song_time)} / {fmt_time(self.total)}"
              f"   ({PAUSE_KEY} pause, {STOP_KEY} stop)   ", end="", flush=True)

    # ---------- the actual key presses ----------
    def _run_step(self, step):
        ops = []
        for key, note_id in step.releases:
            if self.held.get(key) == note_id:
                ops.append((key, False))
                del self.held[key]
        # Pedal: what matters is where it ends up. Lift before the new keys if it ends up, or if it's
        # re-pressed in this step (a lift, then down again after the keys).
        pedal_end = step.pedal[-1] if step.pedal else None
        if pedal_end is False or (pedal_end is True and False in step.pedal):
            self._set_pedal(False, ops)

        gap = self.shift_gap
        groups = {None: [], "shift": [], "ctrl": []}
        for press in step.presses:
            groups[press[1] if press[1] in groups else None].append(press)

        for key, _mod, note_id in groups[None]:
            if key in self.held:
                ops.append((key, False))          # re-press for repeated notes
            ops.append((key, True))
            self.held[key] = note_id

        for modifier in ("shift", "ctrl"):
            keys = groups[modifier]
            if not keys:
                continue
            if gap > 0 and ops:
                self.kb.send(ops)
                ops = []
                self._precise_sleep(gap)
            ops.append((modifier, True))
            if gap > 0:
                self.kb.send(ops)
                ops = []
                self._precise_sleep(gap)
            for key, _mod, note_id in keys:
                if key in self.held:
                    ops.append((key, False))
                ops.append((key, True))
                self.held[key] = note_id
            if gap > 0:
                self.kb.send(ops)
                ops = []
                self._precise_sleep(gap)
            ops.append((modifier, False))

        if pedal_end is True:
            self._set_pedal(True, ops)
        if ops:
            self.kb.send(ops)

    def play(self, plan, start_at=0.0):
        """Play plan from start_at seconds. Returns True if it reached the end."""
        self.plan = plan
        self.total = plan.duration
        self.stopped = False
        self.manual_pause = False
        self._pending_jump = None
        self.state = "idle"
        self.paused_pos = min(max(0.0, start_at), self.total)
        if not self._wait_for_start():
            self._say("  Cancelled.")
            self._status("stopped", pos=self.paused_pos)
            return False
        gc_was_enabled = gc.isenabled()
        gc.disable()                      # no surprise pauses mid-song
        finished = False
        try:
            self._set_position(self.paused_pos)
            self._sync_pedal(self.base_pos)
            self.last_check = self.clock()
            self._status("playing", pos=self.base_pos)
            labels = [lb for lb in self.plan.labels if lb[0] >= self.base_pos - 1e-9]
            i = self._index_for(self.base_pos)
            while True:
                steps = self.plan.steps
                loop = self.loop
                if loop and (i >= len(steps) or steps[i].time >= loop[1]):
                    target = self._wait_until(loop[1])          # play out to the loop end
                    if target is None:
                        break
                    if target == _JUMP or self.loop == loop:
                        if target != _JUMP:                     # wrap around to the loop start
                            self._release_all()
                            self._set_position(loop[0])
                            self._sync_pedal(loop[0])
                            self.loops_done += 1
                        i = self._index_for(self.base_pos)
                        labels = [lb for lb in self.plan.labels if lb[0] >= self.base_pos - 1e-9]
                    # else the loop was changed or turned off while waiting: carry on from step i
                    continue
                if i >= len(steps):
                    finished = True
                    break
                step = steps[i]
                while labels and labels[0][0] <= step.time:
                    label = labels.pop(0)[1]
                    if self.show_progress:
                        print("\r" + " " * 70 + "\r  > " + label, flush=True)
                target = self._wait_until(step.time)
                if target is None:
                    break
                if target == _JUMP:
                    i = self._index_for(self.base_pos)
                    labels = [lb for lb in self.plan.labels if lb[0] >= self.base_pos - 1e-9]
                    continue
                if not self._focused():
                    self.last_check = -1.0      # Roblox just lost focus: pause before pressing anything
                    continue
                late = self.clock() - target
                if late > STALL_LIMIT:
                    self._set_position(step.time)   # restart the clock here instead of a burst of notes
                self._run_step(step)
                self.lateness.append(late)
                i += 1
        finally:
            self._release_all()
            self._resend_missed_ups()
            if gc_was_enabled:
                gc.enable()
        if finished:
            self._say(f"  Finished! {self.timing_report()}")
            self._status("finished", pos=self.total, timing=self.timing_report())
        else:
            self._say("  Stopped.")
            self._status("stopped", pos=self.position() if self.state == "playing" else self.paused_pos)
        return finished

    def timing_report(self):
        if not self.lateness:
            return ""
        ms = sorted(x * 1000 for x in self.lateness)
        p99 = ms[min(len(ms) - 1, int(len(ms) * 0.99))]
        return f"Timing: 99% of key presses within {p99:.1f} ms (worst {ms[-1]:.1f} ms)."


# ========================== CALIBRATION ===========================

def calibration_song():
    notes, labels = [], []

    def add(start, length, *pitches):
        for p in pitches:
            notes.append(Note(start, start + length, p, 90, 0, 1))

    t = 0.0
    labels.append((t, "White keys going up: C D E F G A B C"))
    for p in (60, 62, 64, 65, 67, 69, 71, 72):
        add(t, 0.22, p)
        t += 0.25
    t += 0.5
    labels.append((t, "Black keys only: C# D# F# G# A#  (should sound 'in between')"))
    for p in (61, 63, 66, 68, 70):
        add(t, 0.27, p)
        t += 0.3
    t += 0.5
    labels.append((t, "Chords mixing white + black keys: C, E, A, Db major (all should sound clean)"))
    for chord in ((60, 64, 67), (64, 68, 71), (57, 61, 64), (61, 65, 68)):
        add(t, 0.75, *chord)
        t += 0.8
    t += 0.4
    labels.append((t, "Fast repeated note: 8 quick taps on one key"))
    for _ in range(8):
        add(t, 0.06, 72)
        t += 0.09
    t += 0.4
    labels.append((t, "Trill C <-> C# (same key, with and without Shift)"))
    for i in range(10):
        add(t, 0.08, 72 + (i % 2))
        t += 0.1
    t += 0.4
    labels.append((t, "Lowest key, then highest key"))
    add(t, 0.5, LOWEST_NOTE)
    t += 0.6
    add(t, 0.5, HIGHEST_NOTE)
    t += 0.6

    notes.sort(key=lambda n: (n.start, n.pitch))
    info = TrackInfo(1, "Calibration", len(notes), {0},
                     min(n.pitch for n in notes), max(n.pitch for n in notes))
    return Song("Calibration test", notes, [], [info], t), labels


def calibration_parts():
    """The calibration test in short parts, each with a yes/no question for the player."""
    def notes_at(seq, gap, length, start=0.3):
        out, t = [], start
        for item in seq:
            for p in (item if isinstance(item, tuple) else (item,)):
                out.append(Note(t, t + length, p, 90, 0, 1))
            t += gap
        return out
    return [
        {"id": "white", "title": "White keys",
         "question": "Did you hear a clean scale going up: C D E F G A B C?",
         "notes": notes_at([60, 62, 64, 65, 67, 69, 71, 72], 0.3, 0.27)},
        {"id": "black", "title": "Black keys",
         "question": "Did these five notes sound in between the white keys, not like white keys?",
         "notes": notes_at([61, 63, 66, 68, 70], 0.35, 0.3)},
        {"id": "chords", "title": "Chords with black keys",
         "question": "Did each of the four chords sound full, with every note at once?",
         "notes": notes_at([(60, 64, 67), (64, 68, 71), (57, 61, 64), (61, 65, 68)], 0.8, 0.75)},
        {"id": "fast", "title": "Fast notes",
         "question": "Did you hear 8 separate quick taps on the same note?",
         "notes": notes_at([72] * 8, 0.09, 0.06)},
        {"id": "pedal", "title": "Sustain pedal",
         "question": "Did the notes keep ringing together, then stop together at the end?",
         "notes": notes_at([48, 55, 60, 64, 67, 72], 0.25, 0.12), "pedal": True},
        {"id": "wide", "title": "88-key notes",
         "question": "Did you hear a very deep note, then a very high one, beyond the normal keys?",
         "notes": notes_at([21, 108], 0.8, 0.6), "range": "88"},
    ]


# ============================== MAIN ==============================

def print_song_info(song, plan=None, tracks=None):
    print(f"\n  Song:   {song.name}")
    print(f"  Length: {fmt_time(song.duration)}    Notes: {len(song.notes)}    "
          f"Tracks with notes: {sum(1 for t in song.tracks if t.notes)}")
    for t in song.tracks:
        if not t.notes:
            continue
        drums = SKIP_DRUMS and t.channels == {9}
        skipped = drums or (tracks is not None and t.number not in tracks)
        tag = " (drums, skipped)" if drums else (" (skipped)" if skipped else "")
        print(f"    Track {t.number:<3} {t.name[:28]:<28} {t.notes:>6} notes  "
              f"{note_name(t.low)}-{note_name(t.high)}{tag}")
    if plan is not None:
        print(f"  Playing {plan.note_count} notes.", end="")
        if plan.folded:
            print(f"  {plan.folded} were outside the piano's range and were moved"
                  f" by an octave to fit.", end="")
        print()
    downs = sum(1 for p in song.pedal if p[3] >= 64)
    if downs:
        print(f"  This file uses the sustain pedal {downs} times. Add --pedal song to play it.")


def parse_tracks(text):
    if not text:
        return None
    try:
        return {int(x) for x in text.replace(" ", "").split(",") if x}
    except ValueError:
        raise SystemExit("  --tracks needs numbers like 1,3")


def pick_file(start_dir):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Pick a MIDI song", initialdir=start_dir,
            filetypes=[("MIDI files", "*.mid *.midi"), ("All files", "*.*")])
        root.destroy()
        return path or None
    except Exception:
        try:
            path = input("  Type or paste the path to a .mid file: ").strip().strip('"')
        except EOFError:
            return None
        return path or None


def make_keyboard(dry_run):
    if dry_run:
        return DryRunKeyboard()
    if os.name != "nt":
        print("  Real key presses only work on Windows. Showing a dry run instead.")
        return DryRunKeyboard()
    return WindowsKeyboard()


def play_song(song, args, labels=None):
    tracks = parse_tracks(args.tracks)
    opts = PlayOptions(range_mode=args.range, max_notes=args.max_notes, note_style=args.style,
                       cleanup=not args.no_cleanup, pedal_mode=args.pedal,
                       pedal_output="notes" if args.pedal_method == "notes" else "key")
    plan = build_plan(song, args.speed, args.transpose, tracks, options=opts)
    if labels:
        plan.labels = [(t / args.speed, text) for t, text in labels]
        print("\n  CALIBRATION: listen to each part as it plays. If the black-key parts"
              "\n  sound like plain white keys, open this file and set SHIFT_GAP_MS = 20.")
    print_song_info(song, plan, tracks)
    if not plan.steps:
        print("  Nothing to play (no notes in the chosen tracks).")
        return
    keyboard = make_keyboard(args.dry_run)
    try:
        if isinstance(keyboard, DryRunKeyboard):
            fake = FakeClock()
            keyboard.clock = fake.time
            player = Player(keyboard, clock=fake.time, sleep=fake.sleep,
                            pedal_style="tap" if args.pedal_method == "tap" else "hold",
                            show_progress=False, countdown=0)
            print()
            player.play(plan)
            print(f"\n  Dry run done: {len(plan.steps)} key moments, "
                  f"{plan.note_count} notes. Nothing was actually pressed.")
        else:
            print(f"\n  Tip: make sure Roblox chat isn't open, or the notes get typed into it.")
            Player(keyboard, pedal_style="tap" if args.pedal_method == "tap" else "hold").play(plan)
    finally:
        keyboard.close()


class FakeClock:
    """Instant clock for dry runs and tests."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        self.now += 1e-6
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 1e-6)


def main():
    parser = argparse.ArgumentParser(description="Roblox piano auto player")
    parser.add_argument("file", nargs="?", help="MIDI file to play")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--transpose", type=int, default=0)
    parser.add_argument("--tracks", default=None)
    parser.add_argument("--info", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--pedal", default="off", choices=["off", "song", "auto", "always"])
    parser.add_argument("--pedal-method", default="hold", choices=["hold", "tap", "notes"])
    parser.add_argument("--range", default="octave", choices=["octave", "skip", "88"])
    parser.add_argument("--max-notes", type=int, default=0)
    parser.add_argument("--style", default="real", choices=["real", "short", "legato"])
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--bpm", type=float, default=120, help="tempo for .txt piano sheets")
    parser.add_argument("--export-sheet", action="store_true", help="print the song as a text sheet")
    args = parser.parse_args()
    if args.speed <= 0:
        raise SystemExit("  --speed must be more than 0")

    print("\n  === Roblox Piano Auto Player ===")

    if args.test:
        song, labels = calibration_song()
        play_song(song, args, labels)
        return

    if args.file:
        if args.file.lower().endswith(".txt"):
            with open(args.file, encoding="utf-8", errors="replace") as f:
                notes, problems = parse_sheet(f.read(), args.bpm)
            for problem in problems:
                print("  " + problem)
            if not notes:
                raise SystemExit("  No notes in that sheet.")
            name = os.path.splitext(os.path.basename(args.file))[0]
            song = Song(name, notes, [], [TrackInfo(1, "Sheet", len(notes), {0}, min(n.pitch for n in notes),
                                                     max(n.pitch for n in notes))], max(n.end for n in notes), args.bpm)
        else:
            song = load_midi(args.file)
        if args.export_sheet:
            text, bpm = export_sheet(song, args.transpose, parse_tracks(args.tracks), args.range)
            print(f"# {song.name}\n# Tempo: {bpm} BPM\n{text}")
            return
        if args.info:
            print_song_info(song, tracks=parse_tracks(args.tracks))
        else:
            play_song(song, args)
        return

    # No file given: pick songs from a window, one after another.
    folder = os.path.dirname(os.path.abspath(__file__))
    while True:
        path = pick_file(folder)
        if not path:
            print("  No song picked. Bye!")
            break
        folder = os.path.dirname(path)
        try:
            song = load_midi(path)
            if args.info:
                print_song_info(song)
            else:
                play_song(song, args)
        except (MidiError, OSError) as e:
            print(f"  Couldn't read that file: {e}")
        answer = input("\n  Press Enter to pick another song, or type q and Enter to quit: ")
        if answer.strip().lower() == "q":
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    except MidiError as e:
        print(f"\n  Couldn't read that MIDI file: {e}")
    except FileNotFoundError as e:
        print(f"\n  File not found: {e.filename}")
