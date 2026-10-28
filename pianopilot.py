#! python3
"""PianoPilot: plays MIDI songs on Roblox pianos, with an app window.

Start it with PianoPilot.exe (no command window) or "Start PianoPilot.bat" (with one), or:
python pianopilot.py. It runs a small local server on this computer only and opens the app
window. Closing the window closes the app.

  Songs/   your MIDI files (drop new ones in any time)
  data/    settings, the audio-import model, the log, temporary files
"""
import hashlib
import json
import os
import queue
import random
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
LOG_FILE = APP_DIR / "data" / "pianopilot.log"
EXIT_REPORTED = 3       # exit code meaning "an error message was already shown" (PianoPilot.exe stays quiet)


class _Tee:
    """Sends printed text to the log file, and to the command window too when there is one."""

    def __init__(self, console, file):
        self.console, self.file = console, file
        self.encoding = "utf-8"
        self.errors = "replace"

    def write(self, text):
        for out in (self.console, self.file):
            if out is not None:
                try:
                    out.write(text)
                except Exception:
                    pass
        return len(text)

    def flush(self):
        for out in (self.console, self.file):
            if out is not None:
                try:
                    out.flush()
                except Exception:
                    pass

    def isatty(self):
        return False

    def fileno(self):
        if self.console is None:
            raise OSError("PianoPilot has no command window")
        return self.console.fileno()


def start_log():
    """Copy everything printed into data/pianopilot.log. Started from PianoPilot.exe there's no command
    window at all, so the log is where errors end up. It starts over once it passes 1 MB."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.is_file() and LOG_FILE.stat().st_size > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE.with_name("pianopilot.old.log"))
        log_file = open(LOG_FILE, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return
    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)


if __name__ == "__main__":
    start_log()             # before anything else, so even a broken install leaves a trace

sys.path.insert(0, str(APP_DIR))
import piano_player as pp  # noqa: E402

APP_NAME = "PianoPilot"
VERSION = "0.8.0"
SONGS_DIR = APP_DIR / "Songs"
DATA_DIR = APP_DIR / "data"
UI_DIR = APP_DIR / "ui"
MODEL_DIR = DATA_DIR / "model"
INCOMING_DIR = DATA_DIR / "incoming"
SETTINGS_FILE = DATA_DIR / "settings.json"
SERVER_FILE = DATA_DIR / "server.json"
PREFERRED_PORT = 47615
IS_WINDOWS = os.name == "nt"
SIMULATE = bool(os.environ.get("PIANOPILOT_SIMULATE")) or not IS_WINDOWS
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

MODEL_FILE_NAME = "PianoModel.pianoscribe"
MODEL_URL = "https://downloads.justagwas.com/pianoscribe/PianoModel.pianoscribe"
MODEL_SHA256 = "bd7c3835288100e07ab4433ec690539685496790d1cba42e66501b36c6a85790"
MODEL_PARTS = ("manifest.json", "scorer.onnx", "attributes.onnx", "frontend.npz", "LICENSE.txt")
AUDIO_TYPES = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".aiff", ".aif",
               ".opus", ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".mpg", ".mpeg", ".3gp", ".flv"}
MIDI_TYPES = {".mid", ".midi"}
MAX_UPLOAD = 2 * 1024 ** 3

TOKEN = secrets.token_urlsafe(24)


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


# ============================== EVENTS ==============================

class Events:
    """Live updates pushed to the app window (server-sent events)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.last_seen = time.time()
        self.ever_connected = False

    def add(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.clients.append(q)
            self.ever_connected = True
        return q

    def remove(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)
            self.last_seen = time.time()

    def count(self):
        with self.lock:
            return len(self.clients)

    def send(self, kind, data):
        payload = f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass


EVENTS = Events()


# ============================= SETTINGS =============================

class Settings:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()          # guards self.data
        self.write_lock = threading.Lock()    # one file write at a time, outside self.lock
        self.saved_seq = self.written_seq = 0
        self.data = {"songs": {}, "app": {}}
        # Every change to a song's settings gets a higher number, so a window can tell which of two
        # answers is newer (they can arrive out of order). Kept in memory only.
        self.rev = 0
        self.base_rev = 0                     # the number of a song nobody has changed since the last reset
        self.song_revs = {}
        if not path.is_file():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("it isn't a settings file")
            self.data["songs"] = dict(loaded.get("songs") or {})
            self.data["app"] = dict(loaded.get("app") or {})
        except Exception as exc:
            # Keep the unreadable file instead of overwriting it on the next save.
            broken = path.with_name("settings.broken.json")
            try:
                os.replace(path, broken)
                log(f"settings.json couldn't be read ({exc}). Kept it as {broken.name} and started with defaults.")
            except OSError:
                log(f"settings.json couldn't be read ({exc}).")

    def save(self):
        with self.lock:
            self.saved_seq += 1
            seq = self.saved_seq
            text = json.dumps(self.data, indent=1, ensure_ascii=False)
        with self.write_lock:
            if seq < self.written_seq:        # a newer snapshot is already on disk
                return
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(6):          # antivirus or a backup tool can hold the file for a moment
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.1)
            self.written_seq = seq

    def song(self, key):
        with self.lock:
            return dict(self.data["songs"].get(key) or {})

    def song_state(self, key):
        """A song's stored settings and their change number, read together."""
        with self.lock:
            return dict(self.data["songs"].get(key) or {}), self.song_revs.get(key, self.base_rev)

    def _bump(self, key):
        self.rev += 1
        self.song_revs[key] = self.rev
        return self.rev

    def set_song(self, key, values):
        with self.lock:
            current = self.data["songs"].setdefault(key, {})
            current.update(values)
            rev = self._bump(key)
        self.save()
        return rev

    def set_songs(self, values_by_key, expect=None):
        """Several songs at once, saved to disk once. Returns {key: change number} for the songs written.
        expect: {key: change number}; a song changed since then (by you, say) is left alone.
        If saving fails, nothing changes here either, so the caller can simply try again."""
        revs, undo = {}, {}
        with self.lock:
            for key, values in values_by_key.items():
                if expect is not None and self.song_revs.get(key, self.base_rev) != expect.get(key):
                    continue
                undo[key] = (key in self.data["songs"], dict(self.data["songs"].get(key) or {}))
                self.data["songs"].setdefault(key, {}).update(values)
                revs[key] = self._bump(key)
        if not revs:
            return revs
        try:
            self.save()
        except Exception:
            with self.lock:
                for key, (had, old) in undo.items():
                    if had:
                        self.data["songs"][key] = old
                    else:
                        self.data["songs"].pop(key, None)
            raise
        return revs

    def update_song(self, key, change):
        """Read, change and write one song's settings in one step, so nothing can slip in between
        (a hotkey speed change at the same moment as a change in the window, say).
        change(stored) returns (new settings, anything else); returns (new settings, change number, anything else)."""
        with self.lock:
            new, extra = change(dict(self.data["songs"].get(key) or {}))
            self.data["songs"][key] = dict(new)
            rev = self._bump(key)
        self.save()
        return new, rev, extra

    def reset_songs(self):
        with self.lock:
            self.data["songs"] = {}
            self.rev += 1
            self.base_rev = self.rev
            self.song_revs = {}
        self.save()

    def app(self, name, default=None):
        with self.lock:
            return self.data["app"].get(name, default)

    def set_app(self, name, value):
        with self.lock:
            self.data["app"][name] = value
        self.save()

    def forget_song(self, key):
        """A song was deleted: drop its settings, star, queue entries and last-played time."""
        with self.lock:
            self.data["songs"].pop(key, None)
            app = self.data["app"]
            if isinstance(app.get("favorites"), list):
                app["favorites"] = [k for k in app["favorites"] if k != key]
            if isinstance(app.get("queue"), list):
                app["queue"] = [k for k in app["queue"] if k != key]
            if isinstance(app.get("played"), dict):
                app["played"].pop(key, None)
            for name in ("last_song", "selected"):
                if app.get(name) == key:
                    app.pop(name)
            self._bump(key)
        self.save()

    def move_song(self, old, new):
        """A song file was renamed or moved: its settings, star and last-played time go with it."""
        with self.lock:
            songs = self.data["songs"]
            # The file's own settings and star win over anything left behind under the new name
            # (a song deleted in Explorer leaves its settings there).
            if old in songs:
                songs[new] = songs.pop(old)
            else:
                songs.pop(new, None)
            app = self.data["app"]
            favorites = app.get("favorites")
            if isinstance(favorites, list) and old in favorites:
                app["favorites"] = [k for k in favorites if k != new]
                app["favorites"][app["favorites"].index(old)] = new
            elif isinstance(favorites, list) and new in favorites:
                app["favorites"] = [k for k in favorites if k != new]
            played = app.get("played")
            if isinstance(played, dict) and old in played:
                when = played.pop(old)
                if new not in played:
                    played[new] = when
            for name in ("last_song", "selected"):
                if app.get(name) == old:
                    app[name] = new
            self._bump(old)
            self._bump(new)
            queue = app.get("queue")
            if isinstance(queue, list) and old in queue:
                app["queue"] = [new if k == old else k for k in queue]
        self.save()


# ============================= LIBRARY ==============================

SONG_CHOICES = {
    "range_mode": ("octave", "skip", "88"),
    "note_style": ("real", "short", "legato"),
    "pedal_mode": ("off", "song", "auto", "always"),
    "loop_mode": ("off", "song", "ab"),
}
HOTKEY_ACTIONS = ("play_pause", "stop", "restart", "slower", "faster", "next")
DEFAULT_HOTKEYS = {"play_pause": "F6", "stop": "F7", "restart": "HOME", "slower": "PAGEDOWN",
                   "faster": "PAGEUP", "next": "END"}
APP_DEFAULTS = {
    "pedal_method": "hold",      # hold = hold Space, tap = tap Space on/off, notes = hold the notes instead
    "shift_gap_ms": 0,
    "chord_window_ms": 4,
    "presets": [],               # your own humanizer presets: {name, feel, mistakes}
    "hotkeys": DEFAULT_HOTKEYS,  # action -> key name ("" = off)
    "key_hold_ms": 20,           # shortest time a key is held down
    "layout": "",                # "" = the standard Roblox layout, else a custom one
    "layout_lowest": 36,
    "has_88": None,              # does this piano play the 88-key extras? (answered in the calibration test)
    "auto_setup": True,          # give newly added songs the "Best for this song" settings
}


def _int(value, default, lo, hi):
    try:
        return min(hi, max(lo, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def clean_song_settings(raw, song):
    """Validated settings for a song (speed, transpose, tracks and sound options)."""
    try:
        speed = float(raw.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    speed = min(2.0, max(0.25, round(speed, 2)))
    transpose = _int(raw.get("transpose", 0), 0, -12, 12)
    playable = [t.number for t in song.tracks if t.notes and t.channels != {9}]
    tracks = raw.get("tracks")
    if isinstance(tracks, list):
        tracks = [int(t) for t in tracks if isinstance(t, (int, float)) and int(t) in playable]
    else:
        tracks = playable
    cfg = {"speed": speed, "transpose": transpose, "tracks": tracks}
    for name, choices in SONG_CHOICES.items():
        value = raw.get(name, choices[0])
        cfg[name] = value if value in choices else choices[0]
    cfg["max_notes"] = _int(raw.get("max_notes", 0), 0, 0, 10)
    cfg["cleanup"] = bool(raw.get("cleanup", True))
    cfg["feel"] = _int(raw.get("feel", 0), 0, 0, 100)
    cfg["mistakes"] = _int(raw.get("mistakes", 0), 0, 0, 100)
    seed = raw.get("seed")
    cfg["lock_take"] = bool(raw.get("lock_take")) and isinstance(seed, int)
    cfg["seed"] = seed if isinstance(seed, int) else None
    duration = max(0.0, song.duration)
    for name in ("loop_a", "loop_b"):
        try:
            cfg[name] = round(min(duration, max(0.0, float(raw.get(name, 0.0 if name == "loop_a" else duration)))), 3)
        except (TypeError, ValueError):
            cfg[name] = 0.0 if name == "loop_a" else round(duration, 3)
    if cfg["loop_mode"] == "ab" and cfg["loop_b"] - cfg["loop_a"] < 0.3:
        cfg["loop_mode"] = "off"
    return cfg


def loop_window(cfg, plan):
    """Loop range in plan time (seconds at the chosen speed), or None."""
    if cfg["loop_mode"] == "song":
        return (0.0, plan.duration + 1.0)
    if cfg["loop_mode"] == "ab":
        return (cfg["loop_a"] / cfg["speed"], cfg["loop_b"] / cfg["speed"])
    return None


def clean_presets(raw):
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name", "")).split())[:24]
        if not name or name.lower() in seen or name in pp.HUMAN_PRESETS:
            continue
        seen.add(name.lower())
        out.append({"name": name, "feel": _int(item.get("feel"), 0, 0, 100),
                    "mistakes": _int(item.get("mistakes"), 0, 0, 100)})
    return out[:12]


def clean_app_settings(raw):
    raw = raw or {}
    method = raw.get("pedal_method", APP_DEFAULTS["pedal_method"])
    return {"pedal_method": method if method in ("hold", "tap", "notes") else "hold",
            "shift_gap_ms": _int(raw.get("shift_gap_ms", 0), 0, 0, 60),
            "chord_window_ms": _int(raw.get("chord_window_ms", 4), 4, 0, 40),
            "presets": clean_presets(raw.get("presets")),
            "hotkeys": clean_hotkeys(raw.get("hotkeys")),
            "key_hold_ms": _int(raw.get("key_hold_ms", 20), 20, 10, 120),
            "has_88": raw.get("has_88") if isinstance(raw.get("has_88"), bool) else None,
            "auto_setup": raw.get("auto_setup", True) is not False,
            **clean_layout(raw.get("layout", ""), raw.get("layout_lowest", 36))}


def clean_layout(layout, lowest):
    layout = str(layout or "").strip().replace(" ", "")
    lowest = _int(lowest, 36, 12, 108)
    if layout and (layout == pp.STANDARD_LAYOUT and lowest == pp.STANDARD_LOWEST or pp.check_layout(layout, lowest)):
        layout = ""
    return {"layout": layout, "layout_lowest": lowest if layout else 36}


def apply_layout(app):
    """Point the engine at the chosen key layout."""
    if app["layout"]:
        pp.set_layout(app["layout"], app["layout_lowest"])
    else:
        pp.set_layout()


def clean_hotkeys(raw):
    raw = raw if isinstance(raw, dict) else {}
    out, used = {}, set()
    for action in HOTKEY_ACTIONS:
        key = str(raw.get(action, DEFAULT_HOTKEYS[action]) or "").upper()
        if key and (key not in pp.HOTKEY_CODES or key in used):
            key = ""
        out[action] = key
        if key:
            used.add(key)
    return out


def best_settings(song, cfg, app):
    """The settings that should suit this song, plus one plain sentence for each change.
    Speed, Play like (feel, mistakes, takes) and the loop are a matter of taste, so they stay as they are."""
    apply_layout(app)
    new = dict(cfg)
    said = []
    from_audio = song.source == "audio"

    # Tracks: every part with music in it. Drums are never played; nearly silent parts are left out.
    music = [t for t in song.tracks if t.notes and t.channels != {9}]
    loudness = {}
    for n in song.notes:
        if n.channel != 9:
            total, count = loudness.get(n.track, (0, 0))
            loudness[n.track] = (total + n.velocity, count + 1)
    quiet = [t for t in music if loudness.get(t.number, (0, 1))[0] / max(1, loudness.get(t.number, (0, 1))[1]) < 12]
    tracks = [t.number for t in music if t not in quiet] or [t.number for t in music]
    quiet = [t for t in quiet if t.number not in tracks]
    if not music:                       # drums only, or no notes at all: nothing the piano can play
        return clean_song_settings(cfg, song), []
    turned_on = [t for t in tracks if t not in cfg["tracks"]]
    if turned_on:
        if len(music) == 1:
            said.append("Turned the song's only track back on.")
        elif quiet:
            said.append("Turned the song's parts back on, so you hear all of the music.")
        else:
            said.append("Turned every part of the song back on, so you hear all of it.")
    for t in quiet:
        if t.number in cfg["tracks"]:
            said.append(f"Left out “{t.name}”: its notes are almost silent.")
    new["tracks"] = tracks
    chosen = set(tracks)
    notes = pp.playable_notes(song, chosen)
    pitches = [n.pitch for n in notes]
    total = max(1, len(pitches))

    # Fit on the piano. Keep the song's own key when (nearly) everything fits as written; then the 88-key
    # extras if this piano has them; otherwise shift the whole song so the most notes fit.
    def outside(shift, low, high):
        return sum(1 for p in pitches if not low <= p + shift <= high)
    low, high = pp.LOWEST_NOTE, pp.HIGHEST_NOTE
    # 88-key notes: when the calibration test said this piano has them, or (before anyone answered it)
    # when this song was already set to use them by hand.
    use_88 = pp.is_standard_layout() and (app.get("has_88") is True
                                          or (app.get("has_88") is None and cfg["range_mode"] == "88"))
    out_61 = outside(0, low, high)
    if out_61 == 0:
        shift, mode = 0, None
    elif use_88 and outside(0, pp.LOWEST_88, pp.HIGHEST_88) / total <= 0.01:
        shift, mode = 0, "88"               # every note stays where it's written
    elif out_61 / total <= 0.01:
        shift, mode = 0, "octave"
    else:
        # Moving a whole octave keeps the song in its own key, so it sounds the same, only higher or lower.
        # Another shift changes the key; it's only worth it when it fits clearly more notes.
        fits = {s: total - outside(s, low, high) for s in range(-12, 13)}
        octave = max((0, -12, 12), key=lambda s: (fits[s], -abs(s)))
        other = max(range(-12, 13), key=lambda s: (fits[s], -abs(s)))
        clearly = fits[other] - fits[octave] >= max(3, 0.02 * total)
        shift = other if clearly else octave
        mode = "octave" if outside(shift, low, high) else None
    if mode is None:    # nothing ends up outside the piano, so that setting doesn't matter: leave it as it is
        mode = "octave" if cfg["range_mode"] == "88" and not use_88 else cfg["range_mode"]
    left = outside(shift, *((pp.LOWEST_88, pp.HIGHEST_88) if mode == "88" else (low, high)))
    if not left:
        fits_text = "every note fits on the piano."
    elif left == 1:
        fits_text = "every note fits except 1, which moves by an octave."
    else:
        fits_text = f"every note fits except {left}, which move by an octave."
    if shift != cfg["transpose"]:
        if shift == 0:
            said.append("Back to the song's own key: " + ("every note fits as written." if not left else fits_text))
        else:
            way = "higher" if shift > 0 else "lower"
            if abs(shift) == 12:
                said.append(f"The whole song now plays an octave {way} than written, so {fits_text}")
            else:
                said.append(f"The whole song now plays {abs(shift)} {'key' if abs(shift) == 1 else 'keys'} {way} "
                            f"than written, so {fits_text} This changes its key a little.")
    if mode != cfg["range_mode"]:
        if mode == "88":
            said.append("Using your piano's extra 88-key notes, so every note stays where it's written.")
        elif cfg["range_mode"] == "skip":
            said.append("Notes outside the piano now move by an octave instead of being left out.")
        else:
            said.append("Notes outside the piano move by an octave"
                        + (" (the calibration test found this piano has no 88-key notes)." if app.get("has_88") is False else "."))
    new["transpose"], new["range_mode"] = shift, mode

    # Pedal
    presses = len(pp.pedal_intervals(song, notes, "song", chosen)) if song.pedal else 0
    if from_audio:
        pedal = "off"
        why = "Pedal off: songs made from recordings already hold each note as long as the pedal did."
    elif presses >= 2:
        pedal, why = "song", f"Using the song's own pedal ({presses} presses)."
    else:
        lengths = sorted(n.end - n.start for n in notes) or [0.0]
        flowing = lengths[len(lengths) // 2] >= 0.25 and pp.song_stats(song, chosen)["nps"] <= 8
        pedal = "auto" if flowing else "off"
        why = ("Added an automatic pedal: it's a slow, flowing piece that sounds better with sustain."
               if flowing else "Pedal off: this song is quick and detailed, and sustain would blur it.")
    if pedal != cfg["pedal_mode"]:
        said.append(why)
    new["pedal_mode"] = pedal

    # Keys at once: only huge chords get a limit (they sound muddy and swamp the melody).
    groups = pp.onset_groups(notes, 0.03)
    big = sum(1 for g in groups if len({n.pitch for n in g}) > 8)
    max_notes = 8 if groups and big >= max(3, 0.02 * len(groups)) else 0
    if max_notes != cfg["max_notes"]:
        said.append(f"Limited chords to 8 keys: {big} chords had more, which sounds muddy. The melody and bass "
                    "are always kept." if max_notes else "Removed the chord limit, so every chord plays in full.")
    new["max_notes"] = max_notes

    # Cleanup: always on (it only removes doubled notes and tiny glitches)
    if not cfg["cleanup"]:
        said.append("Turned Cleanup on" + (" to tidy up the recording's stray notes." if from_audio else "."))
    new["cleanup"] = True

    # Note length: short taps only when fast repeated notes would blur together. Counted after Cleanup, so
    # the same note doubled in two parts (struck once) isn't mistaken for a fast repeat.
    by_pitch = {}
    for n in pp.clean_notes(notes)[0]:
        by_pitch.setdefault(n.pitch + shift, []).append(n.start)
    repeats = 0
    for starts in by_pitch.values():
        starts.sort()
        repeats += sum(1 for a, b in zip(starts, starts[1:]) if 0.015 < b - a < 0.09)
    style = "short" if repeats / total > 0.10 else "real"
    if style != cfg["note_style"]:
        said.append("Short key taps, so the fast repeated notes stay clear." if style == "short"
                    else "Keys are held for each note's real length.")
    new["note_style"] = style
    return clean_song_settings(new, song), said


SETUP_TIME_LIMIT = 1.0      # seconds of setup per folder scan; a big drop of songs is spread over a few scans


def setup_new_songs(library, settings, keys):
    """Give songs that were just added the "Best for this song" settings (unless that's turned off).
    Returns (the keys dealt with, [(key, settings)] for the songs set up, {key: change number}).
    Keys left over when the time limit runs out are not dealt with; the next scan does them."""
    app = clean_app_settings(settings.app("player"))
    if not app["auto_setup"]:
        return list(keys), [], {}
    started = time.monotonic()
    handled, made, notes, expect = [], {}, {}, {}
    for key in keys:
        if made and time.monotonic() - started > SETUP_TIME_LIMIT:
            break
        handled.append(key)
        try:
            song, _meta = library.get(key)
            stored, rev = settings.song_state(key)
            defaults = clean_song_settings({}, song)
            current = clean_song_settings(stored, song)
            # Settings Best would change were already set (a file that was moved back, or changed by hand
            # before setup got to it): leave them. A speed nudge alone doesn't count.
            if any(current[name] != defaults[name] for name in BEST_KEYS):
                continue
            best, said = best_settings(song, current, app)
        except Exception:                   # this song stays on plain settings
            traceback.print_exc()
            continue
        made[key], expect[key] = best, rev
        if said:
            notes[key] = {"changes": said, "previous": current}
    revs = settings.set_songs(made, expect) if made else {}   # if saving fails, nothing counts as done
    SETUP_NOTES.update({key: note for key, note in notes.items() if key in revs})
    return handled, [(key, best) for key, best in made.items() if key in revs], revs


BEST_KEYS = ("tracks", "transpose", "range_mode", "pedal_mode", "max_notes", "cleanup", "note_style")


SETUP_NOTES = {}        # song key -> what automatic setup changed, shown once on the song page


def play_options(cfg, app, seed=None):
    human = None
    if cfg["feel"] or cfg["mistakes"]:
        human = pp.Humanizer(cfg["feel"], cfg["mistakes"], cfg["seed"] if cfg["lock_take"] else seed)
    return pp.PlayOptions(range_mode=cfg["range_mode"], max_notes=cfg["max_notes"], note_style=cfg["note_style"],
                          cleanup=cfg["cleanup"], pedal_mode=cfg["pedal_mode"],
                          pedal_output="notes" if app["pedal_method"] == "notes" else "key",
                          chord_window_ms=app["chord_window_ms"], min_hold_ms=app["key_hold_ms"], humanize=human)


def make_plan(song, cfg, app, seed=None):
    apply_layout(app)
    return pp.build_plan(song, speed=cfg["speed"], transpose=cfg["transpose"],
                         tracks=set(cfg["tracks"]), options=play_options(cfg, app, seed))


def nothing_to_play(song, cfg):
    """Why a song has no keys to press, in words the player can act on."""
    if not cfg["tracks"]:
        return "Pick at least one track to play."
    chosen = [n for n in song.notes if n.track in set(cfg["tracks"]) and n.channel != 9]
    if chosen and cfg["range_mode"] == "skip":
        return ("Nothing to play: every note is outside the piano's keys, and Notes outside the piano is set to "
                "skip them. Choose Move an octave or 88 keys.")
    return "Nothing to play: the chosen tracks have no notes."


class SongQueue:
    """Songs to play next, saved between sessions."""

    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()

    def items(self):
        q = self.settings.app("queue", [])
        return [k for k in q if isinstance(k, str)] if isinstance(q, list) else []

    def _save(self, items):
        self.settings.set_app("queue", items[:200])
        EVENTS.send("queue", {"items": items[:200]})

    def change(self, action, key=None, index=None, all_keys=()):
        with self.lock:
            items = self.items()
            if action == "add" and key:
                items.append(key)
            elif action == "next" and key:
                items.insert(0, key)
            elif action == "remove" and index is not None and 0 <= index < len(items):
                items.pop(index)
            elif action == "move" and index is not None and 0 < index < len(items):
                items[index - 1], items[index] = items[index], items[index - 1]
            elif action == "clear":
                items = []
            elif action == "shuffle":
                random.shuffle(items)
            elif action == "shuffle_all":
                items = list(all_keys)
                random.shuffle(items)
            self._save(items)
            return items

    def pop_next(self, exists):
        with self.lock:
            items = self.items()
            while items:
                key = items.pop(0)
                if exists(key):
                    self._save(items)
                    return key
            self._save(items)
            return None


class Library:
    def __init__(self, folder, settings):
        self.folder = folder
        self.settings = settings
        self.lock = threading.RLock()      # re-entrant: new songs are set up while the lock is held
        self.cache = {}            # key -> (mtime, size, song or None, meta)
        self.sig = ""
        self.scanned = False       # the first scan of this session
        self.on_new = None         # called with the keys of newly added songs
        self.waiting = {}          # new song -> (mtime, size) while it may still be copying in
        self.known = None          # key -> [mtime, size] of the songs already seen (kept between sessions)
        self.recent_gone = {}      # song that vanished -> (mtime, size, scan) for a few scans, to catch moves
        self.scan_no = 0
        self.failed = {}           # new song -> how many times setting it up failed

    def _files(self):
        found = []
        for path in self.folder.rglob("*"):
            if path.suffix.lower() not in MIDI_TYPES or path.name.startswith("."):
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:         # can't look at it right now (busy): the scan keeps what it knew about it
                pass
            found.append(path)
        return found

    def _meta(self, key, path, song, error=None):
        title = path.stem
        if error:
            return {"key": key, "title": title, "error": error}
        stats = pp.song_stats(song)
        tracks = [{"number": t.number, "name": t.name, "notes": t.notes,
                   "low": pp.note_name(t.low), "high": pp.note_name(t.high),
                   "drums": t.channels == {9}} for t in song.tracks if t.notes]
        return {"key": key, "title": title, "duration": round(stats["duration"], 2),
                "notes": stats["notes"], "nps": stats["nps"], "difficulty": stats["difficulty"],
                "tracks": tracks, "pedals": len(song.pedal),
                "folder": str(path.parent.relative_to(self.folder)).replace("\\", "/").strip(".")}

    def _known_from_settings(self):
        raw = self.settings.app("known_songs")
        if not isinstance(raw, dict):
            return None
        return {k: (v[0], v[1], True) for k, v in raw.items() if isinstance(v, list) and len(v) == 2}

    @staticmethod
    def _pair(olds, news):
        """Match songs that vanished with songs that appeared with the same size and time stamp:
        the same file name first (a move to another folder), then one left on each side (a rename)."""
        olds, news, pairs = list(olds), sorted(news), []
        for new in list(news):
            name = new.rsplit("/", 1)[-1]
            same = [old for old in olds if old.rsplit("/", 1)[-1] == name]
            if len(same) == 1:
                pairs.append((same[0], new))
                olds.remove(same[0])
                news.remove(new)
        if len(olds) == 1 and len(news) == 1:
            pairs.append((olds[0], news[0]))
        return pairs

    RECENT_GONE_SCANS = 4          # a move can show up a scan or two after the file vanished

    def _new_songs(self, seen, written):
        """Songs that just arrived and are ready to be set up, PianoPilot's own files first. Renamed or moved
        songs keep their settings instead; a file that's still being copied in waits until it stops changing."""
        if self.scanned:
            before = {k: (e[0], e[1], e[2] is not None) for k, e in self.cache.items()}
        else:
            before = self._known_from_settings()
            if before is None:              # the first time this version runs: the songs already here aren't new
                return []
        self.scan_no += 1
        for k, b in before.items():
            if b[2] and k not in seen:
                # remember whether it was still waiting to be set up, so a move doesn't lose its place in line
                self.recent_gone[k] = (b[0], b[1], self.scan_no, k in self.waiting)
        for k in list(self.recent_gone):
            if k in seen or self.scan_no - self.recent_gone[k][2] >= self.RECENT_GONE_SCANS:
                del self.recent_gone[k]
        arrived = [k for k, e in seen.items() if e[2] is not None and not (k in before and before[k][2])]
        # The new name of a moved song can also be one that showed up a scan earlier and is still waiting
        # (a scan that saw both names in the middle of a move, or a program that copies and then deletes).
        candidates = set(arrived) | {k for k in self.waiting if k in seen and seen[k][2] is not None}
        gone, came = {}, {}
        for k, (mtime, size, _scan, _was_waiting) in self.recent_gone.items():
            gone.setdefault((mtime, size), []).append(k)
        for k in candidates:
            came.setdefault((seen[k][0], seen[k][1]), []).append(k)
        for stamp, keys in came.items():
            for old, new in self._pair(gone.get(stamp, ()), keys):
                was_waiting = self.recent_gone.pop(old)[3]
                self.settings.move_song(old, new)       # its star, queue spot and settings go with it
                if was_waiting:                         # not set up yet: the new name takes its place in line
                    self.waiting[new] = stamp
                else:                                   # a song that was already here: not new at all
                    if new in arrived:
                        arrived.remove(new)
                    self.waiting.pop(new, None)
        now = time.time()
        ready, waiting = [], {}
        for k in set(arrived) | (set(self.waiting) & set(seen)):
            if seen[k][2] is None:
                continue
            stamp = (seen[k][0], seen[k][1])
            settled = self.waiting.get(k) == stamp
            if not self.scanned and now - stamp[0] > 10:
                settled = True      # at startup, a file that hasn't changed lately isn't still being copied
            if k in written or settled:
                ready.append(k)
            else:
                waiting[k] = stamp
        self.waiting = waiting
        return sorted(ready, key=lambda k: (k not in written, k))

    def refresh(self, written=()):
        """Rescan the Songs folder. written: songs PianoPilot itself just saved there (complete already)."""
        with self.lock:
            seen = {}
            for path in self._files():
                key = path.relative_to(self.folder).as_posix()
                try:
                    st = path.stat()
                except OSError:             # gone for a moment (being moved, say): keep it until the next scan
                    if key in self.cache:
                        seen[key] = self.cache[key]
                    continue
                old = self.cache.get(key)
                if old and old[0] == st.st_mtime and old[1] == st.st_size:
                    seen[key] = old
                    continue
                try:
                    song = pp.load_midi(str(path))
                    meta = self._meta(key, path, song)
                except Exception as exc:
                    song, meta = None, self._meta(key, path, None, error=f"Couldn't read this MIDI file ({exc})")
                seen[key] = (st.st_mtime, st.st_size, song, meta)
            ready = self._new_songs(seen, set(written))
            self.cache = seen
            self.scanned = True
            digest = hashlib.sha1()
            for key in sorted(seen):
                digest.update(f"{key}|{seen[key][0]}|{seen[key][1]}\n".encode("utf-8"))
            changed = digest.hexdigest() != self.sig
            self.sig = digest.hexdigest()
            # Set new songs up before anyone else can look at them: another refresh (the folder watcher, or
            # the request that added the song) waits here until the settings are saved.
            done, broke = set(ready), False
            if ready and self.on_new:
                try:
                    done = set(self.on_new(ready))
                except Exception:
                    traceback.print_exc()
                    done, broke = set(), True
            for k in ready:
                if k in done:
                    self.failed.pop(k, None)
                    continue
                if broke:
                    self.failed[k] = self.failed.get(k, 0) + 1
                    if self.failed[k] >= 3:         # give up: it plays with plain settings
                        continue
                self.waiting[k] = (seen[k][0], seen[k][1])     # unchanged, so it's ready again on the next scan
            # Only now remember the songs as known: a song that wasn't set up yet still counts as new next time.
            known = {k: [e[0], e[1]] for k, e in seen.items() if e[2] is not None and k not in self.waiting}
            if known != self.known:
                try:
                    self.settings.set_app("known_songs", known)
                    self.known = known
                except Exception:
                    traceback.print_exc()
        return changed

    def listing(self):
        with self.lock:
            items = [dict(entry[3]) for entry in self.cache.values()]
        favorites = set(self.settings.app("favorites", []) or [])
        played = self.settings.app("played", {}) or {}
        for item in items:
            item["favorite"] = item["key"] in favorites
            item["played"] = played.get(item["key"], 0)
        items.sort(key=lambda m: m["title"].lower())
        return {"sig": self.sig, "songs": items}

    def exists(self, key):
        with self.lock:
            entry = self.cache.get(key)
        return bool(entry and entry[2] is not None)

    def get(self, key):
        with self.lock:
            entry = self.cache.get(key)
        if not entry or entry[2] is None:
            raise ApiError(404, "That song isn't in the Songs folder anymore.")
        return entry[2], entry[3]

    def path_for(self, key):
        path = (self.folder / key).resolve()
        if self.folder.resolve() not in path.parents:
            raise ApiError(400, "Bad song name.")
        return path

    def unique_path(self, title, suffix=".mid"):
        safe = "".join(c for c in title if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")
        safe = safe[:120] or "Song"
        path = self.folder / f"{safe}{suffix}"
        n = 2
        while path.exists():
            path = self.folder / f"{safe} ({n}){suffix}"
            n += 1
        return path

    def watch(self):
        while True:
            try:
                if self.refresh():
                    EVENTS.send("library", {"sig": self.sig})
            except Exception:
                traceback.print_exc()
            time.sleep(1.5)


# ============================= PLAYBACK =============================

class SimKeyboard:
    """Stands in for the real keyboard when not on Windows (for testing)."""

    def __init__(self):
        self.presses = 0

    def send(self, ops):
        self.presses += sum(1 for k, d in ops if d and k != "shift")

    def is_focused(self):
        return True

    def poll_hotkeys(self):
        return set()

    def close(self):
        pass


class Playback:
    """Runs one Player at a time. Every control goes through one lock, so a double-click
    on Play can never start two players."""

    def __init__(self, library, queue_):
        self.library = library
        self.queue = queue_
        self.lock = threading.RLock()
        self.player = None
        self.thread = None
        self.key = None
        self.speed = 1.0
        self.cfg = None
        self.app_cfg = None
        self.seed = None
        self.plan = None
        self.last_seeds = {}       # song -> seed of its most recent take
        self.status = {"state": "idle", "pos": 0.0, "total": 0.0, "key": None}

    def _publisher(self, player, key):
        def publish(info):
            if player is not self.player:          # an old player winding down: ignore it
                return
            speed = self.speed
            status = {"state": info.get("state"), "reason": info.get("reason"), "count": info.get("count"),
                      "pos": round(info.get("pos", 0.0) * speed, 3), "total": round(info.get("total", 0.0) * speed, 3),
                      "key": key, "timing": info.get("timing"), "simulated": SIMULATE,
                      "loops": player.loops_done}
            if info.get("state") == "finished" and self.plan is not None and self.plan.report.get("human"):
                status["mistakes"] = self.plan.report["human"].get("mistakes", 0)
            upcoming = self.queue.items()
            if upcoming:
                status["next"] = upcoming[0]
            self.status = status
            EVENTS.send("status", status)
            if info.get("state") == "finished" and upcoming and key != "__test__":
                threading.Thread(target=self._advance, args=(player,), daemon=True).start()
        return publish

    def _advance(self, finished_player):
        """Play the next song in the queue a moment after one finishes."""
        time.sleep(2.0)
        with self.lock:
            if self.player is not finished_player:     # something else started meanwhile
                return
            nxt = self.queue.pop_next(self.library.exists)
            if nxt:
                try:
                    self.play(nxt, countdown=0)
                except ApiError as exc:
                    EVENTS.send("notice", {"text": exc.message, "bad": True})

    def _blocked(self):
        EVENTS.send("notice", {"text": "Windows blocked the key presses. If Roblox is running as administrator, "
                                       "close PianoPilot and start it as administrator too.", "bad": True})

    def play(self, key, start_at=0.0, countdown=None):
        with self.lock:
            song, _meta = self.library.get(key)
            cfg = clean_song_settings(self.library.settings.song(key), song)
            app = clean_app_settings(self.library.settings.app("player"))
            seed = cfg["seed"] if cfg["lock_take"] else random.randrange(1, 2 ** 31)
            plan = make_plan(song, cfg, app, seed)
            if not plan.steps:
                raise ApiError(400, nothing_to_play(song, cfg))
            self._stop_locked(wait=True)
            if SIMULATE:
                keyboard = SimKeyboard()
            else:
                keyboard = pp.WindowsKeyboard(hotkeys={})     # hotkeys are handled by HotkeyWatcher
            keyboard.on_blocked = self._blocked
            player = pp.Player(keyboard, show_progress=False,
                               countdown=pp.COUNTDOWN_SECONDS if countdown is None else countdown,
                               shift_gap_ms=app["shift_gap_ms"],
                               pedal_style="tap" if app["pedal_method"] == "tap" else "hold")
            player.on_status = self._publisher(player, key)
            player.loop = loop_window(cfg, plan)
            self.key = key
            self.speed, self.cfg, self.app_cfg, self.seed, self.plan = cfg["speed"], cfg, app, seed, plan
            self.last_seeds[key] = seed
            self.player = player
            start_plan_time = max(0.0, float(start_at)) / cfg["speed"]

            def run():
                try:
                    player.play(plan, start_at=start_plan_time)
                except Exception as exc:
                    traceback.print_exc()
                    player.on_status({"state": "error", "pos": 0, "total": plan.duration, "reason": str(exc)})
                finally:
                    keyboard.close()

            self.thread = threading.Thread(target=run, name="player", daemon=True)
            self.thread.start()
        settings = self.library.settings
        settings.set_app("last_song", key)
        played = dict(settings.app("played", {}) or {})
        played[key] = int(time.time())
        if len(played) > 2000:
            played = dict(sorted(played.items(), key=lambda kv: kv[1])[-2000:])
        settings.set_app("played", played)
        return {"ok": True}

    def play_custom(self, notes, title, pedal=False, range_mode="octave"):
        """Play notes that aren't a song in the library (the calibration test)."""
        with self.lock:
            app = clean_app_settings(self.library.settings.app("player"))
            apply_layout(app)
            song = pp.Song(title, list(notes), [], [pp.TrackInfo(1, title, len(notes), {0}, 0, 127)],
                           max(n.end for n in notes))
            opts = pp.PlayOptions(pedal_mode="always" if pedal else "off", range_mode=range_mode,
                                  pedal_output="notes" if app["pedal_method"] == "notes" else "key",
                                  chord_window_ms=app["chord_window_ms"], min_hold_ms=app["key_hold_ms"])
            plan = pp.build_plan(song, options=opts)
            self._stop_locked(wait=True)
            keyboard = SimKeyboard() if SIMULATE else pp.WindowsKeyboard(hotkeys={})
            keyboard.on_blocked = self._blocked
            player = pp.Player(keyboard, show_progress=False, shift_gap_ms=app["shift_gap_ms"],
                               pedal_style="tap" if app["pedal_method"] == "tap" else "hold")
            player.on_status = self._publisher(player, "__test__")
            self.key, self.speed, self.cfg, self.plan, self.player = "__test__", 1.0, None, plan, player

            def run():
                try:
                    player.play(plan)
                finally:
                    keyboard.close()
            self.thread = threading.Thread(target=run, name="player", daemon=True)
            self.thread.start()
        return {"ok": True}

    def _stop_locked(self, wait):
        player, thread = self.player, self.thread
        if player and thread and thread.is_alive():
            player.request_stop()
            if wait:
                thread.join(5)

    def stop(self, wait=False):
        with self.lock:
            self._stop_locked(wait)
        return {"ok": True}

    def active(self):
        return self.player is not None and self.thread is not None and self.thread.is_alive()

    def pause(self):
        with self.lock:
            if self.active():
                self.player.request_pause()
        return {"ok": True}

    def resume(self):
        with self.lock:
            if self.active():
                self.player.request_resume()
        return {"ok": True}

    def seek(self, seconds):
        with self.lock:
            if self.active():
                self.player.request_seek(max(0.0, float(seconds)) / self.speed)
        return {"ok": True}

    def next(self):
        with self.lock:
            nxt = self.queue.pop_next(self.library.exists)
            if not nxt:
                raise ApiError(400, "The queue is empty. Add songs to it first.")
            return self.play(nxt, countdown=0 if self.active() else None)

    def settings_changed(self, key):
        """Apply new song settings to the song that's playing, without restarting."""
        with self.lock:
            if not self.active() or key != self.key or key == "__test__":
                return
            song, _meta = self.library.get(key)
            cfg = clean_song_settings(self.library.settings.song(key), song)
            if cfg == self.cfg:
                return
            seed = cfg["seed"] if cfg["lock_take"] else self.seed
            plan = make_plan(song, cfg, self.app_cfg, seed)
            if not plan.steps:
                return
            position_seconds = self.player.position() * self.speed
            self.speed, self.cfg, self.seed, self.plan = cfg["speed"], cfg, seed, plan
            # plan, position and loop go over as one command, so the player never mixes old and new
            self.player.request_replace(plan, position_seconds / cfg["speed"], loop=loop_window(cfg, plan))

    # ---- hotkeys ----
    def hotkey(self, action, chosen=None):
        """chosen: the song picked in the window that sent the hotkey (it knows best what's picked)."""
        settings = self.library.settings
        with self.lock:
            active = self.active()
            playing = self.key if active and self.key != "__test__" else None
            chosen = chosen if chosen and self.library.exists(chosen) else None
            key = playing or chosen or settings.app("selected") or settings.app("last_song")
            state = self.status.get("state")
            try:
                if action == "play_pause":
                    if active and state == "paused" and self.status.get("reason") == "manual":
                        self.resume()
                    elif active:
                        self.pause()
                    elif key and self.library.exists(key):
                        self.play(key)
                elif action == "stop":
                    self.stop()
                elif action == "restart":
                    if active:
                        self.seek(0.0)
                    elif key and self.library.exists(key):
                        self.play(key)
                elif action in ("slower", "faster") and key and self.library.exists(key):
                    step = 0.05 if action == "faster" else -0.05

                    def change(stored):
                        cfg = clean_song_settings(stored, song)
                        return clean_song_settings(dict(cfg, speed=round(cfg["speed"] + step, 2)), song), None

                    with self.library.lock:          # so a delete can't slip in between
                        song, _meta = self.library.get(key)
                        cfg, rev, _ = settings.update_song(key, change)
                    self.settings_changed(key)
                    EVENTS.send("song-settings", {"key": key, "settings": cfg, "rev": rev})
                    EVENTS.send("notice", {"text": f"Speed {cfg['speed']:.2f}x"})
                elif action == "next":
                    self.next()
            except ApiError as exc:
                EVENTS.send("notice", {"text": exc.message, "bad": True})


class HotkeyWatcher:
    """Global hotkeys (Windows) while Roblox is in front. PianoPilot's own windows handle the keys in the page,
    so Home, End and Page Up/Down still scroll and move around there."""

    def __init__(self, playback, settings):
        self.playback = playback
        self.settings = settings

    def run(self):
        if not IS_WINDOWS or SIMULATE:
            return
        try:
            kb = pp.WindowsKeyboard(hotkeys={}, timing=False)
        except Exception:
            traceback.print_exc()
            return
        current = None
        last_read = 0.0
        while True:
            try:
                now = time.time()
                if now - last_read > 1.0:
                    last_read = now
                    keys = clean_app_settings(self.settings.app("player"))["hotkeys"]
                    if keys != current:
                        current = keys
                        kb.hotkeys = {action: pp.HOTKEY_CODES[name] for action, name in keys.items() if name}
                        kb._was_down = {action: True for action in kb.hotkeys}
                pressed = kb.poll_hotkeys()
                if pressed and kb.foreground() == "roblox":
                    for action in pressed:
                        self.playback.hotkey(action)
            except Exception:
                traceback.print_exc()
                time.sleep(1)
            time.sleep(0.015)


# ========================== AUDIO IMPORT ============================

def python_command(settings):
    custom = settings.app("converter_python")
    if isinstance(custom, list) and custom:
        return [str(x) for x in custom]
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")
    if exe.name.lower() == "pythonw.exe" and console.is_file():
        return [str(console)]       # helpers print progress, so give them the console Python (still hidden)
    return [str(exe)]


def remember_python():
    """Save which Python runs PianoPilot, so PianoPilot.exe finds it next time (data/python.txt)."""
    if not IS_WINDOWS:
        return
    windowed = Path(sys.executable).with_name("pythonw.exe")
    if not windowed.is_file():
        return
    path = DATA_DIR / "python.txt"
    try:
        if not path.is_file() or path.read_text(encoding="utf-8").strip() != str(windowed):
            path.write_text(str(windowed), encoding="utf-8")
    except OSError:
        pass


def model_ready():
    try:
        manifest = json.loads((MODEL_DIR / "manifest.json").read_text(encoding="utf-8"))
        return all((MODEL_DIR / name).is_file() for name in MODEL_PARTS[:4]) and bool(manifest.get("sample_rate"))
    except Exception:
        return False


FOLDER_DOWNLOADS = "374DE290-123F-4565-9164-39C4925E467B"
FOLDER_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
FOLDER_PROGRAMS = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"      # Start menu > Programs


def known_folder(folder_id, fallback):
    """A Windows folder like Downloads or Desktop, even when it has been moved (for example into OneDrive)."""
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]
            g = uuid.UUID(folder_id)
            guid = GUID(g.fields[0], g.fields[1], g.fields[2], (ctypes.c_ubyte * 8)(*g.bytes[8:]))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
                path = Path(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
                return path
        except Exception:
            pass
    return fallback


def downloads_folder():
    return known_folder(FOLDER_DOWNLOADS, Path.home() / "Downloads")


def model_files():
    """Every copy of the model file in the usual places, most likely first."""
    candidates = [APP_DIR / MODEL_FILE_NAME, downloads_folder() / MODEL_FILE_NAME,
                  Path.home() / "Downloads" / MODEL_FILE_NAME, APP_DIR.parent / MODEL_FILE_NAME,
                  DATA_DIR / MODEL_FILE_NAME]
    out = []
    for path in candidates:
        if path.is_file() and path.resolve() not in [p.resolve() for p in out]:
            out.append(path)
    return out


def find_model_file():
    found = model_files()
    return found[0] if found else None


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_model(path):
    if sha256_of(path) != MODEL_SHA256:
        raise RuntimeError(f"The {path.name} in {path.parent} isn't the genuine Pianoscribe model (it may be an "
                           f"unfinished download). Download it again from {MODEL_URL}")
    tmp = MODEL_DIR.with_name("model.partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for name in MODEL_PARTS:
            if name not in names:
                raise RuntimeError(f"The model file is missing {name}.")
            with archive.open(name) as src, open(tmp / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    shutil.rmtree(MODEL_DIR, ignore_errors=True)
    os.replace(tmp, MODEL_DIR)


class AudioImport:
    """First-time setup, plus a queue of audio files to convert one at a time."""

    def __init__(self, library, settings):
        self.library = library
        self.settings = settings
        self.lock = threading.Lock()
        self.jobs = []
        self.wake = threading.Event()
        self.current_proc = None
        self.check_cache = None
        self.setup = {"running": False, "step": "", "log": [], "error": None, "done": False}
        INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        for leftover in INCOMING_DIR.glob("*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        threading.Thread(target=self._worker, name="converter", daemon=True).start()

    # ---- status / setup ----
    def check_deps(self, refresh=False):
        if self.check_cache is not None and not refresh:
            return self.check_cache
        cmd = python_command(self.settings) + [str(APP_DIR / "converter.py"), "--check"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
            line = [ln for ln in out.stdout.splitlines() if ln.startswith("{")][-1]
            result = json.loads(line)
        except Exception as exc:
            result = {"ok": False, "missing": ["python"], "error": str(exc)}
        self.check_cache = result
        return result

    def status(self, refresh=False):
        deps = self.check_deps(refresh)
        found = find_model_file()
        return {"model": model_ready(), "model_file": str(found) if found else None, "model_url": MODEL_URL,
                "deps": deps, "ready": model_ready() and bool(deps.get("ok")), "setup": self.setup_view()}

    def setup_view(self):
        with self.lock:
            return {k: (list(v[-60:]) if k == "log" else v) for k, v in self.setup.items()}

    def _setup_log(self, text, step=None):
        with self.lock:
            if step is not None:
                self.setup["step"] = step
            if text:
                self.setup["log"].append(text)
                self.setup["log"] = self.setup["log"][-300:]
        EVENTS.send("setup", self.setup_view())

    def start_setup(self, use_python=None):
        with self.lock:
            if self.setup["running"]:
                return {"ok": True}
            self.setup = {"running": True, "step": "Starting", "log": [], "error": None, "done": False}
        threading.Thread(target=self._run_setup, args=(use_python,), daemon=True).start()
        return {"ok": True}

    def _run(self, cmd, step):
        self._setup_log("> " + " ".join(cmd), step)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._setup_log(line)
        return proc.wait()

    def _run_setup(self, use_python):
        try:
            if use_python:
                # Second Python just for audio import (when the main one is too new for the AI library).
                if self._run(["py", "install", "--yes", use_python], f"Installing Python {use_python}") != 0:
                    raise RuntimeError(f"Couldn't install Python {use_python}. Open a command window and run: "
                                       f"py install {use_python}")
                self.settings.set_app("converter_python", ["py", f"-V:{use_python}"])
                self.check_cache = None
            if not model_ready():
                found = model_files()
                if not found:
                    raise RuntimeError(f"Couldn't find {MODEL_FILE_NAME}. Download it from {MODEL_URL} "
                                       "into your Downloads folder, then click Set up again.")
                problem = None
                for path in found:            # a bad copy in one place mustn't hide a good one elsewhere
                    self._setup_log(f"Checking {path}", "Installing the piano model")
                    try:
                        install_model(path)
                        problem = None
                        break
                    except Exception as exc:
                        problem = exc
                        self._setup_log(str(exc))
                if problem:
                    raise RuntimeError(str(problem))
                self._setup_log("Piano model installed and verified.")
            deps = self.check_deps(refresh=True)
            if not deps.get("ok"):
                py = python_command(self.settings)
                if self._run(py + ["-m", "pip", "--version"], "Checking pip") != 0:
                    self._run(py + ["-m", "ensurepip", "--upgrade"], "Installing pip")
                base = py + ["-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "--no-input",
                             "--only-binary=:all:"]      # never try to compile anything; fail fast instead
                common = ["numpy", "scipy", "imageio-ffmpeg"]
                runtime = "onnxruntime-directml" if IS_WINDOWS else "onnxruntime"
                code = self._run(base + common + [runtime], "Downloading the AI libraries (about 150 MB)")
                if code != 0 and runtime != "onnxruntime":
                    self._setup_log("Graphics-card version didn't install; trying the CPU version.")
                    code = self._run(base + common + ["onnxruntime"], "Downloading the AI libraries (CPU version)")
                deps = self.check_deps(refresh=True)
                if code != 0 or not deps.get("ok"):
                    version = deps.get("python") or sys.version.split()[0]
                    raise RuntimeError(
                        f"The AI libraries wouldn't install for Python {version}. This usually means that "
                        "Python version is too new for them. Click \"Use Python 3.13\" to install a second, "
                        "older Python just for audio import.")
            with self.lock:
                self.setup.update(running=False, done=True, step="Ready", error=None)
            self._setup_log("Audio import is ready.")
        except Exception as exc:
            with self.lock:
                self.setup.update(running=False, done=False, error=str(exc))
            self._setup_log(f"Setup stopped: {exc}")
        EVENTS.send("setup", self.setup_view())

    # ---- jobs ----
    def jobs_view(self):
        with self.lock:
            return [{k: v for k, v in job.items() if k not in ("input",)} for job in self.jobs]

    def _publish_jobs(self):
        EVENTS.send("jobs", self.jobs_view())

    def add_job(self, name, input_path):
        size = Path(input_path).stat().st_size
        job = {"id": uuid.uuid4().hex[:10], "name": name, "title": Path(name).stem, "input": str(input_path),
               "size": size, "state": "queued", "done": 0, "total": 0, "text": "Waiting", "song": None, "error": None}
        with self.lock:
            # The same recording dropped in twice: it's already on its way, so don't convert it again.
            if any(j["name"] == name and j.get("size") == size and j["state"] in ("queued", "converting")
                   for j in self.jobs):
                Path(input_path).unlink(missing_ok=True)
                raise ApiError(409, f"“{Path(name).stem}” is already being turned into a song. "
                                    "You can watch it on the left.")
            self.jobs.append(job)
            self.jobs = [j for j in self.jobs if j["state"] in ("queued", "converting")] + \
                [j for j in self.jobs if j["state"] not in ("queued", "converting")][-8:]
        self._publish_jobs()
        self.wake.set()
        return job

    def cancel(self, job_id):
        with self.lock:
            for job in self.jobs:
                if job["id"] == job_id and job["state"] in ("queued", "converting"):
                    job.update(state="cancelled", text="Cancelled")
                    if self.current_proc is not None and job.get("running"):
                        try:
                            self.current_proc.kill()
                        except Exception:
                            pass
        self._publish_jobs()
        return {"ok": True}

    def dismiss(self, job_id):
        with self.lock:
            self.jobs = [j for j in self.jobs if j["id"] != job_id or j["state"] in ("queued", "converting")]
        self._publish_jobs()
        return {"ok": True}

    def _next_job(self):
        with self.lock:
            for job in self.jobs:
                if job["state"] == "queued":
                    job.update(state="converting", text="Starting", running=True)
                    return job
        return None

    def _worker(self):
        while True:
            self.wake.wait(2)
            self.wake.clear()
            while True:
                job = self._next_job()
                if not job:
                    break
                try:
                    self._convert(job)
                except Exception as exc:
                    traceback.print_exc()
                    with self.lock:
                        job.update(state="error", error=str(exc), text="Failed")
                finally:
                    job.pop("running", None)
                    try:
                        Path(job["input"]).unlink()
                    except OSError:
                        pass
                    self._publish_jobs()

    def _convert(self, job):
        if not model_ready() or not self.check_deps().get("ok"):
            with self.lock:
                job.update(state="error", text="Needs setup",
                           error="Audio import isn't set up yet. Click \"Set up audio import\" first.")
            return
        part = INCOMING_DIR / f"{job['id']}.mid"
        cmd = python_command(self.settings) + [str(APP_DIR / "converter.py"), "--model", str(MODEL_DIR),
                                               "--input", job["input"], "--output", str(part),
                                               "--device", self.settings.app("convert_device", "auto"),
                                               f"--title={job['title']}"]     # "=" so a title like "-Intro" works
        # stderr goes into the same pipe, so a chatty library can never fill an unread pipe and freeze it
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        with self.lock:
            self.current_proc = proc
        result, error, other = None, None, []
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    msg = None
                if not isinstance(msg, dict) or "type" not in msg:
                    if line.strip():
                        other.append(line.strip())
                        other[:] = other[-20:]
                    continue
                with self.lock:
                    if msg["type"] == "progress":
                        job.update(done=msg.get("done", 0), total=msg.get("total", 0))
                    elif msg["type"] == "status":
                        job["text"] = str(msg.get("text", ""))
                    elif msg["type"] == "done":
                        result = msg
                    elif msg["type"] == "error":
                        error = str(msg.get("text", ""))
                self._publish_jobs()
            code = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            with self.lock:
                self.current_proc = None
        with self.lock:
            cancelled = job["state"] == "cancelled"
        if cancelled or code != 0 or not result or not part.is_file():
            try:
                part.unlink()
            except OSError:
                pass
            if not cancelled:
                with self.lock:
                    job.update(state="error", text="Failed",
                               error=error or (other[-1] if other else "The converter stopped unexpectedly."))
            return
        output = self.library.unique_path(job["title"])     # chosen now, so nothing gets overwritten
        shutil.move(str(part), str(output))
        key = output.relative_to(SONGS_DIR).as_posix()
        with self.lock:
            job.update(state="done", song=key, notes=result.get("notes"), device=result.get("device"),
                       took=result.get("took"), text=f"{result.get('notes')} notes found")
        self.library.refresh(written={key})
        EVENTS.send("library", {"sig": self.library.sig, "select": key})


# ============================== SERVER ==============================

class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class App:
    def __init__(self):
        for folder in (SONGS_DIR, DATA_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(SETTINGS_FILE)
        self.library = Library(SONGS_DIR, self.settings)
        self.library.on_new = self.set_up_new_songs
        self.library.refresh()                 # also sets up songs that were added while PianoPilot was closed
        self.queue = SongQueue(self.settings)
        self.playback = Playback(self.library, self.queue)
        self.hotkeys = HotkeyWatcher(self.playback, self.settings)
        self.audio = AudioImport(self.library, self.settings)

    def set_up_new_songs(self, keys):
        handled, made, revs = setup_new_songs(self.library, self.settings, keys)
        for key, cfg in made:
            # A window may already show this song (a file dropped into the folder is set up a moment later).
            EVENTS.send("song-settings", {"key": key, "settings": cfg, "rev": revs[key], "setup": key in SETUP_NOTES})
            playback = getattr(self, "playback", None)
            if playback and playback.key == key:
                # Not from here: this runs inside the library lock, and playback takes its own lock first.
                threading.Thread(target=playback.settings_changed, args=(key,), daemon=True).start()
        return handled


APP = None
APP_READY = threading.Event()      # set once the library is loaded; the server answers pings before that


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        extra = dict(extra or {})
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _authorized(self, query):
        return self.headers.get("X-PianoPilot") == TOKEN or query.get("token", [""])[0] == TOKEN

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise ApiError(413, "Request too large.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Bad request.")
        return data if isinstance(data, dict) else {}

    def _save_upload(self, dest):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "The file was empty.")
        if length > MAX_UPLOAD:
            raise ApiError(413, "That file is too big (over 2 GB).")
        remaining = length
        with open(dest, "wb") as f:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ApiError(400, "The upload was cut off.")
                f.write(chunk)
                remaining -= len(chunk)

    # ---- routing ----
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if not self._host_ok():
                raise ApiError(403, "Forbidden")
            if method == "GET" and url.path in ("/", "/index.html", "/mini"):
                page = "mini.html" if url.path == "/mini" else "index.html"
                html = (UI_DIR / page).read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
                return self._send(200, html, "text/html; charset=utf-8")
            if method == "GET" and url.path.startswith("/fonts/"):
                name = Path(unquote(url.path)).name
                font = UI_DIR / "fonts" / name
                if font.suffix != ".ttf" or not font.is_file():
                    raise ApiError(404, "Not found")
                return self._send(200, font.read_bytes(), "font/ttf", {"Cache-Control": "max-age=86400"})
            if method == "GET" and url.path == "/api/ping":
                return self._send(200, {"app": APP_NAME, "version": VERSION})
            if not url.path.startswith("/api/"):
                raise ApiError(404, "Not found")
            if not self._authorized(query):
                raise ApiError(403, "Forbidden")
            if not APP_READY.wait(120):
                raise ApiError(503, "PianoPilot is still starting. Try again in a moment.")
            if url.path == "/api/events":
                return self._events()
            handler = ROUTES.get((method, url.path))
            if handler is None:
                raise ApiError(404, "Not found")
            result = handler(self, query)
            if result is not None:
                self._send(200, result)
        except ApiError as exc:
            self._send(exc.code, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            traceback.print_exc()
            try:
                self._send(500, {"error": f"Something went wrong: {exc}"})
            except Exception:
                pass

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = EVENTS.add()
        try:
            hello = {"status": APP.playback.status, "jobs": APP.audio.jobs_view(), "sig": APP.library.sig,
                     "simulated": SIMULATE, "queue": APP.queue.items(), "is_windows": IS_WINDOWS}
            self.wfile.write(f"event: hello\ndata: {json.dumps(hello)}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    payload = b": ping\n\n"
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            EVENTS.remove(q)
            self.close_connection = True


def q1(query, name, default=None):
    values = query.get(name)          # parse_qs has already decoded these
    return values[0] if values else default


def api_library(h, query):
    if q1(query, "since") == APP.library.sig:
        return {"sig": APP.library.sig, "unchanged": True}
    data = APP.library.listing()
    data["last_song"] = APP.settings.app("last_song")
    data["selected"] = APP.settings.app("selected")
    return data


def api_song(h, query):
    key = q1(query, "key", "")
    song, meta = APP.library.get(key)
    apply_layout(clean_app_settings(APP.settings.app("player")))
    stored, rev = APP.settings.song_state(key)
    cfg = clean_song_settings(stored, song)
    notes = [[round(n.start, 3), round(n.end, 3), n.pitch, n.track] for n in song.notes if n.channel != 9]
    return {"meta": meta, "settings": cfg, "notes": notes, "range": [pp.LOWEST_NOTE, pp.HIGHEST_NOTE],
            "range88": [pp.LOWEST_88, pp.HIGHEST_88], "report": plan_report(song, cfg),
            "layout": pp.KEY_LAYOUT, "standard": pp.is_standard_layout(), "details": pp.song_details(song),
            "presets": [{"name": k, "feel": v[0], "mistakes": v[1]} for k, v in pp.HUMAN_PRESETS.items()],
            "from_audio": song.source == "audio", "rev": rev,
            # what automatic setup did; only the song page asks for it (note=1), and it's shown once
            "setup_note": SETUP_NOTES.pop(key, None) if q1(query, "note", "") == "1" else None}


def plan_report(song, cfg):
    app = clean_app_settings(APP.settings.app("player"))
    plan = make_plan(song, cfg, app)
    report = dict(plan.report)
    report["pedal_presses"] = sum(1 for _t, down in plan.pedal_changes if down)
    report.pop("human", None)
    if cfg["mistakes"]:
        counts = []
        for seed in (11, 22, 33):
            trial = dict(cfg, lock_take=True, seed=seed, feel=0)
            counts.append(make_plan(song, trial, app).report.get("human", {}).get("mistakes", 0))
        report["expected_mistakes"] = round(sum(counts) / len(counts), 1)
    report["song_pedal"] = sum(1 for p in song.pedal if p[3] >= 64)
    return report


def api_song_settings(h, query):
    body = h._json_body()
    key = str(body.get("key", ""))
    recent = APP.playback.last_seeds.get(key)

    def change(merged):                 # only the settings this request sent; the rest stay as stored
        for name in ("speed", "transpose", "tracks", "max_notes", "cleanup", "feel", "mistakes", "loop_a", "loop_b",
                     *SONG_CHOICES):
            if name in body:
                merged[name] = body[name]
        if "lock_take" in body:
            if body["lock_take"]:
                if not merged.get("lock_take") and recent:      # locking now: keep the take you just heard
                    merged["seed"] = recent
                elif not isinstance(merged.get("seed"), int):
                    merged["seed"] = random.randrange(1, 2 ** 31)
                merged["lock_take"] = True
            else:
                merged["lock_take"] = False
        return clean_song_settings(merged, song), None

    with APP.library.lock:                      # a delete waits for this, so nothing is saved to a deleted song
        song, _meta = APP.library.get(key)
        cfg, rev, _ = APP.settings.update_song(key, change)
    SETUP_NOTES.pop(key, None)
    APP.playback.settings_changed(key)
    EVENTS.send("song-settings", {"key": key, "settings": cfg, "rev": rev, "from": "page"})
    return {"settings": cfg, "rev": rev, "report": plan_report(song, cfg)}


def api_app_settings(h, query):
    if h.command == "POST":
        current = clean_app_settings(APP.settings.app("player"))
        body = h._json_body()
        if body.get("layout"):
            problem = pp.check_layout(str(body["layout"]).strip().replace(" ", ""),
                                      _int(body.get("layout_lowest", current.get("layout_lowest", 36)), 36, 12, 108))
            if problem:
                raise ApiError(400, problem)
        current.update({k: v for k, v in body.items() if k in APP_DEFAULTS})
        if "presets" in current:
            current["presets"] = clean_presets(current["presets"])
        APP.settings.set_app("player", clean_app_settings(current))
    app = clean_app_settings(APP.settings.app("player"))
    apply_layout(app)
    return {"player": app, "standard_layout": pp.STANDARD_LAYOUT}


def api_best_fit(h, query):
    body = h._json_body()
    key = str(body.get("key", ""))
    song, _meta = APP.library.get(key)
    cfg = clean_song_settings(APP.settings.song(key), song)
    low, high = (pp.LOWEST_88, pp.HIGHEST_88) if cfg["range_mode"] == "88" else (pp.LOWEST_NOTE, pp.HIGHEST_NOTE)
    shift = pp.best_fit_transpose(song, set(cfg["tracks"]) or None, low=low, high=high)
    pitches = [n.pitch for n in pp.playable_notes(song, set(cfg["tracks"]) or None)]
    outside = sum(1 for p in pitches if not low <= p + shift <= high)
    return {"transpose": shift, "outside": outside}


def api_song_best(h, query):
    key = str(h._json_body().get("key", ""))
    app = clean_app_settings(APP.settings.app("player"))

    def change(stored):
        before = clean_song_settings(stored, song)
        after, said = best_settings(song, before, app)
        return after, (before, said)

    with APP.library.lock:
        song, _meta = APP.library.get(key)
        after, rev, (before, said) = APP.settings.update_song(key, change)
    SETUP_NOTES.pop(key, None)
    APP.playback.settings_changed(key)
    EVENTS.send("song-settings", {"key": key, "settings": after, "rev": rev, "from": "page"})
    playable = any(t.notes and t.channels != {9} for t in song.tracks)    # False: drums only, or no notes
    return {"settings": after, "rev": rev, "report": plan_report(song, after), "changes": said, "previous": before,
            "playable": playable}


UI_DEFAULTS = {"more_open": False, "welcomed": False}


def api_ui(h, query):
    """Small things the app window remembers: whether More controls is open, whether the welcome was seen."""
    ui = dict(UI_DEFAULTS, **{k: v for k, v in (APP.settings.app("ui") or {}).items() if k in UI_DEFAULTS})
    if h.command == "POST":
        body = h._json_body()
        ui.update({k: bool(v) for k, v in body.items() if k in UI_DEFAULTS})
        APP.settings.set_app("ui", ui)
    return ui


def api_play(h, query):
    body = h._json_body()
    return APP.playback.play(str(body.get("key", "")), float(body.get("start", 0.0) or 0.0))


def api_pause(h, query):
    return APP.playback.pause()


def api_resume(h, query):
    return APP.playback.resume()


def api_stop(h, query):
    return APP.playback.stop()


def api_seek(h, query):
    body = h._json_body()
    return APP.playback.seek(float(body.get("pos", 0.0) or 0.0))


def api_add_midi(h, query):
    name = Path(q1(query, "name", "Song.mid")).name
    if Path(name).suffix.lower() not in MIDI_TYPES:
        raise ApiError(400, "That's not a MIDI file (.mid).")
    tmp = INCOMING_DIR / f"{uuid.uuid4().hex}.mid"
    h._save_upload(tmp)
    try:
        pp.load_midi(str(tmp))
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise ApiError(400, f"{name} isn't a readable MIDI file ({exc}).")
    dest = APP.library.unique_path(Path(name).stem)
    shutil.move(str(tmp), dest)
    key = dest.relative_to(SONGS_DIR).as_posix()
    APP.library.refresh(written={key})
    EVENTS.send("library", {"sig": APP.library.sig})
    return {"key": key}


def api_import_audio(h, query):
    name = Path(q1(query, "name", "Recording.mp3")).name
    suffix = Path(name).suffix.lower()
    if suffix not in AUDIO_TYPES:
        raise ApiError(400, f"PianoPilot can't convert {suffix or 'that kind of'} files. "
                            "Use an MP3, WAV, M4A or video file.")
    tmp = INCOMING_DIR / f"{uuid.uuid4().hex}{suffix}"
    h._save_upload(tmp)
    job = APP.audio.add_job(name, tmp)
    return {"job": job["id"]}


def api_jobs_cancel(h, query):
    return APP.audio.cancel(str(h._json_body().get("id", "")))


def api_jobs_dismiss(h, query):
    return APP.audio.dismiss(str(h._json_body().get("id", "")))


def api_audio_status(h, query):
    return APP.audio.status(refresh=q1(query, "refresh") == "1")


def api_audio_setup(h, query):
    body = h._json_body()
    use_python = body.get("python")
    if use_python not in (None, "3.13", "3.12"):
        raise ApiError(400, "Bad Python version.")
    return APP.audio.start_setup(use_python)


def api_model_upload(h, query):
    dest = DATA_DIR / "model-upload.part"
    try:
        h._save_upload(dest)
        install_model(dest)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(400, str(exc).replace(f"The {dest.name} in {dest.parent}", "The file you chose"))
    finally:
        dest.unlink(missing_ok=True)      # installed or rejected, the upload itself isn't needed any more
    return {"ok": True}


def api_queue(h, query):
    if h.command == "POST":
        body = h._json_body()
        action = body.get("action")
        if action not in ("add", "next", "remove", "move", "clear", "shuffle", "shuffle_all"):
            raise ApiError(400, "Unknown queue action.")
        key = body.get("key")
        if key is not None and not APP.library.exists(str(key)):
            raise ApiError(404, "That song isn't in the Songs folder anymore.")
        index = body.get("index")
        index = int(index) if isinstance(index, (int, float)) else None
        all_keys = [m["key"] for m in APP.library.listing()["songs"] if not m.get("error")]
        return {"items": APP.queue.change(action, key, index, all_keys)}
    return {"items": APP.queue.items()}


_BIN = None
DELETE_LOCK = threading.Lock()          # one delete at a time


def _bin_api():
    """The Windows calls for the Recycle Bin, set up once."""
    global _BIN
    if _BIN is None:
        import ctypes
        from ctypes import wintypes
        packed = {"_pack_": 1} if ctypes.sizeof(ctypes.c_void_p) == 4 else {}   # 32-bit Windows packs these tightly
        op_struct = type("SHFILEOPSTRUCTW", (ctypes.Structure,), dict(packed, _fields_=[
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]))
        info_struct = type("SHQUERYRBINFO", (ctypes.Structure,), dict(packed, _fields_=[
            ("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]))
        shell32, kernel32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("kernel32"), ctypes.WinDLL("ole32")
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(op_struct)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(info_struct)]
        shell32.SHQueryRecycleBinW.restype = ctypes.c_long
        kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetDriveTypeW.restype = wintypes.UINT
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        _BIN = (ctypes, shell32, kernel32, ole32, op_struct, info_struct)
    return _BIN


def _recycle(path):
    """Put a file in the Recycle Bin. Returns "bin"; "gone" (Windows couldn't recycle it and you said yes to
    deleting it for good); "kept" (you said no to that); or None when this drive has no Recycle Bin."""
    ctypes, shell32, kernel32, ole32, op_struct, info_struct = _bin_api()
    root = os.path.splitdrive(str(path))[0] + "\\"
    if kernel32.GetDriveTypeW(root) != 3:          # 3 = a fixed drive; the others have no Recycle Bin
        return None
    started = ole32.CoInitializeEx(None, 2) in (0, 1)      # the shell wants COM on this thread
    try:
        def in_bin():
            info = info_struct()
            info.cbSize = ctypes.sizeof(info_struct)
            return info.i64NumItems if shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) == 0 else None
        before = in_bin()
        if before is None:
            return None
        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO = 3, 0x4, 0x10, 0x40
        FOF_NOERRORUI, FOF_WANTNUKEWARNING = 0x400, 0x4000     # if it can't be recycled, Windows asks first
        owner = (find_windows(APP_NAME) or [None])[0]           # so that question shows over PianoPilot
        op = op_struct(owner, FO_DELETE, str(path) + "\0", None,
                       FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI | FOF_WANTNUKEWARNING,
                       False, None, None)
        code = shell32.SHFileOperationW(ctypes.byref(op))
        if op.fAnyOperationsAborted and os.path.lexists(path):
            return "kept"
        if code or os.path.lexists(path):
            raise ApiError(500, f"Windows couldn't delete it (code {code}). "
                                "If it's open in another program, close that and try again.")
        after = in_bin()
        return "bin" if after is not None and after > before else "gone"
    finally:
        if started:
            ole32.CoUninitialize()


def delete_song_file(path):
    """Delete a song the way Explorer does, into the Recycle Bin, so it can be put back. Where a drive has no
    Recycle Bin (or not on Windows) it moves into data/Deleted songs instead.
    Returns "bin", "folder", "gone" or "kept" (see _recycle)."""
    if IS_WINDOWS:
        where = _recycle(path)
        if where:
            return where
    bin_dir = DATA_DIR / "Deleted songs"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest, n = bin_dir / path.name, 2
    while dest.exists():
        dest, n = bin_dir / f"{path.stem} ({n}){path.suffix}", n + 1
    shutil.move(str(path), str(dest))
    return "folder"


def api_song_delete(h, query):
    key = str(h._json_body().get("key", ""))
    with DELETE_LOCK:
        with APP.library.lock:
            entry = APP.library.cache.get(key)
        if not entry:
            raise ApiError(404, "That song isn't in the Songs folder anymore.")
        songs = os.path.abspath(SONGS_DIR)
        # The file itself, not where a link points: deleting a link must never delete another song.
        path = Path(os.path.abspath(os.path.join(songs, key)))
        if os.path.commonpath([songs, str(path)]) != songs or path == Path(songs):
            raise ApiError(400, "Bad song name.")
        title = entry[3]["title"]
        if APP.playback.key == key and APP.playback.active():
            APP.playback.stop(wait=True)
        where = delete_song_file(path) if os.path.lexists(path) else "gone"
        if where == "kept":
            return {"message": f"Kept “{title}”. Windows couldn't put it in the Recycle Bin, "
                               "and you chose not to delete it for good.", "kept": True}
        APP.library.refresh()           # first, so a settings change that arrives late finds no song to save to
        APP.settings.forget_song(key)
        SETUP_NOTES.pop(key, None)
    EVENTS.send("library", {"sig": APP.library.sig})
    EVENTS.send("queue", {"items": APP.queue.items()})
    EVENTS.send("favorites", {"keys": APP.settings.app("favorites", []) or []})
    if where == "bin":
        return {"message": f"Deleted “{title}”. It's in the Recycle Bin if you want it back."}
    if where == "folder":
        return {"message": f"Deleted “{title}”. It's in the PianoPilot folder, under data/Deleted songs, "
                           "if you want it back."}
    return {"message": f"Deleted “{title}”."}


def api_favorite(h, query):
    body = h._json_body()
    key = str(body.get("key", ""))
    favorites = [k for k in (APP.settings.app("favorites", []) or []) if k != key]
    if body.get("on"):
        if not APP.library.exists(key):
            raise ApiError(404, "That song isn't in the Songs folder anymore.")
        favorites.append(key)
    APP.settings.set_app("favorites", favorites)
    EVENTS.send("favorites", {"keys": favorites})
    return {"favorites": favorites}


SELECT_LOCK = threading.Lock()
SELECT_SEQ = [0]


def api_select(h, query):
    body = h._json_body()
    key = str(body.get("key", ""))
    seq = body.get("seq")
    with SELECT_LOCK:
        if isinstance(seq, (int, float)) and not isinstance(seq, bool):
            if seq <= SELECT_SEQ[0]:            # an older pick that arrived late
                return {"ok": True, "stale": True}
            SELECT_SEQ[0] = seq
        entry = APP.library.cache.get(key)      # no lock: a folder scan in progress mustn't hold this up
        if entry and entry[2] is not None:
            APP.settings.set_app("selected", key)
    return {"ok": True}


def api_next(h, query):
    return APP.playback.next()


def api_hotkey(h, query):
    body = h._json_body()
    action = body.get("action")
    if action not in HOTKEY_ACTIONS:
        raise ApiError(400, "Unknown action.")
    APP.playback.hotkey(action, str(body["key"]) if body.get("key") else None)
    return {"ok": True}


MINI_TITLE = "PianoPilot mini"


_WIN = None


def _win():
    """user32/kernel32 with proper 64-bit argument types (Windows only)."""
    global _WIN
    if _WIN is None:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        for name in ("IsWindowVisible", "IsIconic", "SetForegroundWindow", "BringWindowToTop"):
            getattr(user32, name).argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.UINT]
        user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        _WIN = (ctypes, user32, kernel32, enum_proc)
    return _WIN


def find_windows(title):
    """Visible top-level windows with exactly this title (Windows only)."""
    if not IS_WINDOWS:
        return []
    try:
        ctypes, user32, _kernel32, enum_proc = _win()
        found = []

        def check(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value == title:
                    found.append(hwnd)
            return True
        user32.EnumWindows(enum_proc(check), 0)
        return found
    except Exception:
        traceback.print_exc()
        return []


def bring_to_front(hwnd):
    """Show a window and bring it forward. Windows only lets the app that's in front do that, so borrow its
    input for a moment (AttachThreadInput); otherwise the taskbar button would just flash."""
    try:
        _ctypes, user32, kernel32, _enum = _win()
        user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)      # SW_RESTORE only when minimized
        front = user32.GetForegroundWindow()
        mine = kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(front, None) if front else 0
        attached = bool(theirs and theirs != mine and user32.AttachThreadInput(mine, theirs, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(mine, theirs, False)
        return True
    except Exception:
        traceback.print_exc()
        return False


def keep_on_top(title, timeout=10.0):
    """Wait for a window with this title to appear, then keep it above other windows."""
    if not IS_WINDOWS:
        return
    _ctypes, user32, _kernel32, _enum = _win()
    end = time.time() + timeout
    while time.time() < end:
        found = find_windows(title)
        if found:
            for hwnd in found:   # HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001)
            return
        time.sleep(0.25)


def api_mini(h, query):
    open_minis = find_windows(MINI_TITLE)
    if open_minis:                      # one mini player is enough: bring it forward instead
        bring_to_front(open_minis[0])
        return {"ok": True, "already": True}
    port = h.server.server_address[1]
    open_window(f"http://127.0.0.1:{port}/mini", size=(420, 210))
    threading.Thread(target=keep_on_top, args=(MINI_TITLE,), daemon=True).start()
    return {"ok": True}


def api_focus_main(h, query):
    if not focus_existing_window():
        open_window(f"http://127.0.0.1:{h.server.server_address[1]}/")
    return {"ok": True}


def _sheet_body(h):
    body = h._json_body()
    text = str(body.get("text", ""))
    if len(text) > 200_000:
        raise ApiError(413, "That sheet is too long.")
    try:
        bpm = float(body.get("bpm", 120))
    except (TypeError, ValueError):
        bpm = 120.0
    apply_layout(clean_app_settings(APP.settings.app("player")))
    notes, problems = pp.parse_sheet(text, bpm)
    return body, notes, problems, bpm


def api_sheet_preview(h, query):
    _body, notes, problems, _bpm = _sheet_body(h)
    return {"count": len(notes), "duration": round(max((n.end for n in notes), default=0.0), 2),
            "problems": problems, "notes": [[round(n.start, 3), round(n.end, 3), n.pitch] for n in notes[:20000]]}


def api_sheet_save(h, query):
    body, notes, problems, bpm = _sheet_body(h)
    if not notes:
        raise ApiError(400, "There are no notes in this sheet to save.")
    title = " ".join(str(body.get("title") or "Text sheet").split())[:80] or "Text sheet"
    dest = APP.library.unique_path(title)
    pp.write_midi_file(notes, dest, title, bpm)
    key = dest.relative_to(SONGS_DIR).as_posix()
    APP.library.refresh(written={key})
    EVENTS.send("library", {"sig": APP.library.sig, "select": key})
    return {"key": key, "problems": problems}


def api_sheet_export(h, query):
    key = q1(query, "key", "")
    song, meta = APP.library.get(key)
    cfg = clean_song_settings(APP.settings.song(key), song)
    apply_layout(clean_app_settings(APP.settings.app("player")))
    text, bpm = pp.export_sheet(song, cfg["transpose"], set(cfg["tracks"]) or None, cfg["range_mode"])
    header = f"# {meta['title']}\n# Tempo: {bpm} BPM. Exported by PianoPilot.\n"
    return {"text": header + text, "bpm": bpm, "title": meta["title"]}


CALIBRATION_FIXES = {
    "black": "shift_gap_ms", "chords": "shift_gap_ms", "fast": "key_hold_ms", "pedal": "pedal_method",
}


def api_calibrate_parts(h, query):
    return {"parts": [{k: v for k, v in part.items() if k in ("id", "title", "question")}
                      for part in pp.calibration_parts()]}


def api_calibrate_play(h, query):
    part_id = h._json_body().get("id")
    part = next((p for p in pp.calibration_parts() if p["id"] == part_id), None)
    if not part:
        raise ApiError(400, "Unknown test part.")
    return APP.playback.play_custom(part["notes"], f"Test: {part['title']}", pedal=bool(part.get("pedal")),
                                    range_mode=part.get("range", "octave"))


def api_calibrate_answer(h, query):
    body = h._json_body()
    part_id, ok = body.get("id"), bool(body.get("ok"))
    app = clean_app_settings(APP.settings.app("player"))
    if part_id == "wide":                     # remembered for "Best for this song"
        APP.settings.set_app("player", clean_app_settings(dict(app, has_88=ok)))
    if ok:
        msgs = {"wide": "Your piano has the extra 88-key notes. Best for this song will use them on songs with very low "
                        "or high notes.",
                "pedal": "The pedal works with your current setting."}
        return {"message": msgs.get(part_id, "Good, nothing to change."), "retry": False}
    if part_id == "white":
        return {"message": "Make sure you're sitting at the piano and the chat box is closed, then try again. "
                           "If it's still wrong, this piano may use a different key layout (Settings, Key layout).",
                "retry": True}
    if part_id == "wide":
        return {"message": "This piano doesn't have the extra notes, so notes outside the piano will move by an octave.",
                "retry": False}
    setting = CALIBRATION_FIXES.get(part_id)
    if setting == "shift_gap_ms":
        new = {0: 20, 20: 40}.get(app["shift_gap_ms"], min(60, app["shift_gap_ms"] + 20))
        message = f"Added a {new} ms pause around Shift for black keys. Play it again to check."
    elif setting == "key_hold_ms":
        new = {20: 40, 40: 70}.get(app["key_hold_ms"], min(120, app["key_hold_ms"] + 30))
        message = f"Keys are now held for at least {new} ms. Play it again to check."
    elif setting == "pedal_method":
        new = {"hold": "tap", "tap": "notes", "notes": "hold"}[app["pedal_method"]]
        message = {"tap": "Switched to tapping Space on and off. Play it again to check.",
                   "notes": "Switched to holding the notes longer instead of using a pedal. Play it again to check.",
                   "hold": "Back to holding Space. This piano's pedal may not work with PianoPilot."}[new]
    else:
        return {"message": "Nothing to change for this part.", "retry": False}
    app[setting] = new
    APP.settings.set_app("player", clean_app_settings(app))
    return {"message": message, "retry": True, "changed": {setting: new}}


def api_open_folder(h, query):
    if IS_WINDOWS:
        os.startfile(str(SONGS_DIR))  # noqa: S606 (opens Explorer on the Songs folder)
    return {"ok": True, "path": str(SONGS_DIR)}



# ------------------------- shortcuts, reset, help -------------------------

SHORTCUT_NAME = "PianoPilot.lnk"


def shortcut_paths():
    if not IS_WINDOWS:      # lets the whole flow run in tests; nothing outside data/ is touched
        test = DATA_DIR / "test-shortcuts"
        return {"desktop": test / "Desktop" / SHORTCUT_NAME, "start_menu": test / "Start menu" / SHORTCUT_NAME}
    programs = Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft/Windows/Start Menu/Programs"
    return {"desktop": known_folder(FOLDER_DESKTOP, Path.home() / "Desktop") / SHORTCUT_NAME,
            "start_menu": known_folder(FOLDER_PROGRAMS, programs) / SHORTCUT_NAME}


def shortcut_target():
    """(program, arguments) a shortcut should run: PianoPilot.exe, or windowless Python as a fallback."""
    exe = APP_DIR / "PianoPilot.exe"
    if exe.is_file():
        return str(exe), ""
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    return str(windowed if windowed.is_file() else python), f'"{APP_DIR / "pianopilot.py"}"'


def shortcut_script(action):
    """PowerShell that makes (or removes) the shortcuts. Every value is single-quoted, so nothing is interpreted."""
    def q(text):
        text = str(text)
        for mark in "'\u2018\u2019\u201a\u201b":     # PowerShell treats all of these as single quotes
            text = text.replace(mark, mark * 2)
        return "'" + text + "'"
    target, args = shortcut_target()
    body = ["$shell = New-Object -ComObject WScript.Shell"]
    for path in shortcut_paths().values():
        if action == "remove":
            body.append(f"if (Test-Path -LiteralPath {q(path)}) {{ Remove-Item -LiteralPath {q(path)} }}")
            continue
        body += [f"$link = $shell.CreateShortcut({q(path)})",
                 f"$link.TargetPath = {q(target)}",
                 f"$link.Arguments = {q(args)}",
                 f"$link.WorkingDirectory = {q(APP_DIR)}",
                 f"$link.IconLocation = {q(str(UI_DIR / 'pianopilot.ico') + ',0')}",
                 "$link.Description = 'Plays songs on Roblox pianos'",
                 "$link.Save()"]
    # Errors go to plain stdout: with -EncodedCommand, stderr comes back wrapped in CLIXML.
    return "\n".join(["$ErrorActionPreference = 'Stop'", "try {"] + ["  " + line for line in body] +
                     ["} catch {", "  [Console]::Out.WriteLine($_.Exception.Message)", "  exit 1", "}"])


def shortcuts_view():
    paths = shortcut_paths()
    return {"desktop": paths["desktop"].is_file(), "start_menu": paths["start_menu"].is_file(),
            "uses_exe": (APP_DIR / "PianoPilot.exe").is_file()}


def api_shortcuts(h, query):
    if h.command == "GET":
        return shortcuts_view()
    action = h._json_body().get("action", "add")
    if action not in ("add", "remove"):
        raise ApiError(400, "Unknown shortcut action.")
    script = shortcut_script(action)
    if IS_WINDOWS:
        import base64
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-EncodedCommand", encoded], capture_output=True, text=True, errors="replace",
                                  timeout=60, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(500, f"Windows didn't make the shortcuts ({exc}).")
        if done.returncode != 0:
            lines = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
            lines += [line.strip() for line in (done.stderr or "").splitlines()
                      if line.strip() and not line.startswith("#< CLIXML") and not line.startswith("<Objs")]
            detail = lines[0] if lines else "unknown error"
            log("Shortcut script failed:", done.stdout, done.stderr)
            raise ApiError(500, f"Windows didn't make the shortcuts: {detail}")
    else:
        for path in shortcut_paths().values():
            if action == "remove":
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(script, encoding="utf-8")
    view = shortcuts_view()
    if action == "add" and not (view["desktop"] and view["start_menu"]):
        raise ApiError(500, "Windows didn't make the shortcuts. Try again, or start PianoPilot.exe from its folder.")
    view["message"] = ("Added. PianoPilot is on your desktop and in the Start menu." if action == "add"
                       else "Removed the desktop and Start menu shortcuts.")
    return view


def api_reset(h, query):
    what = h._json_body().get("what")
    if what == "app":
        presets = clean_app_settings(APP.settings.app("player"))["presets"]     # your saved mixes stay
        APP.settings.set_app("player", clean_app_settings({"presets": presets}))
        apply_layout(clean_app_settings(APP.settings.app("player")))
        message = "Settings are back to how they started."
    elif what == "songs":
        APP.settings.reset_songs()
        SETUP_NOTES.clear()
        if APP.playback.key:
            APP.playback.settings_changed(APP.playback.key)
        message = "Every song is back to its normal settings."
    else:
        raise ApiError(400, "Nothing to reset.")
    EVENTS.send("reset", {"what": what})
    return {"message": message}


def api_about(h, query):
    return {"version": VERSION, "python": sys.version.split()[0], "python_path": sys.executable,
            "app_folder": str(APP_DIR), "songs_folder": str(SONGS_DIR), "log": str(LOG_FILE),
            "has_log": LOG_FILE.is_file(), "has_exe": (APP_DIR / "PianoPilot.exe").is_file(),
            "simulated": SIMULATE, "is_windows": IS_WINDOWS}


def api_open_log(h, query):
    sys.stdout.flush()
    sys.stderr.flush()
    if not LOG_FILE.is_file():
        raise ApiError(404, "There's no log yet.")
    if IS_WINDOWS:
        os.startfile(str(LOG_FILE))  # noqa: S606 (opens the log in Notepad)
    return {"ok": True, "path": str(LOG_FILE)}


ROUTES = {
    ("GET", "/api/library"): api_library,
    ("GET", "/api/song"): api_song,
    ("POST", "/api/song/settings"): api_song_settings,
    ("POST", "/api/song/best-fit"): api_best_fit,
    ("POST", "/api/song/best"): api_song_best,
    ("POST", "/api/song/delete"): api_song_delete,
    ("GET", "/api/ui"): api_ui,
    ("POST", "/api/ui"): api_ui,
    ("POST", "/api/play"): api_play,
    ("POST", "/api/pause"): api_pause,
    ("POST", "/api/resume"): api_resume,
    ("POST", "/api/stop"): api_stop,
    ("POST", "/api/seek"): api_seek,
    ("POST", "/api/add-midi"): api_add_midi,
    ("POST", "/api/import-audio"): api_import_audio,
    ("POST", "/api/jobs/cancel"): api_jobs_cancel,
    ("POST", "/api/jobs/dismiss"): api_jobs_dismiss,
    ("GET", "/api/audio/status"): api_audio_status,
    ("POST", "/api/audio/setup"): api_audio_setup,
    ("POST", "/api/audio/model-upload"): api_model_upload,
    ("POST", "/api/open-folder"): api_open_folder,
    ("GET", "/api/app-settings"): api_app_settings,
    ("POST", "/api/app-settings"): api_app_settings,
    ("GET", "/api/queue"): api_queue,
    ("POST", "/api/queue"): api_queue,
    ("POST", "/api/favorite"): api_favorite,
    ("POST", "/api/select"): api_select,
    ("POST", "/api/next"): api_next,
    ("POST", "/api/hotkey"): api_hotkey,
    ("POST", "/api/sheet/preview"): api_sheet_preview,
    ("POST", "/api/sheet/save"): api_sheet_save,
    ("GET", "/api/sheet/export"): api_sheet_export,
    ("GET", "/api/calibrate/parts"): api_calibrate_parts,
    ("POST", "/api/calibrate/play"): api_calibrate_play,
    ("POST", "/api/calibrate/answer"): api_calibrate_answer,
    ("POST", "/api/mini"): api_mini,
    ("POST", "/api/focus-main"): api_focus_main,
    ("GET", "/api/shortcuts"): api_shortcuts,
    ("POST", "/api/shortcuts"): api_shortcuts,
    ("POST", "/api/reset"): api_reset,
    ("GET", "/api/about"): api_about,
    ("POST", "/api/open-log"): api_open_log,
}


# ============================== WINDOW ==============================

def open_window(url, size=(1180, 780)):
    """Open the app in its own window (Edge or Chrome app mode), or a normal browser tab."""
    if IS_WINDOWS:
        roots = [os.environ.get(v, "") for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        browsers = [Path(r) / "Microsoft/Edge/Application/msedge.exe" for r in roots if r] + \
                   [Path(r) / "Google/Chrome/Application/chrome.exe" for r in roots if r]
        for exe in browsers:
            if exe.is_file():
                try:
                    subprocess.Popen([str(exe), f"--app={url}", f"--window-size={size[0]},{size[1]}"],
                                     creationflags=NO_WINDOW)
                    return
                except OSError:
                    continue
    webbrowser.open(url)


def existing_instance():
    """Port of a PianoPilot that's already running, or None."""
    ports = []
    try:
        ports.append(json.loads(SERVER_FILE.read_text(encoding="utf-8"))["port"])
    except Exception:
        pass
    ports.append(PREFERRED_PORT)
    import urllib.request
    for port in dict.fromkeys(ports):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
                if json.loads(r.read()).get("app") == APP_NAME:
                    return port
        except Exception:
            continue
    return None


def focus_existing_window():
    """Bring an open PianoPilot window to the front instead of opening another one."""
    windows = find_windows(APP_NAME)
    return bool(windows) and bring_to_front(windows[0])


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False      # never share the port with another copy of the app
    allow_reuse_port = False

    def server_bind(self):
        if IS_WINDOWS:
            import socket
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server():
    try:
        return Server(("127.0.0.1", PREFERRED_PORT), Handler)
    except OSError:
        pass
    return Server(("127.0.0.1", 0), Handler)


def watch_for_close(server):
    """Quit when the app window has been closed for a while."""
    while True:
        time.sleep(2)
        if EVENTS.ever_connected and EVENTS.count() == 0 and time.time() - EVENTS.last_seen > 20:
            log("App window closed. Shutting down.")
            try:
                APP.playback.stop(wait=True)
            except Exception:
                pass
            server.shutdown()
            return


_console_handler = None


def release_keys_on_console_close():
    """If the command window is closed mid-song, let go of every key first."""
    global _console_handler
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes

        def on_event(_event):
            try:
                APP.playback.stop(wait=True)
            except Exception:
                pass
            return False
        _console_handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(on_event)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
    except Exception:
        pass


def show_error(text):
    """Startup problems: show a message box, since the command window is minimized."""
    log(text)
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10)
        except Exception:
            pass


_INSTANCE_MUTEX = None


def claim_single_instance():
    """Windows named mutex as a backstop against two copies starting at the same moment."""
    global _INSTANCE_MUTEX
    if not IS_WINDOWS:
        return True
    try:
        ctypes, _user32, kernel32, _enum = _win()
        _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\PianoPilot.App")
        return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS
    except Exception:
        traceback.print_exc()
        return True


def show_running_copy(no_window):
    """Another PianoPilot is open: bring its window forward (or open one) instead of starting again."""
    port = existing_instance()
    if not port:
        return False
    log(f"{APP_NAME} is already running.")
    if not no_window and not focus_existing_window():
        open_window(f"http://127.0.0.1:{port}/")
    return True


def main():
    global APP
    no_window = "--no-window" in sys.argv
    started_from = "command window" if getattr(sys.stdout, "console", sys.stdout) is not None else "no window"
    log(f"--- {APP_NAME} {VERSION} starting {time.strftime('%Y-%m-%d')} with Python {sys.version.split()[0]} "
        f"({Path(sys.executable).name}, {started_from})")
    if show_running_copy(no_window):
        return
    if not claim_single_instance():
        # Another copy is starting right now. Give it a moment to open, then hand over to it.
        for _ in range(40):
            time.sleep(0.5)
            if show_running_copy(no_window):
                return
        log("Another copy seems stuck while starting. Starting anyway.")
    server = make_server()            # take the port and answer pings at once, so a quick second launch finds us
    port = server.server_address[1]
    http = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="http", daemon=True)
    http.start()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_FILE.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    try:
        remember_python()
        APP = App()
        APP_READY.set()
        release_keys_on_console_close()
        url = f"http://127.0.0.1:{port}/"
        threading.Thread(target=APP.library.watch, name="library", daemon=True).start()
        threading.Thread(target=APP.hotkeys.run, name="hotkeys", daemon=True).start()
        threading.Thread(target=watch_for_close, args=(server,), name="close-watch", daemon=True).start()
        log(f"{APP_NAME} {VERSION} running at {url}")
        log(f"Songs folder: {SONGS_DIR}")
        if SIMULATE:
            log("Simulation mode: key presses are counted, not sent.")
        if not no_window:
            open_window(url)
        while http.is_alive():
            http.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if APP:
                APP.playback.stop(wait=True)
        except Exception:
            pass
        if http.is_alive():
            server.shutdown()
        server.server_close()
        try:
            SERVER_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        show_error(f"PianoPilot couldn't start:\n\n{exc}\n\nDetails are in {LOG_FILE}")
        sys.exit(EXIT_REPORTED)
