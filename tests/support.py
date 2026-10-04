"""Helpers shared by the mining tests."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

SIMULATION_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "simulate_mining_crash.py"


def load_simulation() -> ModuleType:
    """Import ``scripts/simulate_mining_crash.py`` (dataset generator and fault injectors).

    Reusing the script's code keeps one definition of the planted-email dataset and
    of the crash injectors for both the pytest suite and the standalone validation.
    """
    name = "simulate_mining_crash"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SIMULATION_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    import _winapi

    def _create_junction(link: Path, target: Path) -> bool:
        try:
            _winapi.CreateJunction(str(target), str(link))
            return True
        except OSError:
            return False

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]  # fmt: skip
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p,
    ]  # fmt: skip
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    def describe_dacl(path: Path) -> str:
        """The object's DACL as an SDDL string, straight from the OS (Windows only)."""
        file_object, dacl_information, sddl_revision = 1, 4, 1
        descriptor = ctypes.c_void_p()
        status = _advapi32.GetNamedSecurityInfoW(
            str(path), file_object, dacl_information, None, None, None, None, ctypes.byref(descriptor)
        )
        assert status == 0, f"GetNamedSecurityInfo failed with {status}"
        text = wintypes.LPWSTR()
        try:
            assert _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, sddl_revision, dacl_information, ctypes.byref(text), None
            )
            return str(text.value)
        finally:
            _kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
            _kernel32.LocalFree(descriptor)

else:

    def _create_junction(link: Path, target: Path) -> bool:
        return False

    def describe_dacl(path: Path) -> str:
        raise NotImplementedError("DACLs exist on Windows only")


def make_directory_link(link: Path, target: Path) -> bool:
    """Link ``link`` to the directory ``target``: a symlink, or on Windows a junction.

    Junctions need no special privilege, so Windows can exercise link-escape defences
    even where symlink creation is denied. Returns False if neither is possible.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return _create_junction(link, target)


def remove_link(link: Path) -> None:
    """Remove a link without touching its target."""
    try:
        link.unlink()
    except OSError:
        try:
            os.rmdir(link)
        except OSError:
            pass