"""Path confinement shared by every component that writes run artefacts."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from datamining_skill.domain.exceptions import InvalidConfigurationException, printable

DEFAULT_ALLOWED_DIRECTORIES = (".scratch", "data")

_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
        *(f"COM{n}" for n in "123456789¹²³"),
        *(f"LPT{n}" for n in "123456789¹²³"),
    }
)  # fmt: skip


def default_allowed_roots() -> list[Path]:
    """``.scratch`` and ``data`` under the current working directory."""
    return [Path.cwd() / name for name in DEFAULT_ALLOWED_DIRECTORIES]


def check_path_text(raw: str, label: str) -> None:
    """Reject path text that is dangerous wherever it points, before any filesystem call.

    The ordering matters on Windows: resolving a UNC path (``\\\\host\\share``) opens an SMB
    connection, so a containment check would already have contacted a remote machine and
    offered it the user's NTLM credentials.

    Rejected everywhere: control characters (they forge log lines and terminal output) and
    macOS resource-fork suffixes. Rejected on Windows: UNC, device and extended-length
    prefixes, colons past a drive letter (alternate data streams), reserved device names
    (``NUL``, ``COM1.txt``) and names ending in a dot or space, which Windows strips and so
    aliases another name. Raises ``InvalidConfigurationException`` naming the category only.
    """
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise InvalidConfigurationException(f"{label} must not contain control characters")
    parts = raw.replace("\\", "/").split("/")
    if any(part.casefold() == "..namedfork" for part in parts):
        raise InvalidConfigurationException(f"{label} must not address a resource fork")
    if os.name != "nt":
        return
    if raw.replace("\\", "/").startswith("//"):
        raise InvalidConfigurationException(f"{label} must not be a network or device path")
    body = raw[2:] if len(raw) >= 2 and raw[1] == ":" else raw
    if ":" in body:
        raise InvalidConfigurationException(f"{label} must not contain a stream specifier")
    for part in parts:
        if part in ("", ".", ".."):
            continue
        if part.endswith((".", " ")):
            raise InvalidConfigurationException(f"{label} must not end a name with a dot or space")
        if part.split(".")[0].rstrip(" ").upper() in _WINDOWS_DEVICE_NAMES:
            raise InvalidConfigurationException(f"{label} must not use a reserved device name")


def resolve_within(
    path: str | os.PathLike[str],
    roots: Sequence[Path],
    *,
    label: str,
    allow_root: bool = False,
) -> Path:
    """Resolve ``path`` (following symlinks and ``..``) and require it to lie inside a root.

    ``label`` names the argument in errors, which never include the offending path. A root
    itself, or any existing directory, is refused unless ``allow_root``. Raises
    ``InvalidConfigurationException``.
    """
    candidate = Path(path).expanduser().resolve()
    for root in roots:
        resolved_root = root.resolve()
        if candidate == resolved_root:
            if allow_root:
                return candidate
            continue
        if candidate.is_relative_to(resolved_root):
            if not allow_root and candidate.is_dir():
                raise InvalidConfigurationException(f"{label} path is a directory")
            return candidate
    allowed = ", ".join(f"'{printable(root.name)}/'" for root in roots)
    raise InvalidConfigurationException(
        f"{label} must be located inside one of the allowed directories: {allowed}"
    )


def confined_child(parent: Path, name: str) -> Path:
    """Return ``parent/name``, refusing a symlink or junction that leads out of ``parent``.

    A planted ``.scratch`` link would redirect state and scratch files, which hold mined data,
    to any directory the process can write.
    """
    base = parent.resolve()
    child = base / name
    if child.exists() and not child.resolve().is_relative_to(base):
        raise InvalidConfigurationException(
            f"'{printable(name)}' leads outside the workspace; remove the link or choose another workspace"
        )
    return child
