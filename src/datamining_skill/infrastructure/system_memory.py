"""Available physical memory from ``GlobalMemoryStatusEx`` (Windows), ``/proc/meminfo`` (Linux) or ``sysconf``.

Read directly to stay free of runtime dependencies. ``None`` means unknown, and the caller
applies its configured fallback.
"""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    _kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatusEx)]

    def _read_available() -> int | None:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys)

elif sys.platform.startswith("linux"):

    def _read_available() -> int | None:
        try:
            with open("/proc/meminfo", encoding="ascii", errors="replace") as meminfo:
                for line in meminfo:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None

else:

    def _read_available() -> int | None:
        try:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            return None


class SystemMemoryProbe:
    """``AvailableMemoryProvider`` backed by the OS."""

    def available_bytes(self) -> int | None:
        try:
            return _read_available()
        except OSError:  # pragma: no cover - probing must never break planning
            return None
