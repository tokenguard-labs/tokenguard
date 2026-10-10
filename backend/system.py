"""Best-effort host resource detection, used to recommend local models that fit this machine."""
import logging
import os
import shutil
import subprocess
import time
from typing import Optional

log = logging.getLogger("localpilot.system")

_CACHE_TTL_S = 30.0
_cache: Optional[tuple[float, dict]] = None


def _gb_from_kib(kib: Optional[int]) -> float:
    return round(kib / (1024 * 1024), 1) if kib else 0.0


def _linux_meminfo() -> dict:
    info: dict = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts:
                    info[key.strip()] = int(parts[0])  # values are in kB
    except (OSError, ValueError):
        return {}
    return info


def _gpu_vram_gb() -> float:
    """Total VRAM in GB for an NVIDIA GPU, if nvidia-smi is available. 0.0 otherwise."""
    if not shutil.which("nvidia-smi"):
        return 0.0
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        values = [float(x) for x in out.stdout.split() if x.strip().replace(".", "", 1).isdigit()]
        if values:
            return round(sum(values) / 1024, 1)  # MiB -> GiB
    except Exception as e:  # pragma: no cover - depends on host
        log.debug("nvidia-smi lookup failed: %s", e)
    return 0.0


def system_resources(force: bool = False) -> dict:
    """Total/available RAM, swap, CPU count and (optional) GPU VRAM. Cached for 30s."""
    global _cache
    now = time.time()
    if not force and _cache and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1]

    total_kib = available_kib = swap_kib = swap_free_kib = None
    if os.path.exists("/proc/meminfo"):
        mi = _linux_meminfo()
        total_kib = mi.get("MemTotal")
        available_kib = mi.get("MemAvailable") or mi.get("MemFree")
        swap_kib = mi.get("SwapTotal")
        swap_free_kib = mi.get("SwapFree")

    if total_kib is None:  # POSIX fallback (macOS, BSD)
        try:
            page = os.sysconf("SC_PAGE_SIZE")
            total_kib = page * os.sysconf("SC_PHYS_PAGES") // 1024
            available_kib = page * os.sysconf("SC_AVPHYS_PAGES") // 1024
        except (ValueError, OSError, AttributeError):
            pass

    gpu = _gpu_vram_gb()
    info = {
        "total_ram_gb": _gb_from_kib(total_kib),
        "available_ram_gb": _gb_from_kib(available_kib),
        "swap_total_gb": _gb_from_kib(swap_kib),
        "swap_free_gb": _gb_from_kib(swap_free_kib),
        "cpu_count": os.cpu_count() or 0,
        "gpu_vram_gb": gpu,
        "has_gpu": gpu > 0,
    }
    _cache = (now, info)
    return info
