"""Stand-in for Pianoscribe's runtime_service: creates ONNX Runtime sessions.

Uses the graphics card through DirectML (onnxruntime-directml) when USE_GPU is set and
available, otherwise the CPU. Also keeps time (TIMES) so PianoPilot's log can say where a
conversion spent it: loading the model, and running each of its two parts.
"""
import time
from pathlib import Path

import onnxruntime as ort

from . import resource_service

USE_GPU = False
GPU_BATCH_SIZE = 8           # audio pieces sent to the graphics card at once; the engine halves it if that fails
TIMES = {}                   # seconds spent: "load", "scorer", "attributes" (and "features", added by converter.py)


def add_time(name, seconds):
    TIMES[name] = TIMES.get(name, 0.0) + seconds


class _TimedSession:
    """An ONNX Runtime session that adds up how long its runs take. Everything else passes straight through."""

    def __init__(self, session, label):
        self._session = session
        self._label = label

    def run(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self._session.run(*args, **kwargs)
        finally:
            add_time(self._label, time.perf_counter() - started)

    def __getattr__(self, name):
        return getattr(self._session, name)


def gpu_available() -> bool:
    try:
        return "DmlExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def get_device_preference() -> str:
    return "gpu" if USE_GPU else "cpu"


def get_gpu_provider_preference() -> str:
    return "dml"


def is_gpu_runtime_enabled() -> bool:
    return USE_GPU


def get_gpu_runtime_path() -> str:
    return ""


def get_gpu_batch_size() -> int:
    return GPU_BATCH_SIZE


def create_session(model_path, device_preference="cpu", gpu_provider_preference="dml"):
    started = time.perf_counter()
    label = "scorer" if "scorer" in Path(str(model_path)).name else "attributes"
    session, where = _create_session(model_path, device_preference)
    add_time("load", time.perf_counter() - started)
    return _TimedSession(session, label), where


def _create_session(model_path, device_preference):
    options = ort.SessionOptions()
    options.log_severity_level = 3
    if device_preference == "gpu" and gpu_available():
        # DirectML needs these two settings
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session = ort.InferenceSession(str(model_path), options,
                                       providers=["DmlExecutionProvider", "CPUExecutionProvider"])
        return session, "Graphics card"
    options.intra_op_num_threads = resource_service.active_worker_count()
    session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
    return session, "CPU"
