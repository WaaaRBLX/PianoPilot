"""Stand-in for Pianoscribe's runtime_service: creates ONNX Runtime sessions.

Uses the graphics card through DirectML (onnxruntime-directml) when USE_GPU is set and
available, otherwise the CPU.
"""
import onnxruntime as ort

from . import resource_service

USE_GPU = False
GPU_BATCH_SIZE = 4


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
