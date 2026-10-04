"""Owner-only directories and files (see docs/privacy-and-security.md).

POSIX: the mode is passed explicitly (files ``0600``, directories ``0700``), so the umask,
which can only remove bits, never matters.

Windows has no mode bits and new objects inherit the parent's ACL, which outside the user
profile often lets every local user read. ``restrict_to_owner`` replaces it with a protected,
inheritable ACL granting full control only to the current user, SYSTEM and Administrators, so
files created inside (SQLite's ``-wal``/``-shm``) get it too. Only objects this package
creates are changed, and failing to apply an ACL (a FAT volume) is not an error.
"""

from __future__ import annotations

import sys
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SDDL_REVISION_1 = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_FILE_OBJECT = 1

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]

    def _current_user_sid() -> str | None:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            if not _advapi32.GetTokenInformation(
                token, _TOKEN_USER, buffer, needed, ctypes.byref(needed)
            ):
                return None
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not _advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
                return None
            try:
                return text.value
            finally:
                _kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            _kernel32.CloseHandle(token)

    def restrict_to_owner(path: Path) -> bool:
        """Give ``path`` a protected ACL for the current user, SYSTEM and Administrators."""
        sid = _current_user_sid()
        if sid is None:
            return False
        inherit = "OICI" if path.is_dir() else ""
        sddl = f"D:P(A;{inherit};FA;;;SY)(A;{inherit};FA;;;BA)(A;{inherit};FA;;;{sid})"
        descriptor = ctypes.c_void_p()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
        ):
            return False
        try:
            present, defaulted = wintypes.BOOL(), wintypes.BOOL()
            dacl = ctypes.c_void_p()
            if not _advapi32.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            ):
                return False
            status = _advapi32.SetNamedSecurityInfoW(
                str(path),
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            return bool(status == 0)
        finally:
            _kernel32.LocalFree(descriptor)

else:

    def restrict_to_owner(path: Path) -> bool:
        """No-op: POSIX modes are applied at creation."""
        return True


def ensure_private_directory(path: Path) -> None:
    """Create ``path`` and missing parents as owner-only directories; existing ones are untouched."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            continue  # created by someone else in the meantime
        restrict_to_owner(directory)
    if not path.is_dir():
        raise FileExistsError(f"not a directory: {path.name}")
