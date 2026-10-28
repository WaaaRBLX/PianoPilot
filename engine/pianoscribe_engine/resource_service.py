"""Stand-in for Pianoscribe's resource_service: how many CPU threads to use."""
import os

WORKERS = max(1, (os.cpu_count() or 2) - 1)


def active_worker_count() -> int:
    return WORKERS
