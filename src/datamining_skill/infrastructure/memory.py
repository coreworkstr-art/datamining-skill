"""Process memory via ``K32GetProcessMemoryInfo`` (Windows), ``/proc`` (Linux) or ``getrusage``.

A value the platform cannot provide is ``None``, never a guess.
"""

from __future__ import annotations

import sys

from datamining_skill.domain.models import MemorySnapshot

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    _kernel32.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]

    def _read_memory() -> MemorySnapshot:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        ok = _kernel32.K32GetProcessMemoryInfo(
            _kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not ok:
            return MemorySnapshot(None, None)
        return MemorySnapshot(int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize))

elif sys.platform.startswith("linux"):

    def _read_memory() -> MemorySnapshot:
        rss: int | None = None
        peak: int | None = None
        try:
            with open("/proc/self/status", encoding="ascii", errors="replace") as status:
                for line in status:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) * 1024
                    elif line.startswith("VmHWM:"):
                        peak = int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return MemorySnapshot(None, None)
        return MemorySnapshot(rss, peak)

else:

    def _read_memory() -> MemorySnapshot:
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (ImportError, OSError):
            return MemorySnapshot(None, None)
        # macOS reports bytes, other POSIX systems kibibytes
        peak = usage if sys.platform == "darwin" else usage * 1024
        return MemorySnapshot(None, peak)


class ProcessMemoryProbe:
    """``MemoryProbe`` for the current process."""

    def snapshot(self) -> MemorySnapshot:
        try:
            return _read_memory()
        except OSError:  # pragma: no cover - defensive; probing must never break profiling
            return MemorySnapshot(None, None)
