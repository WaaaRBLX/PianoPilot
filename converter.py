"""PianoPilot audio converter: piano recording (MP3, WAV, video...) -> MIDI.

Runs as its own process so the app stays responsive. Prints one JSON object per line:
  {"type": "status", "text": ...}
  {"type": "progress", "done": 3, "total": 25}
  {"type": "done", "notes": 637, "pedals": 4, "seconds": 41.2, "device": "Graphics card"}
  {"type": "error", "text": ...}

  python converter.py --check
  python converter.py --model data/model --input song.mp3 --output Songs/song.mid [--device auto|gpu|cpu]
"""
import argparse
import contextlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "engine"))

SAMPLE_RATE = 44100


def emit(kind, **data):
    data["type"] = kind
    print(json.dumps(data), flush=True)


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def check():
    """Report which pieces are installed. Never raises."""
    info = {"python": sys.version.split()[0], "executable": sys.executable}
    missing = []
    for name in ("numpy", "scipy", "onnxruntime"):
        try:
            mod = __import__(name)
            info[name] = getattr(mod, "__version__", "?")
        except Exception:
            missing.append(name)
    decoder = "ffmpeg" if find_ffmpeg() else None
    if decoder is None:
        try:
            import soundfile  # noqa: F401
            decoder = "soundfile"
        except Exception:
            missing.append("soundfile")
    info["decoder"] = decoder
    info["missing"] = missing
    info["gpu"] = False
    if "onnxruntime" not in missing:
        try:
            import onnxruntime as ort
            info["gpu"] = "DmlExecutionProvider" in ort.get_available_providers()
        except Exception:
            pass
    info["ok"] = not missing and decoder is not None
    emit("check", **info)


def load_audio(path):
    """Return float32 array (samples, 2) at 44.1 kHz."""
    import numpy as np
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
            capture_output=True, creationflags=flags)
        if result.returncode != 0 or not result.stdout:
            message = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError("Couldn't read this file's audio. " + (message[-1] if message else ""))
        return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2).copy()
    import soundfile as sf
    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    audio = audio[:, :2]
    if rate != SAMPLE_RATE:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(rate), SAMPLE_RATE)
        audio = resample_poly(audio, SAMPLE_RATE // g, int(rate) // g, axis=0).astype(np.float32)
    return np.ascontiguousarray(audio)


def _varlen(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


AUDIO_MARK = "PianoPilot audio import"


def write_midi(events, path, title):
    """Notes plus sustain (CC64) and soft (CC67) pedal, the same events Pianoscribe writes."""
    tpq, tempo_us = 960, 500000
    tick = lambda s: int(round(max(0.0, s) * tpq * 1e6 / tempo_us))
    msgs = []
    for ev in events:
        vel = max(1, min(127, int(ev.velocity)))
        if ev.pitch > 0:
            p = max(0, min(127, int(ev.pitch)))
            on = tick(ev.start)
            msgs.append((on, 2, bytes([0x90, p, vel])))
            msgs.append((max(tick(ev.end), on + 1), 0, bytes([0x80, p, 0])))
        else:
            cc = -int(ev.pitch)
            msgs.append((tick(ev.start), 1, bytes([0xB0, cc, max(64, vel)])))
            msgs.append((tick(ev.end), 0, bytes([0xB0, cc, 0])))
    msgs.sort(key=lambda m: (m[0], m[1], m[2][1]))
    name = title.encode("utf-8")[:120]
    body = bytearray(b"\x00\xFF\x03" + _varlen(len(name)) + name)
    mark = AUDIO_MARK.encode("ascii")           # lets PianoPilot tell recordings from written MIDI files
    body += b"\x00\xFF\x01" + _varlen(len(mark)) + mark
    body += b"\x00\xFF\x51\x03" + tempo_us.to_bytes(3, "big")
    body += b"\x00\xC0\x00"
    last = 0
    for t, _, m in msgs:
        body += _varlen(t - last) + m
        last = t
    body += b"\x00\xFF\x2F\x00"
    data = b"MThd" + struct.pack(">IHHH", 6, 0, 1, tpq) + b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
    tmp = Path(str(path) + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def convert(model_dir, input_path, output_path, device, title=None, gpu_batch=None):
    try:
        import threadpoolctl  # noqa: F401
    except Exception:
        stub = types.ModuleType("threadpoolctl")
        stub.threadpool_limits = lambda *a, **k: contextlib.nullcontext()
        sys.modules["threadpoolctl"] = stub
    from pianoscribe_engine import piano_engine, runtime_service

    if gpu_batch:
        runtime_service.GPU_BATCH_SIZE = max(1, min(32, int(gpu_batch)))
    # Time the engine's audio analysis without changing its code: wrap the function it calls by name.
    extract = piano_engine._extract_features

    def timed_extract(*args, **kwargs):
        t = time.perf_counter()
        try:
            return extract(*args, **kwargs)
        finally:
            runtime_service.add_time("features", time.perf_counter() - t)
    piano_engine._extract_features = timed_extract

    batch_used = [None]

    def on_status(text):
        found = re.search(r"GPU batch(?: reduced to)?:? (\d+)", str(text))
        if found:
            batch_used[0] = int(found.group(1))
        if found and "reduced" in str(text):    # the graphics card couldn't take the bigger batch: PianoPilot remembers
            emit("batch", size=batch_used[0])

    model_dir = Path(model_dir)
    emit("status", text="Reading audio")
    t_read = time.perf_counter()
    audio = load_audio(input_path)
    t_read = time.perf_counter() - t_read
    seconds = len(audio) / SAMPLE_RATE
    if seconds < 0.5:
        raise RuntimeError("This file has no audio, or it's too short to convert.")

    use_gpu = device in ("auto", "gpu") and runtime_service.gpu_available()
    attempts = [True, False] if use_gpu else [False]
    started = time.time()
    events = None
    used = "CPU"
    for gpu in attempts:
        runtime_service.USE_GPU = gpu
        piano_engine.reset_session_cache()
        used = "Graphics card" if gpu else "CPU"
        emit("status", text=f"Listening for notes ({used.lower()})")
        try:
            events = piano_engine.transcribe_audio_array(
                audio, SAMPLE_RATE,
                model_path_override=model_dir / "scorer.onnx",
                device_override="gpu" if gpu else "cpu",
                progress_callback=lambda done, total: emit("progress", done=done, total=total),
                status_callback=on_status)
            break
        except Exception as exc:
            if gpu:
                emit("status", text=f"Graphics card didn't work ({exc.__class__.__name__}); using the CPU instead")
                continue
            raise
    notes = sum(1 for e in events if e.pitch > 0)
    if notes == 0:
        raise RuntimeError("No piano notes were found in this audio.")
    write_midi(events, output_path, title or Path(output_path).stem)
    took = time.time() - started
    t = runtime_service.TIMES
    model = t.get("scorer", 0.0) + t.get("attributes", 0.0)
    timing = {"read": t_read, "load": t.get("load", 0.0), "features": t.get("features", 0.0), "model": model,
              "decode": max(0.0, took - t.get("load", 0.0) - t.get("features", 0.0) - model)}
    emit("done", notes=notes, pedals=len(events) - notes, seconds=round(seconds, 1),
         took=round(took, 1), device=used, batch=batch_used[0] if used == "Graphics card" else None,
         timing={k: round(v, 2) for k, v in timing.items()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--device", default="auto", choices=["auto", "gpu", "cpu"])
    ap.add_argument("--title", default=None)
    ap.add_argument("--gpu-batch", type=int, default=None)
    a = ap.parse_args()
    if a.check:
        check()
        return
    try:
        convert(a.model, a.input, a.output, a.device, a.title, a.gpu_batch)
    except Exception as exc:
        text = str(exc) or exc.__class__.__name__
        emit("error", text=text)
        sys.exit(1)


if __name__ == "__main__":
    main()
