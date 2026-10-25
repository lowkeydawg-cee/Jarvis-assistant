"""Live computer vitals for the browser dashboard: CPU, memory, disk, battery, uptime.

psutil gives the full picture; without it, Jarvis still reports disk space.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path


def vitals() -> dict:
    gb = 1024 ** 3
    disk = shutil.disk_usage(Path.home())
    out: dict = {
        "disk": {"free_gb": round(disk.free / gb, 1), "total_gb": round(disk.total / gb, 1),
                 "percent": round(100 * (disk.total - disk.free) / disk.total)},
        "cpu": None, "memory": None, "battery": None, "uptime_h": None,
    }
    try:
        import psutil
    except ImportError:
        return out
    mem = psutil.virtual_memory()
    out["cpu"] = round(psutil.cpu_percent(interval=0.2))
    out["memory"] = {"used_gb": round((mem.total - mem.available) / gb, 1),
                     "total_gb": round(mem.total / gb, 1), "percent": round(mem.percent)}
    try:
        bat = psutil.sensors_battery()
    except Exception:
        bat = None
    if bat is not None:
        out["battery"] = {"percent": round(bat.percent), "plugged": bool(bat.power_plugged)}
    out["uptime_h"] = round((time.time() - psutil.boot_time()) / 3600, 1)
    return out
